"""Binary sensor entities for irrigation weather status."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
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
    """Set up binary sensor entities from a config entry."""
    coordinator: IrrigationCoordinator = hass.data[DOMAIN][entry.entry_id]

    async_add_entities([RainSkipSensor(coordinator, entry)])


class RainSkipSensor(CoordinatorEntity[IrrigationCoordinator], BinarySensorEntity):
    """Binary sensor: ON when irrigation should be skipped due to rain.

    Uses weather data from the coordinator to determine if recent +
    forecast precipitation exceeds the configured rain threshold.
    """

    _attr_has_entity_name = True
    _attr_device_class = BinarySensorDeviceClass.MOISTURE
    _attr_icon = "mdi:weather-rainy"
    _attr_translation_key = "rain_skip"

    def __init__(
        self,
        coordinator: IrrigationCoordinator,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_rain_skip"
        self._attr_name = "Rain Skip"

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._entry.entry_id)},
            name=self._entry.data.get(CONF_NAME, "Irrigation"),
            manufacturer="Irrigation Proxy",
            model="Virtual Irrigation Controller",
        )

    @property
    def is_on(self) -> bool:
        """Return True if irrigation should be skipped due to rain."""
        if self.coordinator.data is None:
            return False
        weather = self.coordinator.data.get("weather", {})
        return weather.get("rain_skip", False)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        if self.coordinator.data is None:
            return {}
        weather = self.coordinator.data.get("weather", {})
        return {
            "precipitation_last_24h": weather.get("precipitation_last_24h", 0.0),
            "precipitation_forecast_24h": weather.get(
                "precipitation_forecast_24h", 0.0
            ),
            "rain_threshold_mm": weather.get("rain_threshold_mm"),
        }
