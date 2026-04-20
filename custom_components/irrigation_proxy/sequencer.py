"""Sequencer – runs irrigation zones in sequence, one at a time.

Flow per zone (when a master valve is configured):

    1.  Zone valve OPEN   (still pressureless, master is closed)
    2.  Master valve OPEN (water starts flowing through the zone)
    3.  Wait the zone duration
    4.  Master valve CLOSE (flow stops, line pressure decays)
    5.  Depressurize wait (short, configurable)
    6.  Zone valve CLOSE  (drained – no stress on hose connections)
    7.  Inter-zone pause

Without a master valve configured, the sequencer just opens → waits →
closes the zone valve and falls back to the plain pause.

Design principles:
- ONE zone open at a time.
- Master valve is ONLY open while exactly one zone valve is also open.
- Every valve we open gets a deadman timer; every path out closes them.
- Cleanly cancellable via stop().
"""

from __future__ import annotations

import asyncio
import enum
import logging
from collections.abc import Callable
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from homeassistant.core import HomeAssistant

from .const import (
    DEFAULT_CLOSE_RETRY_MAX,
    DOMAIN,
    EVENT_MASTER_CLOSE_FAILED,
    EVENT_PROGRAM_ABORTED,
    EVENT_PROGRAM_COMPLETED,
    EVENT_PROGRAM_STARTED,
    EVENT_ZONE_COMPLETED,
    EVENT_ZONE_ERROR,
    EVENT_ZONE_STARTED,
)

from .zone import (
    entity_state_is_on,
    entity_svc_close,
    entity_svc_open,
    wait_for_entity_state,
)

if TYPE_CHECKING:
    from .safety import SafetyManager
    from .zone import Zone

_LOGGER = logging.getLogger(__name__)


class SequencerState(enum.Enum):
    """Possible states of the sequencer program."""

    IDLE = "idle"
    RUNNING = "running"


