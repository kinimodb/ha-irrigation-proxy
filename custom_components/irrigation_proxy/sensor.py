"""Sensor entities for irrigation program visibility."""

from __future__ import annotations

import logging
from datetime import datetime
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
        ProgramTotalRemainingSensor(coordinator, entry),
        NextStartSensor(coordinator, entry),
    ]

    entities.extend(
        ZoneDurationSensor(coordinator, entry, zone.valve_entity_id)
        for zone in coordinator.zones
    )

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
        return DeviceInfo(
            identifiers={(DOMAIN, self._entry.entry_id)},
            name=self._entry.data.get(CONF_NAME, "Irrigation"),
            manufacturer="Irrigation Proxy",
            model="Virtual Irrigation Controller",
        )

    def _seq_data(self) -> dict[str, Any]:
        if self.coordinator.data is None:
            return {}
        return self.coordinator.data.get("sequencer", {})

    def _sched_data(self) -> dict[str, Any]:
        if self.coordinator.data is None:
            return {}
        return self.coordinator.data.get("scheduler", {}) or {}


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
    """Name of the currently active zone."""

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
    """Seconds left on the current zone; idle fallback = first zone's duration."""

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
        seq = self._seq_data()
        if seq.get("state") == "running":
            return seq.get("remaining_zone_seconds")

        zones = seq.get("zones") or []
        if zones:
            return int(zones[0].get("duration_seconds") or 0)
        return 0


class ProgramTotalRemainingSensor(_BaseSensor):
    """Seconds remaining across the whole program (or total runtime when idle)."""

    _attr_device_class = SensorDeviceClass.DURATION
    _attr_native_unit_of_measurement = "s"
    _attr_icon = "mdi:timer-sand"
    _attr_translation_key = "program_total_remaining"

    def __init__(
        self,
        coordinator: IrrigationCoordinator,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_program_total_remaining"
        self._attr_name = "Program Total Remaining"

    @property
    def native_value(self) -> int | None:
        return self._seq_data().get("total_remaining_seconds")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        seq = self._seq_data()
        return {
            "inter_zone_delay_seconds": seq.get("pause_seconds"),
            "depressurize_seconds": seq.get("depressurize_seconds"),
            "master_valve": seq.get("master_valve"),
        }


class NextStartSensor(_BaseSensor):
    """Next scheduled program start time, or unknown if not scheduled."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:calendar-clock"
    _attr_translation_key = "next_start"

    def __init__(
        self,
        coordinator: IrrigationCoordinator,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_next_start"
        self._attr_name = "Next Scheduled Start"

    @property
    def native_value(self) -> datetime | None:
        value = self._sched_data().get("next_fire")
        if not value:
            return None
        try:
            return datetime.fromisoformat(value)
        except (TypeError, ValueError):
            return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        sched = self._sched_data()
        return {"last_fire": sched.get("last_fire")}


class ZoneDurationSensor(_BaseSensor):
    """Per-zone configured duration in seconds."""

    _attr_device_class = SensorDeviceClass.DURATION
    _attr_native_unit_of_measurement = "s"
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
            f"{entry.entry_id}_{valve_entity_id}_duration"
        )
        self._attr_name = f"{zone.name} Duration"

    @property
    def native_value(self) -> int:
        zone = self.coordinator.zones_by_valve.get(self._valve_entity_id)
        if zone is None:
            return 0
        return zone.duration_seconds

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        zone = self.coordinator.zones_by_valve.get(self._valve_entity_id)
        if zone is None:
            return {}
        return {
            "duration_minutes": zone.duration_minutes,
            "valve_entity_id": self._valve_entity_id,
        }
