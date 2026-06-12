"""Number entities for dashboard-adjustable irrigation parameters."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.number import (
    NumberDeviceClass,
    NumberEntity,
    NumberMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    CONF_DEPRESSURIZE_SECONDS,
    CONF_INTER_ZONE_DELAY_SECONDS,
    CONF_MAX_RUNTIME_MINUTES,
    CONF_ZONE_DURATION_MINUTES,
    CONF_ZONE_VALVE,
    CONF_ZONES,
    DEPRESSURIZE_MAX_SECONDS,
    DEPRESSURIZE_MIN_SECONDS,
    DOMAIN,
    INTER_ZONE_DELAY_MAX_SECONDS,
    INTER_ZONE_DELAY_MIN_SECONDS,
    MAX_RUNTIME_MAX_MINUTES,
    MAX_RUNTIME_MIN_MINUTES,
    ZONE_DURATION_MAX_MINUTES,
    ZONE_DURATION_MIN_MINUTES,
)
from .coordinator import IrrigationCoordinator
from .entity import IrrigationProxyEntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up number entities from a config entry."""
    coordinator: IrrigationCoordinator = hass.data[DOMAIN][entry.entry_id]

    entities: list[NumberEntity] = [
        InterZoneDelayNumber(coordinator, entry),
        MaxRuntimeNumber(coordinator, entry),
        DepressurizeSecondsNumber(coordinator, entry),
    ]

    for zone in coordinator.zones:
        entities.append(ZoneDurationNumber(coordinator, entry, zone.valve_entity_id))

    async_add_entities(entities)


class _BaseNumber(IrrigationProxyEntity, NumberEntity):
    """Base class for irrigation number entities – shared box mode."""

    _attr_mode = NumberMode.BOX


class ZoneDurationNumber(_BaseNumber):
    """Per-zone base runtime in minutes, adjustable from dashboard."""

    _attr_native_min_value = ZONE_DURATION_MIN_MINUTES
    _attr_native_max_value = ZONE_DURATION_MAX_MINUTES
    _attr_native_step = 1
    _attr_native_unit_of_measurement = "min"
    _attr_device_class = NumberDeviceClass.DURATION
    _attr_icon = "mdi:timer-cog-outline"
    _attr_translation_key = "zone_duration"

    def __init__(
        self,
        coordinator: IrrigationCoordinator,
        entry: ConfigEntry,
        valve_entity_id: str,
    ) -> None:
        super().__init__(coordinator, entry)
        self._valve_entity_id = valve_entity_id
        zone = coordinator.zones_by_valve[valve_entity_id]
        self._attr_unique_id = (
            f"{entry.entry_id}_{valve_entity_id}_duration_number"
        )
        self._attr_name = f"{zone.name} Duration"

    @property
    def native_value(self) -> float:
        zone = self.coordinator.zones_by_valve.get(self._valve_entity_id)
        if zone is None:
            return 0
        return zone.duration_minutes

    async def async_set_native_value(self, value: float) -> None:
        """Update zone duration in-memory and persist to the config entry."""
        zone = self.coordinator.zones_by_valve.get(self._valve_entity_id)
        if zone is None:
            return
        new_minutes = max(1, int(value))
        zone.duration_minutes = new_minutes
        _LOGGER.info("Zone '%s' duration set to %d min", zone.name, new_minutes)

        # Die Zonen-Dicts kopieren statt in-place zu mutieren: HA erkennt
        # die Änderung an entry.data sonst nicht und speichert sie nie.
        zones_raw = [dict(z) for z in (self._entry.data.get(CONF_ZONES) or [])]
        for zone_conf in zones_raw:
            if zone_conf.get(CONF_ZONE_VALVE) == self._valve_entity_id:
                zone_conf[CONF_ZONE_DURATION_MINUTES] = new_minutes
                break
        self.coordinator.persist_entry_data({CONF_ZONES: zones_raw})
        self.async_write_ha_state()