class Sequencer:
    """Runs configured zones sequentially with per-zone durations."""

    def __init__(
        self,
        hass: HomeAssistant,
        zones: list[Zone],
        safety: SafetyManager,
        pause_seconds: int = 30,
        master_valve_entity_id: str | None = None,
        depressurize_seconds: int = 5,
        on_complete: Callable[[], Any] | None = None,
    ) -> None:
        self._hass = hass
        self._zones = zones
        self._safety = safety
        self._pause_seconds = max(0, int(pause_seconds))
        self._master_valve = master_valve_entity_id or None
        self._depressurize_seconds = max(0, int(depressurize_seconds))
        self._on_complete = on_complete

        self._state = SequencerState.IDLE
        self._current_index: int = -1
        self._current_zone: Zone | None = None
        self._zone_started_at: datetime | None = None
        self._pause_started_at: datetime | None = None
        self._pause_duration_seconds: int = 0
        self._depressurize_started_at: datetime | None = None
        self._depressurize_duration_seconds: int = 0
        self._task: asyncio.Task[None] | None = None
        self._completed_zones: int = 0

    # -- Config accessors -----------------------------------------------

    @property
    def pause_seconds(self) -> int:
        return self._pause_seconds

    @pause_seconds.setter
    def pause_seconds(self, value: int) -> None:
        self._pause_seconds = max(0, int(value))

    @property
    def master_valve(self) -> str | None:
        return self._master_valve

    @property
    def depressurize_seconds(self) -> int:
        return self._depressurize_seconds

    @depressurize_seconds.setter
    def depressurize_seconds(self, value: int) -> None:
        self._depressurize_seconds = max(0, int(value))

    @property
    def zones(self) -> list[Zone]:
        """List of configured zones (read-only snapshot)."""
        return list(self._zones)

    # -- Runtime state --------------------------------------------------

    @property
    def state(self) -> SequencerState:
        return self._state

    @property
    def current_zone(self) -> Zone | None:
        return self._current_zone

    @property
    def current_zone_index(self) -> int:
        return self._current_index

    @property
    def total_zones(self) -> int:
        return len(self._zones)

    @property
    def next_zone(self) -> Zone | None:
        if self._current_index < 0:
            return None
        next_idx = self._current_index + 1
        if next_idx < len(self._zones):
            return self._zones[next_idx]
        return None

    @property
    def remaining_zone_seconds(self) -> int | None:
        if self._zone_started_at is None or self._current_index < 0:
            return None
        if self._current_index >= len(self._zones):
            return None
        duration_sec = self._zones[self._current_index].duration_seconds
        elapsed = (
            datetime.now(timezone.utc) - self._zone_started_at
        ).total_seconds()
        return max(0, int(round(duration_sec - elapsed)))

    @property
    def pause_remaining_seconds(self) -> int | None:
        """Seconds left in the current inter-zone pause, or None if not pausing."""
        if self._pause_started_at is None:
            return None
        elapsed = (
            datetime.now(timezone.utc) - self._pause_started_at
        ).total_seconds()
        return max(0, int(round(self._pause_duration_seconds - elapsed)))

    @property
    def depressurize_remaining_seconds(self) -> int | None:
        """Seconds left in the current depressurize wait, or None if not active."""
        if self._depressurize_started_at is None:
            return None
        elapsed = (
            datetime.now(timezone.utc) - self._depressurize_started_at
        ).total_seconds()
        return max(0, int(round(self._depressurize_duration_seconds - elapsed)))

    @property
    def _depressurize_active_seconds(self) -> int:
        """Per-zone depressurize wait that actually runs (0 without master)."""
        if self._master_valve and self._depressurize_seconds > 0:
            return self._depressurize_seconds
        return 0

    def _zone_block_seconds(self, zone_index: int) -> int:
        """Full predictable block of one zone: duration + depressurize + trailing pause."""
        if zone_index < 0 or zone_index >= len(self._zones):
            return 0
        block = self._zones[zone_index].duration_seconds
        block += self._depressurize_active_seconds
        if zone_index < len(self._zones) - 1:
            block += self._pause_seconds
        return block

    def _future_blocks_seconds(self, after_index: int) -> int:
        return sum(
            self._zone_block_seconds(j)
            for j in range(after_index + 1, len(self._zones))
        )

    @property
    def total_program_seconds_idle(self) -> int:
        """Total runtime of a full program when idle – zones + depressurize + gaps."""
        if not self._zones:
            return 0
        return sum(
            self._zone_block_seconds(i) for i in range(len(self._zones))
        )

    @property
    def total_remaining_seconds(self) -> int | None:
        """Estimated seconds left across the full program.

        Includes everything we know about: the current phase's remaining time,
        the depressurize wait that follows each zone (when a master valve is
        configured), and inter-zone pauses. Unknown zigbee command latencies
        between phases stay unaccounted for.
        """
        if self._state == SequencerState.IDLE:
            return self.total_program_seconds_idle

        if self._current_index < 0:
            return None

        idx = self._current_index
        is_last = idx >= len(self._zones) - 1
        future = self._future_blocks_seconds(idx)
        trailing_pause = 0 if is_last else self._pause_seconds

        if self._pause_started_at is not None:
            # Inter-zone pause: depressurize already happened.
            pause_left = self.pause_remaining_seconds or 0
            return int(pause_left + future)

        if self._depressurize_started_at is not None:
            # Master closed, draining the line.
            depress_left = self.depressurize_remaining_seconds or 0
            return int(depress_left + trailing_pause + future)

        # Zone is running (or in unknown-latency window between sleep and
        # the depressurize tracker being set – conservative estimate).
        current_remaining = self.remaining_zone_seconds or 0
        return int(
            current_remaining
            + self._depressurize_active_seconds
            + trailing_pause
            + future
        )

    @property
    def zones_total_remaining_seconds(self) -> int:
        """Sum of actual watering time still to come in the program.

        Idle: full runtime of every configured zone. Running: time left on
        the current zone plus the full duration of every later zone. Stays
        at 0 once the last zone finishes watering (depressurize / pause
        phases don't add to this).
        """
        if self._state == SequencerState.IDLE:
            return sum(z.duration_seconds for z in self._zones)

        if self._current_index < 0:
            return 0

        idx = self._current_index
        if idx >= len(self._zones):
            return 0

        future_zone_time = sum(
            z.duration_seconds for z in self._zones[idx + 1 :]
        )

        if (
            self._pause_started_at is not None
            or self._depressurize_started_at is not None
        ):
            # Current zone already finished watering; only future zones count.
            return int(future_zone_time)

        current_remaining = self.remaining_zone_seconds or 0
        return int(current_remaining + future_zone_time)

    @property
    def pauses_total_remaining_seconds(self) -> int:
        """Sum of every inter-zone pause still to come in the program."""
        n = len(self._zones)
        if n <= 1:
            return 0

        if self._state == SequencerState.IDLE:
            return (n - 1) * self._pause_seconds

        if self._current_index < 0:
            return 0

        idx = self._current_index

        if self._pause_started_at is not None:
            # Currently in the pause between idx and idx+1 – count it + any
            # further pauses after later zones.
            current_pause = self.pause_remaining_seconds or 0
            future_pause_count = max(0, n - 2 - idx)
            return int(current_pause + future_pause_count * self._pause_seconds)

        # Zone phase or depressurize phase at idx: the pause after the
        # current zone is still ahead, plus every later zone's pause.
        future_pause_count = max(0, n - 1 - idx)
        return future_pause_count * self._pause_seconds

    @property
    def depressurize_total_remaining_seconds(self) -> int:
        """Sum of every master-valve drain wait still to come in the program."""
        per_zone = self._depressurize_active_seconds
        if per_zone == 0 or not self._zones:
            return 0

        if self._state == SequencerState.IDLE:
            return len(self._zones) * per_zone

        if self._current_index < 0:
            return 0

        idx = self._current_index
        n = len(self._zones)

        if self._depressurize_started_at is not None:
            # Currently draining after zone idx – count it + every later zone.
            current_depress = self.depressurize_remaining_seconds or 0
            future_count = max(0, n - 1 - idx)
            return int(current_depress + future_count * per_zone)

        if self._pause_started_at is not None:
            # Drain for zone idx already finished; only later zones' drains.
            future_count = max(0, n - 1 - idx)
            return future_count * per_zone

        # Zone phase at idx: drain for current zone + every later zone.
        future_count = max(0, n - idx)
        return future_count * per_zone

    @property
    def phase(self) -> str:
        if self._pause_started_at is not None:
            return "pausing"
        if self._depressurize_started_at is not None:
            return "depressurizing"
        return self._state.value

    @property
    def progress(self) -> dict[str, Any]:
        return {
            "state": self._state.value,
            "phase": self.phase,
            "current_zone": self._current_zone.name if self._current_zone else None,
            "current_zone_entity_id": (
                self._current_zone.valve_entity_id
                if self._current_zone
                else None
            ),
            "current_zone_index": self._current_index,
            "total_zones": len(self._zones),
            "next_zone": self.next_zone.name if self.next_zone else None,
            "remaining_zone_seconds": self.remaining_zone_seconds,
            "pause_remaining_seconds": self.pause_remaining_seconds,
            "depressurize_remaining_seconds": self.depressurize_remaining_seconds,
            "total_remaining_seconds": self.total_remaining_seconds,
            "zones_total_remaining_seconds": self.zones_total_remaining_seconds,
            "pauses_total_remaining_seconds": self.pauses_total_remaining_seconds,
            "depressurize_total_remaining_seconds": (
                self.depressurize_total_remaining_seconds
            ),
            "pause_seconds": self._pause_seconds,
            "master_valve": self._master_valve,
            "depressurize_seconds": self._depressurize_seconds,
            "zones": [
                {
                    "name": z.name,
                    "valve_entity_id": z.valve_entity_id,
                    "duration_minutes": z.duration_minutes,
                    "duration_seconds": z.duration_seconds,
                }
                for z in self._zones
            ],
        }

    # -- Control --------------------------------------------------------

    async def start(self) -> None:
        """Start the sequencer program. No-op if already running."""
        if self._state == SequencerState.RUNNING:
            _LOGGER.warning("Sequencer: start() called while already running")
            return

        if not self._zones:
            _LOGGER.warning("Sequencer: no zones configured, nothing to run")
            return

        _LOGGER.info(
            "Sequencer: starting program with %d zone(s)%s",
            len(self._zones),
            f" (master valve {self._master_valve})" if self._master_valve else "",
        )
        self._state = SequencerState.RUNNING
        self._completed_zones = 0
        self._hass.bus.async_fire(
            EVENT_PROGRAM_STARTED,
            {
                "total_zones": len(self._zones),
                "zones": [z.name for z in self._zones],
                "total_duration_seconds": self.total_program_seconds_idle,
            },
        )
        self._task = self._hass.async_create_task(
            self._run(), "irrigation_proxy_sequencer"
        )

    async def stop(self, *, skip_depressurize: bool = False) -> None:
        """Stop the sequencer program and close any open valves.

        If a zone was actively being watered at the moment of the stop and
        a master valve with a non-zero depressurize delay is configured,
        the master is closed first and the zone valve only follows after
        the drain wait – same safety sequence as a normal zone completion.

        ``skip_depressurize=True`` bypasses the drain wait unconditionally
        (used by the leak-emergency path, where every second of flow
        matters more than protecting hose fittings).
        """
        if self._state == SequencerState.IDLE:
            return

        _LOGGER.info("Sequencer: stopping program")

        # Capture before _reset() clears them.
        zone_to_close = self._current_zone
        zone_index = self._current_index
        # "Watering" means water is currently flowing through this zone –
        # master open, zone open. The main-loop sets _zone_started_at right
        # before the duration sleep and clears it right after, so this is
        # the precise window in which a depressurize makes sense.
        was_watering = (
            zone_to_close is not None and self._zone_started_at is not None
        )

        # Cancel the task
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

        depressurize_ran = False
        if (
            was_watering
            and not skip_depressurize
            and self._master_valve
            and self._depressurize_seconds > 0
        ):
            # Close master first (stops flow), then wait the configured
            # drain time before we close the zone valve – identical to
            # the normal per-zone completion path.
            await self._close_master()
            self._depressurize_started_at = datetime.now(timezone.utc)
            self._depressurize_duration_seconds = self._depressurize_seconds
            try:
                await asyncio.sleep(self._depressurize_seconds)
                depressurize_ran = True
            except asyncio.CancelledError:
                # HA shutdown / another stop() came in – fall through and
                # still close the zone so we never leave it open.
                pass
            finally:
                self._depressurize_started_at = None
                self._depressurize_duration_seconds = 0
        else:
            # No watering phase (or caller asked to skip): close master
            # immediately, same as the pre-0.8 behaviour.
            await self._close_master()

        if zone_to_close is not None:
            try:
                await zone_to_close.turn_off(self._hass)
            except Exception:
                _LOGGER.exception(
                    "Sequencer: failed to close zone '%s' during stop",
                    zone_to_close.name,
                )
            self._safety.cancel_deadman(zone_to_close.valve_entity_id)

        self._reset()

        # Fire ABORTED after valves are closed and state is reset so
        # consumers see the sequencer already in IDLE with closed valves.
        self._hass.bus.async_fire(
            EVENT_PROGRAM_ABORTED,
            {
                "reason": "stopped",
                "zone_name": zone_to_close.name if zone_to_close else None,
                "zone_index": zone_index,
                "depressurized": depressurize_ran,
            },
        )

    # -- Internal -------------------------------------------------------

    async def _run(self) -> None:
        """Main sequencer loop – runs each zone for its configured duration."""
        try:
            for i, zone in enumerate(self._zones):
                self._current_index = i
                self._current_zone = zone
                # _zone_started_at is set later, right before the actual
                # duration sleep – so the user-visible countdown starts
                # exactly when water flows, not during Zigbee verify latency.
                self._zone_started_at = None

                _LOGGER.info(
                    "Sequencer: starting zone %d/%d '%s' for %ds",
                    i + 1,
                    len(self._zones),
                    zone.name,
                    zone.duration_seconds,
                )

                # Arm the deadman BEFORE issuing the open command. Zigbee
                # end-devices can ACK an open several seconds after our
                # 5 s verify window times out – without a pre-armed deadman
                # the valve would be an orphan until the 30 s coordinator
                # poll catches it.
                self._safety.start_deadman(zone)

                # 1. Open zone (still no water – master is closed)
                zone_ok = await zone.turn_on(self._hass)
                if not zone_ok:
                    _LOGGER.error(
                        "Sequencer: zone '%s' did not verify open – "
                        "force-closing defensively in case of Zigbee latency",
                        zone.name,
                    )
                    # Defensive close: force_close's own retry/poll loop
                    # catches a valve that opens just after the verify
                    # window. If the valve never opened, this is a no-op.
                    try:
                        await zone.force_close(self._hass)
                    except Exception:
                        _LOGGER.exception(
                            "Sequencer: force_close after failed open raised "
                            "for zone '%s'", zone.name,
                        )
                    self._safety.cancel_deadman(zone.valve_entity_id)
                    self._hass.bus.async_fire(
                        EVENT_ZONE_ERROR,
                        {
                            "zone_name": zone.name,
                            "zone_index": i,
                            "valve_entity_id": zone.valve_entity_id,
                            "reason": "failed_to_open",
                        },
                    )
                    continue

                self._hass.bus.async_fire(
                    EVENT_ZONE_STARTED,
                    {
                        "zone_name": zone.name,
                        "zone_index": i,
                        "valve_entity_id": zone.valve_entity_id,
                        "duration_seconds": zone.duration_seconds,
                    },
                )

                # 2. Open master – water starts flowing.
                master_opened = await self._open_master()
                if master_opened is False:
                    _LOGGER.error(
                        "Sequencer: master valve failed to open – aborting zone '%s'",
                        zone.name,
                    )
                    await zone.turn_off(self._hass)
                    self._safety.cancel_deadman(zone.valve_entity_id)
                    continue

                # 3. Wait the configured duration. Start the user-visible
                # zone countdown exactly here so it aligns with the actual
                # water-flow window (not with the open/verify latency above).
                self._zone_started_at = datetime.now(timezone.utc)
                try:
                    await asyncio.sleep(zone.duration_seconds)
                finally:
                    # Clear so remaining_zone_seconds returns None during
                    # the depressurize phase – the user sees the dedicated
                    # depressurize countdown instead of a stuck "0".
                    self._zone_started_at = None

                # 4. Close master (stop flow)
                await self._close_master()

                # 5. Depressurize wait
                if self._master_valve and self._depressurize_seconds > 0:
                    self._depressurize_started_at = datetime.now(timezone.utc)
                    self._depressurize_duration_seconds = self._depressurize_seconds
                    try:
                        await asyncio.sleep(self._depressurize_seconds)
                    finally:
                        self._depressurize_started_at = None
                        self._depressurize_duration_seconds = 0

                # 6. Close zone valve (drained)
                await zone.turn_off(self._hass)
                self._safety.cancel_deadman(zone.valve_entity_id)
                self._completed_zones += 1

                _LOGGER.info("Sequencer: zone '%s' completed", zone.name)
                self._hass.bus.async_fire(
                    EVENT_ZONE_COMPLETED,
                    {
                        "zone_name": zone.name,
                        "zone_index": i,
                        "valve_entity_id": zone.valve_entity_id,
                    },
                )

                # 7. Pause between zones (not after last)
                if i < len(self._zones) - 1 and self._pause_seconds > 0:
                    _LOGGER.debug(
                        "Sequencer: pausing %ds before next zone",
                        self._pause_seconds,
                    )
                    self._current_zone = None
                    self._zone_started_at = None
                    self._pause_started_at = datetime.now(timezone.utc)
                    self._pause_duration_seconds = self._pause_seconds
                    try:
                        await asyncio.sleep(self._pause_seconds)
                    finally:
                        self._pause_started_at = None
                        self._pause_duration_seconds = 0

            total = len(self._zones)
            completed = self._completed_zones
            skipped = total - completed
            if completed == 0 and total > 0:
                _LOGGER.warning(
                    "Sequencer: program ended with 0/%d zones completed "
                    "(all skipped) – firing aborted", total,
                )
                self._hass.bus.async_fire(
                    EVENT_PROGRAM_ABORTED,
                    {
                        "reason": "all_zones_skipped",
                        "zones_completed": 0,
                        "zones_skipped": skipped,
                        "total_zones": total,
                    },
                )
            else:
                _LOGGER.info(
                    "Sequencer: program completed (%d/%d zones, %d skipped)",
                    completed, total, skipped,
                )
                self._hass.bus.async_fire(
                    EVENT_PROGRAM_COMPLETED,
                    {
                        "zones_completed": completed,
                        "zones_skipped": skipped,
                        "total_zones": total,
                    },
                )

        except asyncio.CancelledError:
            _LOGGER.info("Sequencer: program was cancelled")
            return

        except Exception:
            _LOGGER.exception("Sequencer: unexpected error during program")
            self._hass.bus.async_fire(
                EVENT_PROGRAM_ABORTED,
                {
                    "reason": "error",
                    "zone_name": (
                        self._current_zone.name if self._current_zone else None
                    ),
                    "zone_index": self._current_index,
                },
            )
            await self._close_master()
            if self._current_zone is not None:
                try:
                    await self._current_zone.turn_off(self._hass)
                    self._safety.cancel_deadman(
                        self._current_zone.valve_entity_id
                    )
                except Exception:
                    _LOGGER.exception("Sequencer: cleanup also failed")

        # Cleanup after normal completion or error (not after cancel)
        self._reset()
        if self._on_complete is not None:
            self._on_complete()

    # -- Master valve helpers -------------------------------------------

    async def _open_master(self) -> bool | None:
        """Open the master valve, verify state. Returns None if no master."""
        if not self._master_valve:
            return None

        _LOGGER.info("Sequencer: opening master valve %s", self._master_valve)
        svc_domain, svc_action = entity_svc_open(self._master_valve)
        await self._hass.services.async_call(
            svc_domain,
            svc_action,
            {"entity_id": self._master_valve},
            blocking=True,
        )
        await wait_for_entity_state(
            self._hass, self._master_valve, expected_on=True
        )
        state = self._hass.states.get(self._master_valve)
        actual = state.state if state is not None else "unavailable"
        if not entity_state_is_on(actual):
            _LOGGER.warning(
                "Sequencer: master valve did not open after %s (state=%s)",
                svc_action,
                actual,
            )
            return False
        return True

    async def _close_master(self) -> bool:
        """Close the master valve (best-effort, with retries).

        Returns True when the valve is confirmed closed (or no master is
        configured). Returns False when every retry attempt was exhausted
        without the valve reporting `off`/`closed`. On failure, a persistent
        notification and `EVENT_MASTER_CLOSE_FAILED` are raised so the user
        can intervene — otherwise the master would silently stay open.
        """
        if not self._master_valve:
            return True

        _LOGGER.info("Sequencer: closing master valve %s", self._master_valve)
        svc_domain, svc_action = entity_svc_close(self._master_valve)
        last_state: str = "unknown"
        for attempt in range(1, DEFAULT_CLOSE_RETRY_MAX + 1):
            try:
                await self._hass.services.async_call(
                    svc_domain,
                    svc_action,
                    {"entity_id": self._master_valve},
                    blocking=True,
                )
            except Exception:
                _LOGGER.exception(
                    "Sequencer: failed to call %s on master valve (attempt %d)",
                    svc_action,
                    attempt,
                )
            await wait_for_entity_state(
                self._hass, self._master_valve, expected_on=False
            )
            state = self._hass.states.get(self._master_valve)
            last_state = state.state if state is not None else "unavailable"
            if not entity_state_is_on(last_state):
                return True
            _LOGGER.warning(
                "Sequencer: master valve still open after attempt %d – retrying",
                attempt,
            )

        _LOGGER.error(
            "Sequencer: master valve %s did NOT close after %d attempts "
            "(last_state=%s) – raising notification",
            self._master_valve,
            DEFAULT_CLOSE_RETRY_MAX,
            last_state,
        )
        self._notify_master_close_failed(last_state)
        return False

    def _notify_master_close_failed(self, last_state: str) -> None:
        """Fire a bus event and raise a persistent notification.

        W1: without this, a stuck-open master valve leaves water flowing
        indefinitely with no user-visible signal.
        """
        master_id = self._master_valve or "unknown"
        try:
            self._hass.bus.async_fire(
                EVENT_MASTER_CLOSE_FAILED,
                {
                    "master_entity_id": master_id,
                    "attempts": DEFAULT_CLOSE_RETRY_MAX,
                    "last_state": last_state,
                },
            )
        except Exception:  # noqa: BLE001 – defensive
            _LOGGER.exception(
                "Sequencer: failed to fire master_close_failed event"
            )

        try:
            from homeassistant.components import persistent_notification
        except Exception:  # noqa: BLE001
            _LOGGER.debug(
                "Sequencer: persistent_notification unavailable"
            )
            return

        title = "Irrigation Proxy: master valve stuck open"
        message = (
            f"Master valve `{master_id}` did not close after "
            f"{DEFAULT_CLOSE_RETRY_MAX} attempts (last state: `{last_state}`). "
            "Water may still be flowing. Close the valve manually and "
            "investigate the Zigbee link or power supply before starting "
            "another program."
        )
        notif_id = f"{DOMAIN}_master_close_failed_{master_id}"
        try:
            persistent_notification.async_create(
                self._hass,
                message,
                title=title,
                notification_id=notif_id,
            )
        except Exception:
            _LOGGER.exception(
                "Sequencer: failed to raise master-close persistent notification"
            )

    def _reset(self) -> None:
        """Reset sequencer to idle state."""
        self._state = SequencerState.IDLE
        self._current_index = -1
        self._current_zone = None
        self._zone_started_at = None
        self._pause_started_at = None
        self._pause_duration_seconds = 0
        self._depressurize_started_at = None
        self._depressurize_duration_seconds = 0
        self._task = None
        # Don't reset _completed_zones – on_complete may still inspect it.
