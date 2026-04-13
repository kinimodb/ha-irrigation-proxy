"""Sequencer – runs irrigation zones in sequence, one at a time."""

from __future__ import annotations

import asyncio
import enum
import logging
from collections.abc import Callable
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from homeassistant.core import HomeAssistant

if TYPE_CHECKING:
    from .safety import SafetyManager
    from .zone import Zone

_LOGGER = logging.getLogger(__name__)


class SequencerState(enum.Enum):
    """Possible states of the sequencer program."""

    IDLE = "idle"
    RUNNING = "running"


class Sequencer:
    """Runs configured zones sequentially with per-zone durations.

    Design principles:
    - ONE zone open at a time (safety first)
    - Integrates with SafetyManager (deadman timer per zone)
    - Cleanly cancellable via stop()
    - Reports progress for UI sensors
    """

    def __init__(
        self,
        hass: HomeAssistant,
        zones: list[Zone],
        safety: SafetyManager,
        pause_seconds: int = 30,
        on_complete: Callable[[], Any] | None = None,
    ) -> None:
        self._hass = hass
        self._zones = zones
        self._safety = safety
        self._pause_seconds = pause_seconds
        self._on_complete = on_complete

        self._state = SequencerState.IDLE
        self._current_index: int = -1
        self._current_zone: Zone | None = None
        self._zone_started_at: datetime | None = None
        self._task: asyncio.Task[None] | None = None

    # -- Properties for UI / Coordinator ---------------------------------

    @property
    def state(self) -> SequencerState:
        """Current sequencer state."""
        return self._state

    @property
    def current_zone(self) -> Zone | None:
        """Zone currently being irrigated, or None."""
        return self._current_zone

    @property
    def current_zone_index(self) -> int:
        """0-based index of the current zone, -1 if idle."""
        return self._current_index

    @property
    def total_zones(self) -> int:
        """Total number of zones in the program."""
        return len(self._zones)

    @property
    def next_zone(self) -> Zone | None:
        """Next zone in the queue, or None if last/idle."""
        if self._current_index < 0:
            return None
        next_idx = self._current_index + 1
        if next_idx < len(self._zones):
            return self._zones[next_idx]
        return None

    @property
    def remaining_zone_seconds(self) -> int | None:
        """Seconds remaining on the current zone, or None if idle."""
        if self._zone_started_at is None or self._current_index < 0:
            return None
        if self._current_index >= len(self._zones):
            return None
        zone = self._zones[self._current_index]
        duration_sec = zone.duration_minutes * 60
        elapsed = (datetime.now(timezone.utc) - self._zone_started_at).total_seconds()
        return max(0, int(duration_sec - elapsed))

    @property
    def progress(self) -> dict[str, Any]:
        """Return a snapshot of sequencer progress for the coordinator."""
        return {
            "state": self._state.value,
            "current_zone": self._current_zone.name if self._current_zone else None,
            "current_zone_entity_id": (
                self._current_zone.valve_entity_id if self._current_zone else None
            ),
            "current_zone_index": self._current_index,
            "total_zones": len(self._zones),
            "next_zone": self.next_zone.name if self.next_zone else None,
            "remaining_zone_seconds": self.remaining_zone_seconds,
        }

    # -- Control ---------------------------------------------------------

    async def start(self) -> None:
        """Start the sequencer program.

        Runs all zones in order. No-op if already running.
        """
        if self._state == SequencerState.RUNNING:
            _LOGGER.warning("Sequencer: start() called while already running")
            return

        if not self._zones:
            _LOGGER.warning("Sequencer: no zones configured, nothing to run")
            return

        _LOGGER.info(
            "Sequencer: starting program with %d zones", len(self._zones)
        )
        self._state = SequencerState.RUNNING
        self._task = self._hass.async_create_task(
            self._run(), "irrigation_proxy_sequencer"
        )

    async def stop(self) -> None:
        """Stop the sequencer program.

        Cancels the running task, closes the active zone, resets state.
        """
        if self._state == SequencerState.IDLE:
            return

        _LOGGER.info("Sequencer: stopping program")

        # Grab reference before task cancellation resets it
        zone_to_close = self._current_zone
        valve_id = zone_to_close.valve_entity_id if zone_to_close else None

        # Cancel the task
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

        # Safety: close the zone that was active when stop() was called
        if zone_to_close is not None:
            try:
                await zone_to_close.turn_off(self._hass)
            except Exception:
                _LOGGER.exception(
                    "Sequencer: failed to close zone '%s' during stop",
                    zone_to_close.name,
                )
            if valve_id:
                self._safety.cancel_deadman(valve_id)

        self._reset()

    # -- Internal --------------------------------------------------------

    async def _run(self) -> None:
        """Main sequencer loop – runs each zone for its configured duration."""
        try:
            for i, zone in enumerate(self._zones):
                self._current_index = i
                self._current_zone = zone
                self._zone_started_at = datetime.now(timezone.utc)

                _LOGGER.info(
                    "Sequencer: starting zone %d/%d '%s' for %d min",
                    i + 1,
                    len(self._zones),
                    zone.name,
                    zone.duration_minutes,
                )

                # Open the valve
                ok = await zone.turn_on(self._hass)
                if not ok:
                    _LOGGER.error(
                        "Sequencer: zone '%s' failed to open – skipping",
                        zone.name,
                    )
                    continue

                # Deadman-Timer für diese Zone
                self._safety.start_deadman(zone)

                # Warten bis Dauer abgelaufen
                await asyncio.sleep(zone.duration_minutes * 60)

                # Ventil schließen
                await zone.turn_off(self._hass)
                self._safety.cancel_deadman(zone.valve_entity_id)

                _LOGGER.info("Sequencer: zone '%s' completed", zone.name)

                # Pause zwischen Zonen (nicht nach der letzten)
                if i < len(self._zones) - 1 and self._pause_seconds > 0:
                    _LOGGER.debug(
                        "Sequencer: pausing %ds before next zone",
                        self._pause_seconds,
                    )
                    self._current_zone = None
                    self._zone_started_at = None
                    await asyncio.sleep(self._pause_seconds)

            _LOGGER.info("Sequencer: program completed successfully")

        except asyncio.CancelledError:
            # stop() handles zone cleanup – don't touch state here
            _LOGGER.info("Sequencer: program was cancelled")
            return

        except Exception:
            _LOGGER.exception("Sequencer: unexpected error during program")
            # Versuche aktive Zone zu schließen
            if self._current_zone is not None:
                try:
                    await self._current_zone.turn_off(self._hass)
                    self._safety.cancel_deadman(
                        self._current_zone.valve_entity_id
                    )
                except Exception:
                    _LOGGER.exception("Sequencer: cleanup also failed")

        # Aufräumen nach normalem Ende oder Fehler (nicht nach Cancel)
        self._reset()
        if self._on_complete is not None:
            self._on_complete()

    def _reset(self) -> None:
        """Reset sequencer to idle state."""
        self._state = SequencerState.IDLE
        self._current_index = -1
        self._current_zone = None
        self._zone_started_at = None
        self._task = None