class InterZoneDelayNumber(_BaseNumber):
    """Pause between zones in seconds."""

    _attr_native_min_value = INTER_ZONE_DELAY_MIN_SECONDS
    _attr_native_max_value = INTER_ZONE_DELAY_MAX_SECONDS
    _attr_native_step = 5
    _attr_native_unit_of_measurement = "s"
    _attr_device_class = NumberDeviceClass.DURATION
    _attr_icon = "mdi:timer-pause"
    _attr_translation_key = "inter_zone_delay"
    # Install-time value, not a daily knob – keep off the dashboard by default.
    _attr_entity_registry_enabled_default = False

    def __init__(
        self,
        coordinator: IrrigationCoordinator,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_inter_zone_delay_number"
        self._attr_name = "Inter-Zone Delay"

    @property
    def native_value(self) -> float:
        return self.coordinator.sequencer.pause_seconds

    async def async_set_native_value(self, value: float) -> None:
        new_seconds = max(0, int(value))
        self.coordinator.sequencer.pause_seconds = new_seconds
        _LOGGER.info("Inter-zone delay set to %ds", new_seconds)
        self.coordinator.persist_entry_data(
            {CONF_INTER_ZONE_DELAY_SECONDS: new_seconds}
        )
        self.async_write_ha_state()


class MaxRuntimeNumber(_BaseNumber):
    """Deadman timer max runtime per zone in minutes."""

    _attr_native_min_value = MAX_RUNTIME_MIN_MINUTES
    _attr_native_max_value = MAX_RUNTIME_MAX_MINUTES
    _attr_native_step = 5
    _attr_native_unit_of_measurement = "min"
    _attr_device_class = NumberDeviceClass.DURATION
    _attr_icon = "mdi:shield-alert-outline"
    _attr_translation_key = "max_runtime"
    # Safety ceiling – deliberately kept out of easy reach on dashboards.
    _attr_entity_registry_enabled_default = False

    def __init__(
        self,
        coordinator: IrrigationCoordinator,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_max_runtime_number"
        self._attr_name = "Max Runtime"

    @property
    def native_value(self) -> float:
        return self.coordinator.safety.max_runtime_minutes

    async def async_set_native_value(self, value: float) -> None:
        new_minutes = max(1, int(value))
        self.coordinator.safety.max_runtime_minutes = new_minutes
        _LOGGER.info("Max runtime set to %d min", new_minutes)
        self.coordinator.persist_entry_data(
            {CONF_MAX_RUNTIME_MINUTES: new_minutes}
        )
        self.async_write_ha_state()


class DepressurizeSecondsNumber(_BaseNumber):
    """Drain delay between closing the master valve and the zone valve."""

    _attr_native_min_value = DEPRESSURIZE_MIN_SECONDS
    _attr_native_max_value = DEPRESSURIZE_MAX_SECONDS
    _attr_native_step = 1
    _attr_native_unit_of_measurement = "s"
    _attr_device_class = NumberDeviceClass.DURATION
    _attr_icon = "mdi:water-pump-off"
    _attr_translation_key = "depressurize_seconds"
    # Install-time plumbing value, not a daily knob – off the dashboard by default.
    _attr_entity_registry_enabled_default = False

    def __init__(
        self,
        coordinator: IrrigationCoordinator,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_depressurize_seconds_number"
        self._attr_name = "Depressurize Delay"

    @property
    def native_value(self) -> float:
        return self.coordinator.sequencer.depressurize_seconds

    async def async_set_native_value(self, value: float) -> None:
        new_seconds = max(0, int(value))
        self.coordinator.sequencer.depressurize_seconds = new_seconds
        _LOGGER.info("Depressurize delay set to %ds", new_seconds)
        self.coordinator.persist_entry_data(
            {CONF_DEPRESSURIZE_SECONDS: new_seconds}
        )
        self.async_write_ha_state()
