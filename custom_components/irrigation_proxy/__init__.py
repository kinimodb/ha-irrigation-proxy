"""Irrigation Proxy – simple sequenced irrigation for HA switch valves."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import Event, HomeAssistant, ServiceCall
from homeassistant.helpers import entity_registry as er

from .const import (
    CONF_DEPRESSURIZE_SECONDS,
    CONF_INTER_ZONE_DELAY_SECONDS,
    CONF_LEAK_SENSORS,
    CONF_MASTER_VALVE,
    CONF_MAX_RUNTIME_MINUTES,
    CONF_SCHEDULE_ENABLED,
    CONF_SCHEDULE_START_TIMES,
    CONF_SCHEDULE_WEEKDAYS,
    CONF_ZONE_DURATION_MINUTES,
    CONF_ZONE_ID,
    CONF_ZONE_NAME,
    CONF_ZONE_VALVE,
    CONF_ZONES,
    DEFAULT_DEPRESSURIZE_SECONDS,
    DEFAULT_DURATION_MINUTES,
    DEFAULT_INTER_ZONE_DELAY_SECONDS,
    DEFAULT_MAX_RUNTIME_MINUTES,
    DEFAULT_SCHEDULE_ENABLED,
    DOMAIN,
    PLATFORMS,
    SERVICE_START_PROGRAM,
    SERVICE_STOP_PROGRAM,
    WEEKDAYS,
)
from .coordinator import IrrigationCoordinator
from .migration import migrate_v1_zones
from .safety import SafetyManager
from .scheduler import ProgramScheduler, ScheduleConfig, parse_start_times
from .sequencer import Sequencer
from .zone import Zone, entity_svc_close

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
    )


def _build_zones(raw: dict[str, Any]) -> list[Zone]:
    """Translate the CONF_ZONES list of dicts into Zone objects (in order)."""
    raw = migrate_v1_zones(raw)  # defensive: handle un-migrated v0.4.x data
    zones_raw = raw.get(CONF_ZONES) or []
    zones: list[Zone] = []
    for entry in zones_raw:
        valve = entry.get(CONF_ZONE_VALVE)
        if not valve:
            _LOGGER.warning(
                "Irrigation Proxy: skipping zone without valve entity: %r", entry
            )
            continue
        name = entry.get(CONF_ZONE_NAME) or valve
        try:
            duration = int(
                entry.get(CONF_ZONE_DURATION_MINUTES, DEFAULT_DURATION_MINUTES)
            )
        except (TypeError, ValueError):
            duration = DEFAULT_DURATION_MINUTES
        zones.append(
            Zone(
                name=name,
                valve_entity_id=valve,
                duration_minutes=max(1, duration),
                zone_id=entry.get(CONF_ZONE_ID),
            )
        )
    return zones


async def async_migrate_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> bool:
    """Migrate config entries from older versions."""
    _LOGGER.info(
        "Migrating Irrigation Proxy config entry from version %s",
        config_entry.version,
    )

    if config_entry.version < 2:
        new_data = migrate_v1_zones({**config_entry.data})
        new_options = migrate_v1_zones({**config_entry.options})
        config_entry.version = 2
        hass.config_entries.async_update_entry(
            config_entry, data=new_data, options=new_options
        )
        _LOGGER.info("Irrigation Proxy: migration to version 2 successful")

    return True


def _purge_stale_entities(
    hass: HomeAssistant, entry: ConfigEntry, zones: list[Zone]
) -> None:
    """Remove entities whose unique_ids no longer map to a current platform entity."""
    registry = er.async_get(hass)
    # v0.7.0: ZoneDurationSensor (sensor.<zone>_duration) dropped.
    stale_unique_ids = [
        f"{entry.entry_id}_{zone.valve_entity_id}_duration" for zone in zones
    ]
    for unique_id in stale_unique_ids:
        entity_id = registry.async_get_entity_id("sensor", DOMAIN, unique_id)
        if entity_id:
            _LOGGER.info(
                "Irrigation Proxy: removing stale entity %s (unique_id=%s)",
                entity_id,
                unique_id,
            )
            registry.async_remove(entity_id)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Irrigation Proxy from a config entry."""
    raw = {**entry.data, **entry.options}

    max_runtime: int = int(
        raw.get(CONF_MAX_RUNTIME_MINUTES, DEFAULT_MAX_RUNTIME_MINUTES)
    )
    inter_zone_delay: int = int(
        raw.get(
            CONF_INTER_ZONE_DELAY_SECONDS, DEFAULT_INTER_ZONE_DELAY_SECONDS
        )
    )
    depressurize: int = int(
        raw.get(CONF_DEPRESSURIZE_SECONDS, DEFAULT_DEPRESSURIZE_SECONDS)
    )
    master_valve: str | None = raw.get(CONF_MASTER_VALVE) or None

    leak_sensors_raw = raw.get(CONF_LEAK_SENSORS) or []
    if isinstance(leak_sensors_raw, str):
        leak_sensors_raw = [leak_sensors_raw]
    leak_sensors = [
        str(s) for s in leak_sensors_raw if isinstance(s, str) and s
    ]

    zones = _build_zones(raw)

    # v0.7.0 removed the per-zone "<zone> Duration" sensor in favour of the
    # Number entity. Old registry entries stay behind as "unavailable" until
    # we actively purge them here.
    _purge_stale_entities(hass, entry, zones)

    # Safety: close all valves (zones + master) on startup for crash recovery.
    safety = SafetyManager(hass, max_runtime)
    await safety.emergency_shutdown(zones)
    if master_valve:
        try:
            svc_domain, svc_action = entity_svc_close(master_valve)
            await hass.services.async_call(
                svc_domain,
                svc_action,
                {"entity_id": master_valve},
                blocking=True,
            )
        except Exception:
            _LOGGER.exception(
                "Irrigation Proxy: failed to force-close master valve on startup"
            )
    _LOGGER.info("Irrigation Proxy: closed all valves on startup (safety)")

    sequencer = Sequencer(
        hass=hass,
        zones=zones,
        safety=safety,
        pause_seconds=inter_zone_delay,
        master_valve_entity_id=master_valve,
        depressurize_seconds=depressurize,
    )

    coordinator = IrrigationCoordinator(
        hass, entry, zones, safety, sequencer, leak_sensors=leak_sensors
    )

    scheduler = ProgramScheduler(
        hass=hass,
        sequencer=sequencer,
        get_config=lambda: _build_schedule_config(entry),
        on_fire=coordinator.async_request_refresh,
    )
    coordinator.set_scheduler(scheduler)

    def _on_sequencer_complete() -> None:
        coordinator.notify_sequencer_state_changed()
        hass.async_create_task(coordinator.async_request_refresh())

    sequencer._on_complete = _on_sequencer_complete

    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    coordinator.start_state_tracking()
    coordinator.start_leak_tracking()
    scheduler.reload()

    if not hass.services.has_service(DOMAIN, SERVICE_START_PROGRAM):
        _async_register_services(hass)

    entry.async_on_unload(entry.add_update_listener(_async_options_updated))

    async def _on_ha_stop(event: Event) -> None:
        _LOGGER.info(
            "Irrigation Proxy: HA stopping – closing program and all valves"
        )
        scheduler.unregister()
        await sequencer.stop()
        await safety.emergency_shutdown(zones)

    entry.async_on_unload(
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _on_ha_stop)
    )
    entry.async_on_unload(scheduler.unregister)
    entry.async_on_unload(coordinator.stop_state_tracking)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload Irrigation Proxy config entry."""
    coordinator: IrrigationCoordinator = hass.data[DOMAIN][entry.entry_id]

    await coordinator.sequencer.stop()
    if coordinator.scheduler is not None:
        coordinator.scheduler.unregister()
    coordinator.stop_state_tracking()
    await coordinator.safety.emergency_shutdown(coordinator.zones)

    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)

    remaining = {
        k for k in hass.data.get(DOMAIN, {}) if k != entry.entry_id
    }
    if not remaining:
        hass.services.async_remove(DOMAIN, SERVICE_START_PROGRAM)
        hass.services.async_remove(DOMAIN, SERVICE_STOP_PROGRAM)

    return unload_ok


def _async_register_services(hass: HomeAssistant) -> None:
    """Register domain-level services."""

    def _target_coordinators(
        call: ServiceCall,
    ) -> list[tuple[str, "IrrigationCoordinator"]]:
        """Return the list of (entry_id, coordinator) pairs to act on.

        If the caller supplied entry_id we act on that one entry only.
        Without entry_id we fall back to acting on every entry (legacy
        behaviour) and emit a deprecation warning so users can migrate
        before the fallback is removed in v0.7.
        """
        all_entries: dict = hass.data.get(DOMAIN, {})
        requested_id: str | None = call.data.get("entry_id")

        if requested_id:
            coord = all_entries.get(requested_id)
            if not isinstance(coord, IrrigationCoordinator):
                _LOGGER.warning(
                    "Service: entry_id %r not found or not an IrrigationCoordinator",
                    requested_id,
                )
                return []
            return [(requested_id, coord)]

        # Legacy broadcast: act on all entries.
        if len(all_entries) > 1:
            _LOGGER.warning(
                "Service called without entry_id – targeting ALL %d irrigation "
                "entries. This behaviour is deprecated and will become an error "
                "in v0.7. Pass 'entry_id' to target a specific program.",
                len(all_entries),
            )
        return [
            (eid, c)
            for eid, c in all_entries.items()
            if isinstance(c, IrrigationCoordinator)
        ]

    async def _handle_start_program(call: ServiceCall) -> None:
        for entry_id, coordinator in _target_coordinators(call):
            _LOGGER.info("Service: starting program for entry %s", entry_id)
            await coordinator.sequencer.start()
            coordinator.notify_sequencer_state_changed()
            await coordinator.async_request_refresh()

    async def _handle_stop_program(call: ServiceCall) -> None:
        for entry_id, coordinator in _target_coordinators(call):
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
    """Reload the config entry when options change.

    Number entities persist their changes by writing to ``entry.data`` and
    set ``coordinator.suppress_next_reload`` so we can skip the reload
    here – the live coordinator already holds the new value, and a reload
    would tear down a running program.
    """
    coordinator: IrrigationCoordinator | None = hass.data.get(
        DOMAIN, {}
    ).get(entry.entry_id)
    if coordinator is not None and coordinator.suppress_next_reload:
        coordinator.suppress_next_reload = False
        return

    await hass.config_entries.async_reload(entry.entry_id)
