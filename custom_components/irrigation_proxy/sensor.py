"""Sensor entities for irrigation program visibility and weather data."""

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

    entities: list[SensorEntity] = [
        ProgramStatusSensor(coordinator, entry),
        CurrentZoneSensor(coordinator, entry),
        ZoneTimeRemainingSensor(coordinator, entry),
    ]

    # Weather sensors (only if weather provider is configured)
    if coordinator.weather is not None:
        entities.extend([
            EvapotranspirationSensor(coordinator, entry),
            WaterNeedFactorSensor(coordinator, entry),
        ])

    async_add_entities(entities)


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


# -- Weather sensors --------------------------------------------------------


class _WeatherSensor(_BaseSensor):
    """Base class for weather-related sensors."""

    def _weather_data(self) -> dict[str, Any]:
        """Helper to read weather data from coordinator."""
        if self.coordinator.data is None:
            return {}
        return self.coordinator.data.get("weather", {})


class EvapotranspirationSensor(_WeatherSensor):
    """Shows today's reference evapotranspiration (ET₀) in mm."""

    _attr_icon = "mdi:water-thermometer"
    _attr_native_unit_of_measurement = "mm"
    _attr_translation_key = "evapotranspiration"

    def __init__(
        self,
        coordinator: IrrigationCoordinator,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_evapotranspiration"
        self._attr_name = "Evapotranspiration"

    @property
    def native_value(self) -> float | None:
        et0 = self._weather_data().get("et0_today")
        if et0 is None:
            return None
        return round(et0, 1)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        w = self._weather_data()
        attrs: dict[str, Any] = {}
        if "temperature_max" in w:
            attrs["temperature_max"] = w["temperature_max"]
        if "last_update" in w:
            attrs["weather_last_update"] = w["last_update"]
        if w.get("last_error"):
            attrs["weather_error"] = w["last_error"]
        return attrs


class WaterNeedFactorSensor(_WeatherSensor):
    """Shows the current irrigation adjustment factor (0.0 – 2.0).

    Factor > 1.0 = hotter/drier than normal → water more
    Factor < 1.0 = cooler/wetter than normal → water less
    Factor = 0.0 = rain skip active → no watering needed
    """

    _attr_icon = "mdi:water-percent"
    _attr_native_unit_of_measurement = "x"
    _attr_translation_key = "water_need_factor"

    def __init__(
        self,
        coordinator: IrrigationCoordinator,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_water_need_factor"
        self._attr_name = "Water Need Factor"

    @property
    def native_value(self) -> float | None:
        factor = self._weather_data().get("water_need_factor")
        if factor is None:
            return None
        return round(factor, 2)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        w = self._weather_data()
        return {
            "precipitation_last_24h": w.get("precipitation_last_24h", 0.0),
            "precipitation_forecast_24h": w.get("precipitation_forecast_24h", 0.0),
            "rain_skip": w.get("rain_skip", False),
        }
