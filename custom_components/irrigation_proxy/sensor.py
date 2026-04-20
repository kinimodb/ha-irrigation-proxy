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


def _format_mmss(seconds: int | None) -> str | None:
    """Render *seconds* as ``MM:SS`` (or ``H:MM:SS`` when ≥ 1 h).

    Returns ``None`` for ``None`` so HA renders ``unknown`` rather than
    a zero-padded placeholder. Negatives are clamped to 0.
    """
    if seconds is None:
        return None
    s = max(0, int(seconds))
    if s >= 3600:
        h, rem = divmod(s, 3600)
        m, sec = divmod(rem, 60)
        return f"{h}:{m:02d}:{sec:02d}"
    m, sec = divmod(s, 60)
    return f"{m:02d}:{sec:02d}"


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
        DepressurizeRemainingSensor(coordinator, entry),
        PauseRemainingSensor(coordinator, entry),
        ProgramTotalRemainingSensor(coordinator, entry),
        NextStartSensor(coordinator, entry),
    ]

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
    """Shows the active sequencer phase: idle / running / depressurizing / pausing."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["idle", "running", "depressurizing", "pausing"]
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
        seq = self._seq_data()
        return seq.get("phase") or seq.get("state") or "idle"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        seq = self._seq_data()
        return {
            "state": seq.get("state", "idle"),
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
        seq = self._seq_data()
        current = seq.get("current_zone")
        if current:
            return current
        # Idle (or running between zones) – preview the upcoming zone so the
        # sensor never reads `unknown` while the rest of the program state
        # is already known.
        next_zone = seq.get("next_zone")
        if next_zone:
            return next_zone
        zones = seq.get("zones") or []
        if zones:
            return zones[0].get("name")
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        seq = self._seq_data()
        return {
            "next_zone": seq.get("next_zone"),
            "current_zone_entity_id": seq.get("current_zone_entity_id"),
        }


class ZoneTimeRemainingSensor(_BaseSensor):
    """Zone countdown as ``MM:SS``; idle fallback = first zone's duration."""

    # No device_class / unit: HA renders DURATION sensors as raw seconds,
    # which is exactly what we want to get rid of. The raw integer is
    # still exposed via the `seconds_remaining` attribute so automations
    # can keep doing numeric comparisons.
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

    def _raw_seconds(self) -> int:
        seq = self._seq_data()
        if seq.get("state") == "running":
            remaining = seq.get("remaining_zone_seconds")
            return 0 if remaining is None else int(remaining)

        zones = seq.get("zones") or []
        if zones:
            return int(zones[0].get("duration_seconds") or 0)
        return 0

    @property
    def native_value(self) -> str | None:
        return _format_mmss(self._raw_seconds())

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"seconds_remaining": self._raw_seconds()}


class ProgramTotalRemainingSensor(_BaseSensor):
    """Whole-program countdown as ``MM:SS`` (or ``H:MM:SS`` when ≥ 1 h)."""

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

    def _raw_seconds(self) -> int | None:
        value = self._seq_data().get("total_remaining_seconds")
        return None if value is None else int(value)

    @property
    def native_value(self) -> str | None:
        return _format_mmss(self._raw_seconds())

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        seq = self._seq_data()
        # Breakdown so users can trace how the total is composed:
        #   total ≈ zones_remaining + pauses_remaining + depressurize_remaining
        return {
            "seconds_remaining": self._raw_seconds(),
            "zones_remaining_seconds": seq.get("zones_total_remaining_seconds"),
            "pauses_remaining_seconds": seq.get("pauses_total_remaining_seconds"),
            "depressurize_remaining_seconds": (
                seq.get("depressurize_total_remaining_seconds")
            ),
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


class DepressurizeRemainingSensor(_BaseSensor):
    """Sum of master-valve drain time still ahead in the program."""

    _attr_device_class = SensorDeviceClass.DURATION
    _attr_native_unit_of_measurement = "s"
    _attr_icon = "mdi:water-pump-off"
    _attr_translation_key = "depressurize_remaining"

    def __init__(
        self,
        coordinator: IrrigationCoordinator,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_depressurize_remaining"
        self._attr_name = "Depressurize Total Remaining"

    @property
    def native_value(self) -> int | None:
        return int(self._seq_data().get("depressurize_total_remaining_seconds") or 0)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        seq = self._seq_data()
        return {
            "current_phase_remaining_seconds": (
                seq.get("depressurize_remaining_seconds")
            ),
        }


class PauseRemainingSensor(_BaseSensor):
    """Sum of inter-zone pause time still ahead in the program."""

    _attr_device_class = SensorDeviceClass.DURATION
    _attr_native_unit_of_measurement = "s"
    _attr_icon = "mdi:timer-pause"
    _attr_translation_key = "pause_remaining"

    def __init__(
        self,
        coordinator: IrrigationCoordinator,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_pause_remaining"
        self._attr_name = "Pauses Total Remaining"

    @property
    def native_value(self) -> int | None:
        return int(self._seq_data().get("pauses_total_remaining_seconds") or 0)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        seq = self._seq_data()
        return {
            "current_phase_remaining_seconds": seq.get("pause_remaining_seconds"),
        }
