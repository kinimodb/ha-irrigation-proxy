"""Irrigation Proxy – smart irrigation controller for Sonoff SWV valves."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import Event, HomeAssistant, ServiceCall

from .const import (
    CONF_DURATION_MINUTES,
    CONF_MAX_RUNTIME_MINUTES,
    CONF_RAIN_THRESHOLD_MM,
    CONF_ZONES,
    DEFAULT_DURATION_MINUTES,
    DEFAULT_MAX_RUNTIME_MINUTES,
    DEFAULT_PAUSE_BETWEEN_ZONES_SECONDS,
    DEFAULT_RAIN_THRESHOLD_MM,
    DOMAIN,
    PLATFORMS,
    SERVICE_START_PROGRAM,
    SERVICE_STOP_PROGRAM,
)
from .coordinator import IrrigationCoordinator
from .safety import SafetyManager
from .sequencer import Sequencer
from .weather import WeatherProvider
from .zone import Zone

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Irrigation Proxy from a config entry."""

    # Parse config
    zone_entity_ids: list[str] = entry.data.get(CONF_ZONES, [])
    duration_minutes: int = int(
        entry.data.get(CONF_DURATION_MINUTES, DEFAULT_DURATION_MINUTES)
    )
    max_runtime: int = int(
        entry.data.get(CONF_MAX_RUNTIME_MINUTES, DEFAULT_MAX_RUNTIME_MINUTES)
    )
    rain_threshold: float = float(
        entry.data.get(CONF_RAIN_THRESHOLD_MM, DEFAULT_RAIN_THRESHOLD_MM)
    )

    # Build Zone objects – keyed by valve_entity_id
    zones: dict[str, Zone] = {}
    for valve_entity_id in zone_entity_ids:
        # Derive a friendly name from HA's entity registry
        state = hass.states.get(valve_entity_id)
        if state is not None:
            name = state.attributes.get("friendly_name", valve_entity_id)
        else:
            name = valve_entity_id

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
        pause_seconds=DEFAULT_PAUSE_BETWEEN_ZONES_SECONDS,
    )

    # Create coordinator
    coordinator = IrrigationCoordinator(
        hass, entry, zones, safety, sequencer, weather
    )

    # Wire up on_complete callback to trigger coordinator refresh
    sequencer._on_complete = coordinator.async_request_refresh

    await coordinator.async_config_entry_first_refresh()

    # Store coordinator
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = coordinator

    # Forward platforms
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

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
        await sequencer.stop()
        await safety.emergency_shutdown(list(zones.values()))

    entry.async_on_unload(
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _on_ha_stop)
    )

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload Irrigation Proxy config entry."""
    coordinator: IrrigationCoordinator = hass.data[DOMAIN][entry.entry_id]

    # Stop sequencer and close all valves
    await coordinator.sequencer.stop()
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
            await coordinator.async_request_refresh()

    async def _handle_stop_program(call: ServiceCall) -> None:
        """Stop the irrigation program on all entries."""
        for entry_id, coordinator in hass.data.get(DOMAIN, {}).items():
            if not isinstance(coordinator, IrrigationCoordinator):
                continue
            _LOGGER.info("Service: stopping program for entry %s", entry_id)
            await coordinator.sequencer.stop()
            await coordinator.async_request_refresh()

    hass.services.async_register(DOMAIN, SERVICE_START_PROGRAM, _handle_start_program)
    hass.services.async_register(DOMAIN, SERVICE_STOP_PROGRAM, _handle_stop_program)

    _LOGGER.info("Irrigation Proxy: registered services")


async def _async_options_updated(
    hass: HomeAssistant, entry: ConfigEntry
) -> None:
    """Handle options update by reloading the config entry."""
    await hass.config_entries.async_reload(entry.entry_id)
