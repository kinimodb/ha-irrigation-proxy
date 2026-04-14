"""Irrigation Proxy – smart irrigation controller for Sonoff SWV valves."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import Event, HomeAssistant, ServiceCall

from .const import (
    CONF_DURATION_MINUTES,
    CONF_INTER_ZONE_DELAY_SECONDS,
    CONF_MAX_RUNTIME_MINUTES,
    CONF_RAIN_ADJUST_MODE,
    CONF_RAIN_THRESHOLD_MM,
    CONF_SCHEDULE_ENABLED,
    CONF_SCHEDULE_START_TIMES,
    CONF_SCHEDULE_WEEKDAYS,
    CONF_ZONE_DURATIONS,
    CONF_ZONES,
    DEFAULT_DURATION_MINUTES,
    DEFAULT_MAX_RUNTIME_MINUTES,
    DEFAULT_PAUSE_BETWEEN_ZONES_SECONDS,
    DEFAULT_RAIN_ADJUST_MODE,
    DEFAULT_RAIN_THRESHOLD_MM,
    DEFAULT_SCHEDULE_ENABLED,
    DOMAIN,
    PLATFORMS,
    SERVICE_START_PROGRAM,
    SERVICE_STOP_PROGRAM,
    WEEKDAYS,
)
from .coordinator import IrrigationCoordinator
from .safety import SafetyManager
from .scheduler import ProgramScheduler, ScheduleConfig, parse_start_times
from .sequencer import Sequencer
from .weather import WeatherProvider
from .zone import Zone

_LOGGER = logging.getLogger(__name__)


def _build_schedule_config(entry: ConfigEntry) -> ScheduleConfig:
    """Parse schedule settings from the config entry."""
    raw = {**entry.data, **entry.options}
    start_times = parse_start_times(raw.get(CONF_SCHEDULE_START_TIMES))
    weekdays_raw = raw.get(CONF_SCHEDULE_WEEKDAYS) or list(WEEKDAYS)
    weekdays = {str(w).lower() for w in weekdays_raw if str(w).lower() in WEEKDAYS}
    return ScheduleConfig(
        enabled=bool(raw.get(CONF_SCHEDULE_ENABLED, DEFAULT_SCHEDULE_ENABLED)),
        start_times=start_times,
        weekdays=weekdays,
        rain_adjust_mode=str(
            raw.get(CONF_RAIN_ADJUST_MODE, DEFAULT_RAIN_ADJUST_MODE)
        ),
    )


def _resolve_zone_duration(
    raw: dict[str, Any], valve_id: str, default_minutes: int
) -> int:
    """Look up the per-zone duration from entry data, falling back to default."""
    overrides: dict[str, Any] = raw.get(CONF_ZONE_DURATIONS) or {}
    value = overrides.get(valve_id)
    if value is None:
        return int(default_minutes)
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return int(default_minutes)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Irrigation Proxy from a config entry."""

    raw = {**entry.data, **entry.options}

    # Parse config
    zone_entity_ids: list[str] = raw.get(CONF_ZONES, [])
    default_duration: int = int(
        raw.get(CONF_DURATION_MINUTES, DEFAULT_DURATION_MINUTES)
    )
    max_runtime: int = int(
        raw.get(CONF_MAX_RUNTIME_MINUTES, DEFAULT_MAX_RUNTIME_MINUTES)
    )
    rain_threshold: float = float(
        raw.get(CONF_RAIN_THRESHOLD_MM, DEFAULT_RAIN_THRESHOLD_MM)
    )
    inter_zone_delay: int = int(
        raw.get(
            CONF_INTER_ZONE_DELAY_SECONDS, DEFAULT_PAUSE_BETWEEN_ZONES_SECONDS
        )
    )

    # Build Zone objects – keyed by valve_entity_id, honouring per-zone overrides
    zones: dict[str, Zone] = {}
    for valve_entity_id in zone_entity_ids:
        state = hass.states.get(valve_entity_id)
        if state is not None:
            name = state.attributes.get("friendly_name", valve_entity_id)
        else:
            name = valve_entity_id

        duration_minutes = _resolve_zone_duration(
            raw, valve_entity_id, default_duration
        )
        zones[valve_entity_id] = Zone(
            name=name,
            valve_entity_id=valve_entity_id,
            duration_minutes=duration_minutes,
        )

    # Safety: close all valves on startup (handles crash recovery)
    safety = SafetyManager(hass, max_runtime)
    await safety.emergency_shutdown(list(zones.values()))
    _LOGGER.info("Irrigation Proxy: closed all valves on startup (safety)")

    # Weather: Open-Meteo provider using HA's configured location
    weather: WeatherProvider | None = None
    if hasattr(hass, "config") and hasattr(hass.config, "latitude"):
        weather = WeatherProvider(
            hass=hass,
            latitude=hass.config.latitude,
            longitude=hass.config.longitude,
            rain_threshold_mm=rain_threshold,
        )
        _LOGGER.info(
            "Irrigation Proxy: weather provider configured (%.2f, %.2f)",
            hass.config.latitude,
            hass.config.longitude,
        )

    # Sequencer: runs zones in order, one at a time
    sequencer = Sequencer(
        hass=hass,
        zones=list(zones.values()),
        safety=safety,
        pause_seconds=inter_zone_delay,
    )

    # Create coordinator
    coordinator = IrrigationCoordinator(
        hass, entry, zones, safety, sequencer, weather
    )

    # Scheduler – fires sequencer at configured start times, honouring
    # the selected rain_adjust_mode.
    scheduler = ProgramScheduler(
        hass=hass,
        sequencer=sequencer,
        weather=weather,
        get_config=lambda: _build_schedule_config(entry),
        on_fire=coordinator.async_request_refresh,
    )
    coordinator.set_scheduler(scheduler)

    # Wire up on_complete callback: sequencer finished → refresh coordinator +
    # flip the 1 Hz ticker off.
    def _on_sequencer_complete() -> None:
        coordinator.notify_sequencer_state_changed()
        hass.async_create_task(coordinator.async_request_refresh())

    sequencer._on_complete = _on_sequencer_complete

    await coordinator.async_config_entry_first_refresh()

    # Store coordinator
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = coordinator

    # Forward platforms
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Now that entities exist, enable live state tracking + schedule triggers
    coordinator.start_state_tracking()
    scheduler.reload()

    # Register services (once per domain, not per entry)
    if not hass.services.has_service(DOMAIN, SERVICE_START_PROGRAM):
        _async_register_services(hass)

    # Register options update listener
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))

    # Register HA stop handler – stop sequencer and close all valves
    async def _on_ha_stop(event: Event) -> None:
        _LOGGER.info(
            "Irrigation Proxy: HA stopping – stopping program and closing all valves"
        )
        scheduler.unregister()
        await sequencer.stop()
        await safety.emergency_shutdown(list(zones.values()))

    entry.async_on_unload(
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _on_ha_stop)
    )
    entry.async_on_unload(scheduler.unregister)
    entry.async_on_unload(coordinator.stop_state_tracking)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload Irrigation Proxy config entry."""
    coordinator: IrrigationCoordinator = hass.data[DOMAIN][entry.entry_id]

    # Stop sequencer and close all valves
    await coordinator.sequencer.stop()
    if coordinator.scheduler is not None:
        coordinator.scheduler.unregister()
    coordinator.stop_state_tracking()
    await coordinator.safety.emergency_shutdown(list(coordinator.zones.values()))

    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)

    # Unregister services when no entries remain
    remaining = {
        k for k in hass.data.get(DOMAIN, {}) if k != entry.entry_id
    }
    if not remaining:
        hass.services.async_remove(DOMAIN, SERVICE_START_PROGRAM)
        hass.services.async_remove(DOMAIN, SERVICE_STOP_PROGRAM)

    return unload_ok


def _async_register_services(hass: HomeAssistant) -> None:
    """Register domain-level services for automation."""

    async def _handle_start_program(call: ServiceCall) -> None:
        """Start the irrigation program on all entries."""
        for entry_id, coordinator in hass.data.get(DOMAIN, {}).items():
            if not isinstance(coordinator, IrrigationCoordinator):
                continue
            _LOGGER.info("Service: starting program for entry %s", entry_id)
            await coordinator.sequencer.start()
            coordinator.notify_sequencer_state_changed()
            await coordinator.async_request_refresh()

    async def _handle_stop_program(call: ServiceCall) -> None:
        """Stop the irrigation program on all entries."""
        for entry_id, coordinator in hass.data.get(DOMAIN, {}).items():
            if not isinstance(coordinator, IrrigationCoordinator):
                continue
            _LOGGER.info("Service: stopping program for entry %s", entry_id)
            await coordinator.sequencer.stop()
            coordinator.notify_sequencer_state_changed()
            await coordinator.async_request_refresh()

    hass.services.async_register(DOMAIN, SERVICE_START_PROGRAM, _handle_start_program)
    hass.services.async_register(DOMAIN, SERVICE_STOP_PROGRAM, _handle_stop_program)

    _LOGGER.info("Irrigation Proxy: registered services")


async def _async_options_updated(
    hass: HomeAssistant, entry: ConfigEntry
) -> None:
    """Handle options update by reloading the config entry."""
    await hass.config_entries.async_reload(entry.entry_id)
