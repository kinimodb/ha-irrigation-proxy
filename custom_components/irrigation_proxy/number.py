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
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    CONF_DEPRESSURIZE_SECONDS,
    CONF_INTER_ZONE_DELAY_SECONDS,
    CONF_MAX_RUNTIME_MINUTES,
    CONF_NAME,
    CONF_ZONE_DURATION_MINUTES,
    CONF_ZONE_VALVE,
    CONF_ZONES,
    DOMAIN,
)
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
        DepressurizeSecondsNumber(coordinator, entry),
    ]

    entities.extend(
        ZoneDurationNumber(coordinator, entry, zone.valve_entity_id)
        for zone in coordinator.zones
    )

    async_add_entities(entities)


def _persist_entry_data(
    hass: HomeAssistant,
    coordinator: IrrigationCoordinator,
    entry: ConfigEntry,
    updates: dict[str, Any],
) -> None:
    """Persist runtime tweaks into the config entry without triggering a reload.

    Update listeners get notified for any data change, which would normally
    re-instantiate the coordinator and abort a running program. The
    coordinator already holds the new live values – setting
    `suppress_next_reload` lets the listener skip the reload exactly once.
    """
    coordinator.suppress_next_reload = True
    new_data = {**entry.data, **updates}
    hass.config_entries.async_update_entry(entry, data=new_data)


class _BaseNumber(CoordinatorEntity[IrrigationCoordinator], NumberEntity):
    """Base class for irrigation number entities – shared device grouping."""

    _attr_has_entity_name = True
    _attr_mode = NumberMode.BOX

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
        """Update zone duration in-memory and persist to the config entry."""
        zone = self.coordinator.zones_by_valve.get(self._valve_entity_id)
        if zone is None:
            return
        new_minutes = max(1, int(value))
        zone.duration_minutes = new_minutes
        _LOGGER.info(
            "Zone '%s' duration set to %d min", zone.name, new_minutes
        )

        zones_raw = list(self._entry.data.get(CONF_ZONES) or [])
        for entry in zones_raw:
            if entry.get(CONF_ZONE_VALVE) == self._valve_entity_id:
                entry[CONF_ZONE_DURATION_MINUTES] = new_minutes
                break
        _persist_entry_data(
            self.hass, self.coordinator, self._entry, {CONF_ZONES: zones_raw}
        )
        self.async_write_ha_state()


class InterZoneDelayNumber(_BaseNumber):
    """Pause between zones in seconds."""

    _attr_native_min_value = 0
    _attr_native_max_value = 600
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
        new_seconds = max(0, int(value))
        self.coordinator.sequencer.pause_seconds = new_seconds
        _LOGGER.info("Inter-zone delay set to %ds", new_seconds)
        _persist_entry_data(
            self.hass,
            self.coordinator,
            self._entry,
            {CONF_INTER_ZONE_DELAY_SECONDS: new_seconds},
        )
        self.async_write_ha_state()


class MaxRuntimeNumber(_BaseNumber):
    """Deadman timer max runtime per zone in minutes."""

    _attr_native_min_value = 5
    _attr_native_max_value = 180
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
        new_minutes = max(1, int(value))
        self.coordinator.safety.max_runtime_minutes = new_minutes
        _LOGGER.info("Max runtime set to %d min", new_minutes)
        _persist_entry_data(
            self.hass,
            self.coordinator,
            self._entry,
            {CONF_MAX_RUNTIME_MINUTES: new_minutes},
        )
        self.async_write_ha_state()


class DepressurizeSecondsNumber(_BaseNumber):
    """Drain delay between closing the master valve and the zone valve."""

    _attr_native_min_value = 0
    _attr_native_max_value = 60
    _attr_native_step = 1
    _attr_native_unit_of_measurement = "s"
    _attr_device_class = NumberDeviceClass.DURATION
    _attr_icon = "mdi:water-pump-off"
    _attr_translation_key = "depressurize_seconds"

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
        _persist_entry_data(
            self.hass,
            self.coordinator,
            self._entry,
            {CONF_DEPRESSURIZE_SECONDS: new_seconds},
        )
        self.async_write_ha_state()
