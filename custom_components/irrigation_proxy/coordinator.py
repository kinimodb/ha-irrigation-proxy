"""DataUpdateCoordinator for Irrigation Proxy."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_ON
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import (
    DEFAULT_UPDATE_INTERVAL_SECONDS,
    DOMAIN,
    TIMER_TICK_INTERVAL_SECONDS,
)
from .safety import SafetyManager
from .sequencer import Sequencer, SequencerState
from .zone import Zone

if TYPE_CHECKING:
    from .scheduler import ProgramScheduler

_LOGGER = logging.getLogger(__name__)


class IrrigationCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Central coordinator that polls valve states and sequencer progress."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        zones: list[Zone],
        safety: SafetyManager,
        sequencer: Sequencer,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=DEFAULT_UPDATE_INTERVAL_SECONDS),
        )
        self.entry = entry
        self.zones: list[Zone] = list(zones)
        self.safety = safety
        self.sequencer = sequencer
        self.scheduler: ProgramScheduler | None = None

        self._tick_unsub: Callable[[], None] | None = None
        self._state_change_unsub: Callable[[], None] | None = None

    # -- Lookup helpers -------------------------------------------------

    @property
    def zones_by_valve(self) -> dict[str, Zone]:
        return {z.valve_entity_id: z for z in self.zones}

    # -- Public lifecycle -----------------------------------------------

    def set_scheduler(self, scheduler: ProgramScheduler) -> None:
        self.scheduler = scheduler

    def start_state_tracking(self) -> None:
        """Subscribe to underlying valve state changes."""
        if self._state_change_unsub is not None:
            return
        if not self.zones:
            return
        tracked = [z.valve_entity_id for z in self.zones]
        if self.sequencer.master_valve:
            tracked.append(self.sequencer.master_valve)
        self._state_change_unsub = async_track_state_change_event(
            self.hass,
            tracked,
            self._on_valve_state_change,
        )

    def stop_state_tracking(self) -> None:
        if self._state_change_unsub is not None:
            try:
                self._state_change_unsub()
            except Exception:  # noqa: BLE001
                _LOGGER.debug("Coordinator: state tracking unsubscribe failed")
            self._state_change_unsub = None
        self._stop_tick()

    @callback
    def _on_valve_state_change(self, event: Event) -> None:
        """Push valve state changes into coordinator data immediately."""
        valve_id = event.data.get("entity_id")
        new_state = event.data.get("new_state")
        state_str = new_state.state if new_state is not None else "unavailable"

        zones_by_valve = self.zones_by_valve
        if valve_id not in zones_by_valve:
            # Likely the master valve – just trigger a refresh snapshot.
            self._refresh_timer_snapshot()
            return

        zone = zones_by_valve[valve_id]
        zone.update_state(state_str)

        data = dict(self.data or {})
        entry = dict(data.get(valve_id, {}))
        entry.update(
            {
                "is_on": state_str == STATE_ON,
                "name": zone.name,
                "valve_entity_id": valve_id,
                "expected_state": zone.expected_state,
                "state_mismatch": zone.state_mismatch,
                "remaining_seconds": self.safety.remaining_seconds(valve_id),
                "duration_minutes": zone.duration_minutes,
            }
        )
        data[valve_id] = entry
        self.async_set_updated_data(data)

    # -- 1 Hz tick while running ----------------------------------------

    def notify_sequencer_state_changed(self) -> None:
        if self.sequencer.state == SequencerState.RUNNING:
            self._start_tick()
        else:
            self._stop_tick()
            self._refresh_timer_snapshot()

    def _start_tick(self) -> None:
        if self._tick_unsub is not None:
            return
        self._tick_unsub = async_track_time_interval(
            self.hass,
            self._on_tick,
            timedelta(seconds=TIMER_TICK_INTERVAL_SECONDS),
        )

    def _stop_tick(self) -> None:
        if self._tick_unsub is not None:
            try:
                self._tick_unsub()
            except Exception:  # noqa: BLE001
                _LOGGER.debug("Coordinator: tick unsubscribe failed")
            self._tick_unsub = None

    @callback
    def _on_tick(self, _now: datetime) -> None:
        if self.sequencer.state != SequencerState.RUNNING:
            self._stop_tick()
            self._refresh_timer_snapshot()
            return
        self._refresh_timer_snapshot()

    def _refresh_timer_snapshot(self) -> None:
        if self.data is None:
            return
        data = dict(self.data)
        data["sequencer"] = self.sequencer.progress

        for zone in self.zones:
            valve_id = zone.valve_entity_id
            entry = dict(data.get(valve_id, {}))
            entry.setdefault("name", zone.name)
            entry.setdefault("valve_entity_id", valve_id)
            entry["duration_minutes"] = zone.duration_minutes
            entry["is_on"] = zone.is_on
            entry["state_mismatch"] = zone.state_mismatch
            entry["expected_state"] = zone.expected_state
            entry["remaining_seconds"] = self.safety.remaining_seconds(valve_id)
            data[valve_id] = entry

        self.async_set_updated_data(data)

    # -- Regular 30 s poll ---------------------------------------------

    async def _async_update_data(self) -> dict[str, Any]:
        """Poll valve states, run safety checks, return status dict."""
        data: dict[str, Any] = {}

        for zone in self.zones:
            valve_id = zone.valve_entity_id
            state = self.hass.states.get(valve_id)
            if state is not None:
                zone.update_state(state.state)
            else:
                _LOGGER.debug(
                    "Coordinator: entity %s unavailable during poll", valve_id
                )

            # Orphan detection
            if (
                zone.is_on
                and valve_id not in self.safety.zone_start_times
            ):
                _LOGGER.warning(
                    "Coordinator: zone '%s' is on with no deadman timer – forcing close",
                    zone.name,
                )
                await zone.force_close(self.hass)

            data[valve_id] = {
                "is_on": zone.is_on,
                "name": zone.name,
                "valve_entity_id": valve_id,
                "expected_state": zone.expected_state,
                "state_mismatch": zone.state_mismatch,
                "remaining_seconds": self.safety.remaining_seconds(valve_id),
                "duration_minutes": zone.duration_minutes,
            }

        await self.safety.check_overruns(self.zones)

        data["sequencer"] = self.sequencer.progress

        if self.scheduler is not None:
            next_fire = self.scheduler.next_fire_time
            data["scheduler"] = {
                "next_fire": next_fire.isoformat() if next_fire else None,
                "last_fire": (
                    self.scheduler.last_fire.isoformat()
                    if self.scheduler.last_fire
                    else None
                ),
            }
        else:
            data["scheduler"] = {"next_fire": None, "last_fire": None}

        if self.sequencer.state == SequencerState.RUNNING:
            self._start_tick()
        else:
            self._stop_tick()

        return data
