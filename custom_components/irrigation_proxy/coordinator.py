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
    from .weather import WeatherProvider

_LOGGER = logging.getLogger(__name__)


class IrrigationCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Central coordinator that polls valve states, weather, and safety checks."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        zones: dict[str, Zone],
        safety: SafetyManager,
        sequencer: Sequencer,
        weather: WeatherProvider | None = None,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=DEFAULT_UPDATE_INTERVAL_SECONDS),
        )
        self.entry = entry
        self.zones = zones
        self.safety = safety
        self.sequencer = sequencer
        self.weather = weather
        self.scheduler: ProgramScheduler | None = None

        self._tick_unsub: Callable[[], None] | None = None
        self._state_change_unsub: Callable[[], None] | None = None

    # -- Public lifecycle ------------------------------------------------

    def set_scheduler(self, scheduler: ProgramScheduler) -> None:
        """Attach a scheduler so sensors can surface next-start info."""
        self.scheduler = scheduler

    def start_state_tracking(self) -> None:
        """Subscribe to underlying valve state changes.

        Ensures zone switch entities update within ~1 s when the real
        valve flips, instead of waiting for the 30 s coordinator cycle.
        """
        if self._state_change_unsub is not None:
            return
        if not self.zones:
            return
        self._state_change_unsub = async_track_state_change_event(
            self.hass,
            list(self.zones.keys()),
            self._on_valve_state_change,
        )

    def stop_state_tracking(self) -> None:
        """Cancel the state-change subscription (on unload)."""
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
        if valve_id not in self.zones:
            return

        zone = self.zones[valve_id]
        state_str = new_state.state if new_state is not None else "unavailable"
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

    # -- 1 Hz tick while running -----------------------------------------

    def notify_sequencer_state_changed(self) -> None:
        """Call whenever the sequencer transitions idle ↔ running.

        Starts/stops a 1 Hz ticker that refreshes the timer sensors
        without polling any external system.
        """
        if self.sequencer.state == SequencerState.RUNNING:
            self._start_tick()
        else:
            self._stop_tick()
            # One final refresh so sensors flip cleanly to idle values
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
        """Fire every 1 s while the sequencer is running – cheap in-memory."""
        if self.sequencer.state != SequencerState.RUNNING:
            self._stop_tick()
            self._refresh_timer_snapshot()
            return
        self._refresh_timer_snapshot()

    def _refresh_timer_snapshot(self) -> None:
        """Rebuild just the sequencer / per-zone snapshots and push."""
        if self.data is None:
            return
        data = dict(self.data)
        data["sequencer"] = self.sequencer.progress

        # Keep per-zone dicts in sync for the duration_minutes value – no IO.
        for valve_id, zone in self.zones.items():
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

    # -- Regular 30 s poll ----------------------------------------------

    async def _async_update_data(self) -> dict[str, Any]:
        """Poll valve states, weather, run safety checks, return status dict."""
        data: dict[str, Any] = {}

        for valve_id, zone in self.zones.items():
            state = self.hass.states.get(valve_id)
            if state is not None:
                zone.update_state(state.state)
            else:
                _LOGGER.debug(
                    "Coordinator: entity %s unavailable during poll", valve_id
                )

            # Orphan detection: zone is on but has no deadman timer
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

        # Backup safety check for overruns
        await self.safety.check_overruns(list(self.zones.values()))

        # Sequencer progress snapshot
        data["sequencer"] = self.sequencer.progress

        # Scheduler snapshot (next start, last skip reason, etc.)
        if self.scheduler is not None:
            next_fire = self.scheduler.next_fire_time
            data["scheduler"] = {
                "next_fire": next_fire.isoformat() if next_fire else None,
                "last_fire": (
                    self.scheduler.last_fire.isoformat()
                    if self.scheduler.last_fire
                    else None
                ),
                "last_multiplier": self.scheduler.last_multiplier,
                "last_skip_reason": self.scheduler.last_skip_reason,
            }
        else:
            data["scheduler"] = {
                "next_fire": None,
                "last_fire": None,
                "last_multiplier": 1.0,
                "last_skip_reason": None,
            }

        # Weather data (rate-limited internally, safe to call every poll)
        if self.weather is not None:
            weather_data = await self.weather.async_update()
            data["weather"] = {
                "et0_today": weather_data.et0_today,
                "precipitation_last_24h": weather_data.precipitation_last_24h,
                "precipitation_forecast_24h": weather_data.precipitation_forecast_24h,
                "temperature_max": weather_data.temperature_max,
                "water_need_factor": weather_data.water_need_factor,
                "rain_skip": weather_data.rain_skip,
                "rain_threshold_mm": self.weather.rain_threshold_mm,
                "last_update": (
                    weather_data.last_update.isoformat()
                    if weather_data.last_update
                    else None
                ),
                "last_error": weather_data.last_error,
            }

        # Keep the 1 Hz ticker in sync with sequencer state
        if self.sequencer.state == SequencerState.RUNNING:
            self._start_tick()
        else:
            self._stop_tick()

        return data
