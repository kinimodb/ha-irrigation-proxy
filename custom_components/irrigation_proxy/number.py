"""Number entities for dashboard-adjustable irrigation parameters."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.number import NumberDeviceClass, NumberEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_NAME, DOMAIN
from .coordinator import IrrigationCoordinator

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
    ]

    entities.extend(
        ZoneDurationNumber(coordinator, entry, zone.valve_entity_id)
        for zone in coordinator.zones
    )

    async_add_entities(entities)


class _BaseNumber(CoordinatorEntity[IrrigationCoordinator], NumberEntity):
    """Base class for irrigation number entities – shared device grouping."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: IrrigationCoordinator,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._entry.entry_id)},
            name=self._entry.data.get(CONF_NAME, "Irrigation"),
            manufacturer="Irrigation Proxy",
            model="Virtual Irrigation Controller",
        )


class ZoneDurationNumber(_BaseNumber):
    """Per-zone base runtime in minutes, adjustable from dashboard."""

    _attr_native_min_value = 1
    _attr_native_max_value = 120
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
        """Update zone duration in-memory."""
        zone = self.coordinator.zones_by_valve.get(self._valve_entity_id)
        if zone is None:
            return
        zone.duration_minutes = int(value)
        _LOGGER.info(
            "Zone '%s' duration set to %d min (in-memory)",
            zone.name,
            zone.duration_minutes,
        )
        self.async_write_ha_state()


class InterZoneDelayNumber(_BaseNumber):
    """Pause between zones in seconds."""

    _attr_native_min_value = 0
    _attr_native_max_value = 300
    _attr_native_step = 5
    _attr_native_unit_of_measurement = "s"
    _attr_device_class = NumberDeviceClass.DURATION
    _attr_icon = "mdi:timer-pause"
    _attr_translation_key = "inter_zone_delay"

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
        """Update inter-zone delay in-memory."""
        self.coordinator.sequencer.pause_seconds = int(value)
        _LOGGER.info(
            "Inter-zone delay set to %ds (in-memory)",
            self.coordinator.sequencer.pause_seconds,
        )
        self.async_write_ha_state()


class MaxRuntimeNumber(_BaseNumber):
    """Deadman timer max runtime per zone in minutes."""

    _attr_native_min_value = 10
    _attr_native_max_value = 120
    _attr_native_step = 5
    _attr_native_unit_of_measurement = "min"
    _attr_device_class = NumberDeviceClass.DURATION
    _attr_icon = "mdi:shield-alert-outline"
    _attr_translation_key = "max_runtime"

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
        """Update deadman timer max runtime in-memory."""
        self.coordinator.safety.max_runtime_minutes = int(value)
        _LOGGER.info(
            "Max runtime set to %d min (in-memory)",
            self.coordinator.safety.max_runtime_minutes,
        )
        self.async_write_ha_state()
