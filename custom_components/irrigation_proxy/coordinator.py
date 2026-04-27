"""DataUpdateCoordinator for Irrigation Proxy."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import (
    DEFAULT_UPDATE_INTERVAL_SECONDS,
    DEFAULT_WEATHER_FACTOR,
    DOMAIN,
    EVENT_LEAK_DETECTED,
    TIMER_TICK_INTERVAL_SECONDS,
    WEATHER_FACTOR_MAX,
    WEATHER_FACTOR_MIN,
)
from .safety import SafetyManager
from .sequencer import Sequencer, SequencerState
from .zone import Zone, entity_svc_close

if TYPE_CHECKING:
    from .scheduler import ProgramScheduler

_LOGGER = logging.getLogger(__name__)


def _parse_weather_factor(raw: Any) -> float:
    """Parse and clamp the weather sensor state into a runtime factor.

    Returns ``DEFAULT_WEATHER_FACTOR`` (1.0) for ``None``, ``unknown``,
    ``unavailable`` or non-numeric states. Valid floats are clamped to
    ``[WEATHER_FACTOR_MIN, WEATHER_FACTOR_MAX]`` so a misconfigured sensor
    cannot push runtimes beyond the configured safety bounds.
    """
    if raw is None:
        return DEFAULT_WEATHER_FACTOR
    text = str(raw).strip().lower()
    if text in ("", "unknown", "unavailable", "none"):
        return DEFAULT_WEATHER_FACTOR
    try:
        value = float(text)
    except (TypeError, ValueError):
        return DEFAULT_WEATHER_FACTOR
    return max(WEATHER_FACTOR_MIN, min(WEATHER_FACTOR_MAX, value))


class IrrigationCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Central coordinator that polls valve states and sequencer progress."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        zones: list[Zone],
        safety: SafetyManager,
        sequencer: Sequencer,
        leak_sensors: list[str] | None = None,
        weather_factor_sensor: str | None = None,
        ignore_weather: bool = False,
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
        self.leak_sensors: list[str] = list(leak_sensors or [])

        self._tick_unsub: Callable[[], None] | None = None
        self._state_change_unsub: Callable[[], None] | None = None
        self._leak_unsub: Callable[[], None] | None = None
        self._weather_unsub: Callable[[], None] | None = None
        self._leak_emergency_active: bool = False
        # Set by Number entity setters before they call async_update_entry,
        # so the options-update listener can skip the disruptive reload
        # when only a live-tunable parameter changed.
        self.suppress_next_reload: bool = False

        # Weather-based runtime adjustment
        self.weather_factor_sensor: str | None = weather_factor_sensor or None
        self.weather_factor: float = DEFAULT_WEATHER_FACTOR
        self.ignore_weather: bool = bool(ignore_weather)

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
        self.stop_leak_tracking()
        self.stop_weather_tracking()
        self._stop_tick()

    # -- Weather factor tracking ---------------------------------------

    def current_adjustment(self) -> tuple[float, bool]:
        """Return (factor, ignored) snapshot for the sequencer.

        ``ignored=True`` tells the sequencer to act as if the factor were 1.0.
        The actual factor is still returned so the UI can show it.
        """
        return (self.weather_factor, self.ignore_weather)

    def start_weather_tracking(self) -> None:
        """Subscribe to the configured weather factor sensor."""
        if self._weather_unsub is not None:
            return
        if not self.weather_factor_sensor:
            return

        self._weather_unsub = async_track_state_change_event(
            self.hass,
            [self.weather_factor_sensor],
            self._on_weather_state_change,
        )

        # Seed the cache with the current state so the first run of the
        # program does not have to wait for a state change.
        self._refresh_weather_factor()

    def stop_weather_tracking(self) -> None:
        if self._weather_unsub is not None:
            try:
                self._weather_unsub()
            except Exception:  # noqa: BLE001
                _LOGGER.debug("Coordinator: weather tracking unsubscribe failed")
            self._weather_unsub = None

    @callback
    def _on_weather_state_change(self, event: Event) -> None:
        """Cache the new factor and refresh listeners."""
        new_state = event.data.get("new_state")
        value = new_state.state if new_state is not None else None
        self._apply_weather_value(value)

    def _refresh_weather_factor(self) -> None:
        """Synchronously re-read the weather sensor into the cache."""
        if not self.weather_factor_sensor:
            self.weather_factor = DEFAULT_WEATHER_FACTOR
            return
        state = self.hass.states.get(self.weather_factor_sensor)
        value = state.state if state is not None else None
        self._apply_weather_value(value)

    def _apply_weather_value(self, raw: Any) -> None:
        """Parse, clamp, store, and notify listeners."""
        factor = _parse_weather_factor(raw)
        if factor == self.weather_factor:
            return
        self.weather_factor = factor
        # Data-only update so the WeatherFactorSensor re-renders without a
        # full coordinator poll. Safe when self.data is still None (first
        # refresh) because async_update_listeners is a no-op in that case.
        try:
            self.async_update_listeners()
        except Exception:  # noqa: BLE001
            _LOGGER.debug("Coordinator: async_update_listeners failed")

    # -- Leak / water-shortage sensor handling --------------------------

    def start_leak_tracking(self) -> None:
        """Subscribe to the configured leak / water-shortage sensors.

        When any of them transitions to 'on' the sequencer is stopped and
        every known valve (zones + master) is force-closed.
        """
        if self._leak_unsub is not None:
            return
        if not self.leak_sensors:
            return

        self._leak_unsub = async_track_state_change_event(
            self.hass,
            list(self.leak_sensors),
            self._on_leak_state_change,
        )

        # Catch sensors that are already 'on' at startup.
        for sensor_id in self.leak_sensors:
            state = self.hass.states.get(sensor_id)
            if state is not None and str(state.state).lower() == "on":
                _LOGGER.warning(
                    "Coordinator: leak sensor %s is already 'on' at startup "
                    "– triggering emergency shutdown",
                    sensor_id,
                )
                self.hass.async_create_task(
                    self._trigger_leak_emergency(sensor_id, "on"),
                    f"irrigation_proxy_leak_startup_{sensor_id}",
                )
                break

    def stop_leak_tracking(self) -> None:
        if self._leak_unsub is not None:
            try:
                self._leak_unsub()
            except Exception:  # noqa: BLE001
                _LOGGER.debug("Coordinator: leak tracking unsubscribe failed")
            self._leak_unsub = None

    @callback
    def _on_leak_state_change(self, event: Event) -> None:
        """Fire emergency shutdown when a leak sensor transitions to 'on'."""
        sensor_id = event.data.get("entity_id")
        new_state = event.data.get("new_state")
        old_state = event.data.get("old_state")
        new_str = str(new_state.state).lower() if new_state is not None else ""
        old_str = str(old_state.state).lower() if old_state is not None else ""

        if new_str != "on" or old_str == "on":
            return

        self.hass.async_create_task(
            self._trigger_leak_emergency(sensor_id, new_str),
            f"irrigation_proxy_leak_{sensor_id}",
        )

    async def _trigger_leak_emergency(
        self, sensor_id: str | None, state: str
    ) -> None:
        """Stop the program, close every valve, raise a persistent notification."""
        if self._leak_emergency_active:
            _LOGGER.debug(
                "Coordinator: leak emergency already in progress, ignoring %s",
                sensor_id,
            )
            return
        self._leak_emergency_active = True

        _LOGGER.warning(
            "Coordinator: LEAK DETECTED on %s (state=%s) – stopping program "
            "and closing all valves",
            sensor_id,
            state,
        )

        self.hass.bus.async_fire(
            EVENT_LEAK_DETECTED,
            {
                "sensor_entity_id": sensor_id,
                "state": state,
            },
        )

        try:
            # Leak emergency: every second of flow adds damage. Skip the
            # drain wait even though a zone is watering – safety trumps
            # hose-fitting protection here.
            await self.sequencer.stop(skip_depressurize=True)
        except Exception:
            _LOGGER.exception(
                "Coordinator: sequencer.stop() failed during leak emergency"
            )

        try:
            await self.safety.emergency_shutdown(self.zones)
        except Exception:
            _LOGGER.exception(
                "Coordinator: zone emergency shutdown failed during leak emergency"
            )

        master = self.sequencer.master_valve
        if master:
            try:
                svc_domain, svc_action = entity_svc_close(master)
                await self.hass.services.async_call(
                    svc_domain,
                    svc_action,
                    {"entity_id": master},
                    blocking=True,
                )
            except Exception:
                _LOGGER.exception(
                    "Coordinator: failed to close master valve %s during leak emergency",
                    master,
                )

        self._create_leak_notification(sensor_id, state)

        self.notify_sequencer_state_changed()
        try:
            await self.async_request_refresh()
        except Exception:  # noqa: BLE001
            _LOGGER.debug("Coordinator: refresh after leak emergency failed")

        self._leak_emergency_active = False

    def _create_leak_notification(
        self, sensor_id: str | None, state: str
    ) -> None:
        """Raise a persistent notification about the leak event."""
        try:
            from homeassistant.components import persistent_notification
        except Exception:  # noqa: BLE001
            _LOGGER.debug("Coordinator: persistent_notification unavailable")
            return

        title = "Irrigation Proxy: leak detected"
        message = (
            f"Water leak/shortage sensor `{sensor_id}` reported `{state}`. "
            "The irrigation program has been stopped and every configured "
            "valve (zones + master) was force-closed. Resolve the leak "
            "before starting another run."
        )
        notif_id = f"{DOMAIN}_leak_{sensor_id or 'unknown'}"
        try:
            persistent_notification.async_create(
                self.hass,
                message,
                title=title,
                notification_id=notif_id,
            )
        except Exception:
            _LOGGER.exception(
                "Coordinator: failed to raise leak persistent notification"
            )

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

        # Adopt zones opened outside of the proxy immediately, so the deadman
        # is armed without waiting for the next 30 s coordinator poll.
        if zone.is_on and valve_id not in self.safety.zone_start_times:
            _LOGGER.info(
                "Coordinator: zone '%s' opened outside of proxy – adopting with deadman",
                zone.name,
            )
            self.safety.start_deadman(zone)

        data = dict(self.data or {})
        entry = dict(data.get(valve_id, {}))
        entry.update(
            {
                "is_on": zone.is_on,
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

        # Safety-net revalidation for the weather factor – the state-change
        # subscription is the authoritative source, but a missed / crashed
        # subscription still converges here within 30 s.
        if self.weather_factor_sensor:
            self._refresh_weather_factor()

        for zone in self.zones:
            valve_id = zone.valve_entity_id
            state = self.hass.states.get(valve_id)
            if state is not None:
                zone.update_state(state.state)
            else:
                _LOGGER.debug(
                    "Coordinator: entity %s unavailable during poll", valve_id
                )

            # Adopt zones opened outside of the proxy: keep them running but
            # arm a normal deadman so max_runtime still bounds the open time.
            if (
                zone.is_on
                and valve_id not in self.safety.zone_start_times
            ):
                _LOGGER.info(
                    "Coordinator: zone '%s' opened outside of proxy – adopting with deadman",
                    zone.name,
                )
                self.safety.start_deadman(zone)

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
