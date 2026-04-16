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
    DEFAULT_STATE_VERIFY_DELAY_SECONDS,
    EVENT_PROGRAM_ABORTED,
    EVENT_PROGRAM_COMPLETED,
    EVENT_PROGRAM_STARTED,
    EVENT_ZONE_COMPLETED,
    EVENT_ZONE_ERROR,
    EVENT_ZONE_STARTED,
)

from .zone import entity_state_is_on, entity_svc_close, entity_svc_open

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
        self._task: asyncio.Task[None] | None = None

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
    def total_program_seconds_idle(self) -> int:
        """Total runtime of a full program when idle – zones + gaps."""
        if not self._zones:
            return 0
        zone_sum = sum(z.duration_seconds for z in self._zones)
        gaps = max(0, len(self._zones) - 1) * self._pause_seconds
        return int(zone_sum + gaps)

    @property
    def total_remaining_seconds(self) -> int | None:
        """Estimated seconds left across the full program."""
        if self._state == SequencerState.IDLE:
            return self.total_program_seconds_idle

        if self._current_index < 0:
            return None

        current_remaining = self.remaining_zone_seconds or 0
        pending_zones = self._zones[self._current_index + 1 :]
        pending_seconds = sum(z.duration_seconds for z in pending_zones)
        gaps = len(pending_zones) * self._pause_seconds
        return int(current_remaining + pending_seconds + gaps)

    @property
    def progress(self) -> dict[str, Any]:
        return {
            "state": self._state.value,
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
            "total_remaining_seconds": self.total_remaining_seconds,
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

    async def stop(self) -> None:
        """Stop the sequencer program and close any open valves."""
        if self._state == SequencerState.IDLE:
            return

        _LOGGER.info("Sequencer: stopping program")

        zone_to_close = self._current_zone
        self._hass.bus.async_fire(
            EVENT_PROGRAM_ABORTED,
            {
                "reason": "stopped",
                "zone_name": zone_to_close.name if zone_to_close else None,
                "zone_index": self._current_index,
            },
        )

        # Cancel the task
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

        # Safety: close master first (kills flow), then zone.
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

    # -- Internal -------------------------------------------------------

    async def _run(self) -> None:
        """Main sequencer loop – runs each zone for its configured duration."""
        try:
            for i, zone in enumerate(self._zones):
                self._current_index = i
                self._current_zone = zone
                self._zone_started_at = datetime.now(timezone.utc)

                _LOGGER.info(
                    "Sequencer: starting zone %d/%d '%s' for %ds",
                    i + 1,
                    len(self._zones),
                    zone.name,
                    zone.duration_seconds,
                )

                # 1. Open zone (still no water – master is closed)
                zone_ok = await zone.turn_on(self._hass)
                if not zone_ok:
                    _LOGGER.error(
                        "Sequencer: zone '%s' failed to open – skipping",
                        zone.name,
                    )
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

                # Deadman on the zone valve itself.
                self._safety.start_deadman(zone)

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

                # 3. Wait the configured duration
                await asyncio.sleep(zone.duration_seconds)

                # 4. Close master (stop flow)
                await self._close_master()

                # 5. Depressurize wait
                if self._master_valve and self._depressurize_seconds > 0:
                    await asyncio.sleep(self._depressurize_seconds)

                # 6. Close zone valve (drained)
                await zone.turn_off(self._hass)
                self._safety.cancel_deadman(zone.valve_entity_id)

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
                    await asyncio.sleep(self._pause_seconds)

            _LOGGER.info("Sequencer: program completed successfully")
            self._hass.bus.async_fire(
                EVENT_PROGRAM_COMPLETED,
                {
                    "zones_completed": len(self._zones),
                    "total_zones": len(self._zones),
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
        await asyncio.sleep(DEFAULT_STATE_VERIFY_DELAY_SECONDS)
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

    async def _close_master(self) -> None:
        """Close the master valve (best-effort, with retries)."""
        if not self._master_valve:
            return

        _LOGGER.info("Sequencer: closing master valve %s", self._master_valve)
        svc_domain, svc_action = entity_svc_close(self._master_valve)
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
            await asyncio.sleep(DEFAULT_STATE_VERIFY_DELAY_SECONDS)
            state = self._hass.states.get(self._master_valve)
            actual = state.state if state is not None else "unavailable"
            if not entity_state_is_on(actual):
                return
            _LOGGER.warning(
                "Sequencer: master valve still open after attempt %d – retrying",
                attempt,
            )

        _LOGGER.error(
            "Sequencer: master valve %s did NOT close after %d attempts",
            self._master_valve,
            DEFAULT_CLOSE_RETRY_MAX,
        )

    def _reset(self) -> None:
        """Reset sequencer to idle state."""
        self._state = SequencerState.IDLE
        self._current_index = -1
        self._current_zone = None
        self._zone_started_at = None
        self._task = None
