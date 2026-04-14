"""Switch entities for per-zone manual control and program start/stop."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.switch import SwitchDeviceClass, SwitchEntity
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
    """Set up switch entities from a config entry."""
    coordinator: IrrigationCoordinator = hass.data[DOMAIN][entry.entry_id]

    entities: list[SwitchEntity] = [
        ProgramSwitch(coordinator, entry),
    ]
    entities.extend(
        ZoneSwitch(coordinator, entry, valve_id)
        for valve_id in coordinator.zones
    )
    async_add_entities(entities)


class ProgramSwitch(CoordinatorEntity[IrrigationCoordinator], SwitchEntity):
    """Switch to start/stop the sequencer program."""

    _attr_has_entity_name = True
    _attr_device_class = SwitchDeviceClass.SWITCH
    _attr_icon = "mdi:sprinkler-variant"
    _attr_translation_key = "program"

    def __init__(
        self,
        coordinator: IrrigationCoordinator,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_program"
        self._attr_name = "Program"

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
        if self.coordinator.data is None:
            return False
        seq = self.coordinator.data.get("sequencer", {})
        return seq.get("state") == "running"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        if self.coordinator.data is None:
            return {}
        seq = self.coordinator.data.get("sequencer", {})
        sched = self.coordinator.data.get("scheduler", {})
        return {
            "current_zone": seq.get("current_zone"),
            "total_zones": seq.get("total_zones", 0),
            "current_zone_index": seq.get("current_zone_index", -1),
            "remaining_zone_seconds": seq.get("remaining_zone_seconds"),
            "total_remaining_seconds": seq.get("total_remaining_seconds"),
            "inter_zone_delay_seconds": seq.get("pause_seconds"),
            "duration_multiplier": seq.get("duration_multiplier"),
            "zones": seq.get("zones", []),
            "next_scheduled_start": sched.get("next_fire"),
            "last_skip_reason": sched.get("last_skip_reason"),
        }

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Start the irrigation program."""
        await self.coordinator.sequencer.start()
        self.coordinator.notify_sequencer_state_changed()
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Stop the irrigation program."""
        await self.coordinator.sequencer.stop()
        self.coordinator.notify_sequencer_state_changed()
        await self.coordinator.async_request_refresh()

    async def async_will_remove_from_hass(self) -> None:
        """Stop program when entity is removed."""
        if self.is_on:
            _LOGGER.info("Program switch removed while running – stopping program")
            await self.coordinator.sequencer.stop()
        await super().async_will_remove_from_hass()


class ZoneSwitch(CoordinatorEntity[IrrigationCoordinator], SwitchEntity):
    """Switch entity for a single irrigation zone."""

    _attr_has_entity_name = True
    _attr_device_class = SwitchDeviceClass.SWITCH

    def __init__(
        self,
        coordinator: IrrigationCoordinator,
        entry: ConfigEntry,
        valve_entity_id: str,
    ) -> None:
        """Initialize the zone switch."""
        super().__init__(coordinator)
        self._entry = entry
        self._valve_entity_id = valve_entity_id
        self._zone = coordinator.zones[valve_entity_id]

        self._attr_unique_id = f"{entry.entry_id}_{valve_entity_id}"
        self._attr_name = self._zone.name

    @property
    def device_info(self) -> DeviceInfo:
        """Group all zone switches under one device per config entry."""
        return DeviceInfo(
            identifiers={(DOMAIN, self._entry.entry_id)},
            name=self._entry.data.get(CONF_NAME, "Irrigation"),
            manufacturer="Irrigation Proxy",
            model="Virtual Irrigation Controller",
        )

    @property
    def is_on(self) -> bool | None:
        """Return whether the zone valve is currently on.

        Reads first from the live HA state of the underlying valve (pushed
        by the coordinator's state_change listener within ~1 s) and falls
        back to the cached coordinator snapshot.
        """
        # Live underlying state – reflects reality immediately once HA sees it.
        ha_state = self.hass.states.get(self._valve_entity_id) if self.hass else None
        if ha_state is not None and ha_state.state in ("on", "off"):
            return ha_state.state == "on"

        if self.coordinator.data is None:
            return None
        zone_data = self.coordinator.data.get(self._valve_entity_id)
        if zone_data is None:
            return None
        return zone_data["is_on"]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose debugging attributes."""
        attrs: dict[str, Any] = {
            "valve_entity_id": self._valve_entity_id,
        }
        if self.coordinator.data is not None:
            zone_data = self.coordinator.data.get(self._valve_entity_id, {})
            attrs["state_mismatch"] = zone_data.get("state_mismatch", False)
            attrs["remaining_seconds"] = zone_data.get("remaining_seconds")
            attrs["duration_minutes"] = zone_data.get("duration_minutes")
            attrs["duration_seconds"] = (
                int(zone_data["duration_minutes"] * 60)
                if zone_data.get("duration_minutes") is not None
                else None
            )
        return attrs

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn on the valve and start the deadman timer."""
        await self._zone.turn_on(self.hass)
        self.coordinator.safety.start_deadman(self._zone)
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn off the valve and cancel the deadman timer."""
        await self._zone.turn_off(self.hass)
        self.coordinator.safety.cancel_deadman(self._valve_entity_id)
        await self.coordinator.async_request_refresh()

    async def async_will_remove_from_hass(self) -> None:
        """Close valve when entity is removed."""
        if self._zone.is_on:
            _LOGGER.info(
                "Zone '%s' removed while on – closing valve", self._zone.name
            )
            await self._zone.turn_off(self.hass)
            self.coordinator.safety.cancel_deadman(self._valve_entity_id)
        await super().async_will_remove_from_hass()
