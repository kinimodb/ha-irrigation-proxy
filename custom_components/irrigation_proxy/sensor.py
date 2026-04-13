"""Sensor entities for irrigation program visibility."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
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
    """Set up sensor entities from a config entry."""
    coordinator: IrrigationCoordinator = hass.data[DOMAIN][entry.entry_id]

    async_add_entities(
        [
            ProgramStatusSensor(coordinator, entry),
            CurrentZoneSensor(coordinator, entry),
            ZoneTimeRemainingSensor(coordinator, entry),
        ]
    )


class _BaseSensor(CoordinatorEntity[IrrigationCoordinator], SensorEntity):
    """Base class for irrigation sensors – shared device grouping."""

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
        """Group all sensors under the same device as the switches."""
        return DeviceInfo(
            identifiers={(DOMAIN, self._entry.entry_id)},
            name=self._entry.data.get(CONF_NAME, "Irrigation"),
            manufacturer="Irrigation Proxy",
            model="Virtual Irrigation Controller",
        )

    def _seq_data(self) -> dict[str, Any]:
        """Helper to read sequencer data from coordinator."""
        if self.coordinator.data is None:
            return {}
        return self.coordinator.data.get("sequencer", {})


class ProgramStatusSensor(_BaseSensor):
    """Shows the sequencer state: idle / running."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["idle", "running"]
    _attr_icon = "mdi:sprinkler"
    _attr_translation_key = "program_status"

    def __init__(
        self,
        coordinator: IrrigationCoordinator,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_program_status"
        self._attr_name = "Program Status"

    @property
    def native_value(self) -> str:
        return self._seq_data().get("state", "idle")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        seq = self._seq_data()
        return {
            "total_zones": seq.get("total_zones", 0),
            "current_zone_index": seq.get("current_zone_index", -1),
        }


class CurrentZoneSensor(_BaseSensor):
    """Shows the name of the currently active zone."""

    _attr_icon = "mdi:water"
    _attr_translation_key = "current_zone"

    def __init__(
        self,
        coordinator: IrrigationCoordinator,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_current_zone"
        self._attr_name = "Current Zone"

    @property
    def native_value(self) -> str | None:
        return self._seq_data().get("current_zone")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        seq = self._seq_data()
        return {
            "next_zone": seq.get("next_zone"),
            "current_zone_entity_id": seq.get("current_zone_entity_id"),
        }


class ZoneTimeRemainingSensor(_BaseSensor):
    """Shows remaining seconds on the current zone."""

    _attr_device_class = SensorDeviceClass.DURATION
    _attr_native_unit_of_measurement = "s"
    _attr_icon = "mdi:timer-outline"
    _attr_translation_key = "zone_time_remaining"

    def __init__(
        self,
        coordinator: IrrigationCoordinator,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_zone_time_remaining"
        self._attr_name = "Zone Time Remaining"

    @property
    def native_value(self) -> int | None:
        return self._seq_data().get("remaining_zone_seconds")
