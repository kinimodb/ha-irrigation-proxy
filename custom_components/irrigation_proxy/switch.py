"""Switch entities for per-zone manual control and program start/stop."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.switch import SwitchDeviceClass, SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    CONF_IGNORE_WEATHER_ADJUSTMENT,
    CONF_NAME,
    CONF_SCHEDULE_ENABLED,
    DEFAULT_SCHEDULE_ENABLED,
    DOMAIN,
)
from .coordinator import IrrigationCoordinator
from .sequencer import SequencerState

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
        ScheduleEnabledSwitch(coordinator, entry),
        IgnoreWeatherAdjustmentSwitch(coordinator, entry),
    ]
    entities.extend(
        ZoneSwitch(coordinator, entry, zone.valve_entity_id)
        for zone in coordinator.zones
    )
    if coordinator.sequencer.master_valve:
        entities.append(MasterValveSwitch(coordinator, entry))
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
        self._attr_name = "Program (Manual Start/Stop)"

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
            "depressurize_seconds": seq.get("depressurize_seconds"),
            "master_valve": seq.get("master_valve"),
            "zones": seq.get("zones", []),
            "next_scheduled_start": sched.get("next_fire"),
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
        if self.is_on:
            _LOGGER.info("Program switch removed while running – stopping program")
            await self.coordinator.sequencer.stop()
        await super().async_will_remove_from_hass()


class ScheduleEnabledSwitch(CoordinatorEntity[IrrigationCoordinator], SwitchEntity):
    """Dashboard toggle for the automatic schedule.

    Mirrors ``CONF_SCHEDULE_ENABLED`` from the options flow so the user
    can pause / resume the weekly schedule without opening the config
    dialog. Flipping the switch re-registers the underlying time
    triggers via ``scheduler.reload()`` – no config-entry reload, so a
    running program is not interrupted.
    """

    _attr_has_entity_name = True
    _attr_device_class = SwitchDeviceClass.SWITCH
    _attr_icon = "mdi:calendar-clock"
    _attr_translation_key = "schedule_enabled"

    def __init__(
        self,
        coordinator: IrrigationCoordinator,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_schedule_enabled"
        self._attr_name = "Automatic Schedule"

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
        raw = {**self._entry.data, **self._entry.options}
        return bool(raw.get(CONF_SCHEDULE_ENABLED, DEFAULT_SCHEDULE_ENABLED))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        sched = self.coordinator.data.get("scheduler") if self.coordinator.data else None
        return {
            "next_scheduled_start": (sched or {}).get("next_fire"),
            "last_scheduled_fire": (sched or {}).get("last_fire"),
        }

    async def _set_enabled(self, enabled: bool) -> None:
        # Skip the options-update listener's config-entry reload (would
        # tear down a running program). The scheduler picks the new
        # value up via its own reload() call below.
        self.coordinator.suppress_next_reload = True
        new_data = {**self._entry.data, CONF_SCHEDULE_ENABLED: enabled}
        self.hass.config_entries.async_update_entry(self._entry, data=new_data)
        if self.coordinator.scheduler is not None:
            self.coordinator.scheduler.reload()
        await self.coordinator.async_request_refresh()
        self.async_write_ha_state()

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._set_enabled(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._set_enabled(False)


class IgnoreWeatherAdjustmentSwitch(
    CoordinatorEntity[IrrigationCoordinator], SwitchEntity
):
    """Dashboard toggle that bypasses the weather-based runtime factor.

    OFF (default) → the configured factor sensor shortens or extends the
    per-zone runtime. ON → the factor is ignored and every zone runs its
    full configured duration. The switch state is persisted in
    ``entry.data`` so it survives a Home Assistant restart.
    """

    _attr_has_entity_name = True
    _attr_device_class = SwitchDeviceClass.SWITCH
    _attr_icon = "mdi:weather-sunny-off"
    _attr_translation_key = "ignore_weather_adjustment"

    def __init__(
        self,
        coordinator: IrrigationCoordinator,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_ignore_weather"
        self._attr_name = "Ignore Weather Adjustment"

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
        return bool(self.coordinator.ignore_weather)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "weather_factor": self.coordinator.weather_factor,
            "source_entity": self.coordinator.weather_factor_sensor,
        }

    async def _set_enabled(self, enabled: bool) -> None:
        # Live-tunable – avoid a full config-entry reload which would stop
        # a running program.
        self.coordinator.suppress_next_reload = True
        self.coordinator.ignore_weather = enabled
        new_data = {**self._entry.data, CONF_IGNORE_WEATHER_ADJUSTMENT: enabled}
        self.hass.config_entries.async_update_entry(self._entry, data=new_data)
        await self.coordinator.async_request_refresh()
        self.async_write_ha_state()

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._set_enabled(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._set_enabled(False)


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
        super().__init__(coordinator)
        self._entry = entry
        self._valve_entity_id = valve_entity_id
        self._zone = coordinator.zones_by_valve[valve_entity_id]

        self._attr_unique_id = f"{entry.entry_id}_{valve_entity_id}"
        self._attr_name = self._zone.name

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._entry.entry_id)},
            name=self._entry.data.get(CONF_NAME, "Irrigation"),
            manufacturer="Irrigation Proxy",
            model="Virtual Irrigation Controller",
        )

    @property
    def is_on(self) -> bool | None:
        """Return whether the zone valve is currently on."""
        ha_state = self.hass.states.get(self._valve_entity_id) if self.hass else None
        if ha_state is not None and ha_state.state in ("on", "off", "open", "closed"):
            return ha_state.state in ("on", "open")

        if self.coordinator.data is None:
            return None
        zone_data = self.coordinator.data.get(self._valve_entity_id)
        if zone_data is None:
            return None
        return zone_data["is_on"]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attrs: dict[str, Any] = {"valve_entity_id": self._valve_entity_id}
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
        if self._zone.is_on:
            _LOGGER.info(
                "Zone '%s' removed while on – closing valve", self._zone.name
            )
            await self._zone.turn_off(self.hass)
            self.coordinator.safety.cancel_deadman(self._valve_entity_id)
        await super().async_will_remove_from_hass()


class MasterValveSwitch(CoordinatorEntity[IrrigationCoordinator], SwitchEntity):
    """Manual override for the master / pump valve.

    Mirrors the underlying valve state so the user can confirm the actual
    line state at a glance. Manual toggling is only allowed while the
    sequencer is idle – during a program run the sequencer owns the valve.
    Every manual open arms a deadman so a forgotten test cannot leave the
    line under pressure indefinitely.
    """

    _attr_has_entity_name = True
    _attr_device_class = SwitchDeviceClass.SWITCH
    _attr_icon = "mdi:water-pump"
    _attr_translation_key = "master_valve"

    def __init__(
        self,
        coordinator: IrrigationCoordinator,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._master_entity_id = coordinator.sequencer.master_valve or ""
        self._attr_unique_id = f"{entry.entry_id}_master_valve"
        self._attr_name = "Master Valve"

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._entry.entry_id)},
            name=self._entry.data.get(CONF_NAME, "Irrigation"),
            manufacturer="Irrigation Proxy",
            model="Virtual Irrigation Controller",
        )

    @property
    def is_on(self) -> bool | None:
        if not self._master_entity_id:
            return None
        ha_state = (
            self.hass.states.get(self._master_entity_id) if self.hass else None
        )
        if ha_state is None:
            return None
        return ha_state.state in ("on", "open")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "master_entity_id": self._master_entity_id,
            "deadman_remaining_seconds": (
                self.coordinator.safety.master_remaining_seconds()
            ),
        }

    def _refuse_if_program_running(self) -> None:
        if self.coordinator.sequencer.state == SequencerState.RUNNING:
            raise HomeAssistantError(
                "Cannot toggle the master valve manually while a program "
                "is running – stop the program first."
            )

    async def async_turn_on(self, **kwargs: Any) -> None:
        self._refuse_if_program_running()

        result = await self.coordinator.sequencer._open_master()
        if result is False:
            raise HomeAssistantError(
                f"Master valve {self._master_entity_id} did not verify open."
            )

        async def _close_on_deadman() -> None:
            await self.coordinator.sequencer._close_master()
            await self.coordinator.async_request_refresh()

        self.coordinator.safety.start_master_deadman(
            self._master_entity_id, _close_on_deadman
        )
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs: Any) -> None:
        self._refuse_if_program_running()
        self.coordinator.safety.cancel_master_deadman()
        await self.coordinator.sequencer._close_master()
        await self.coordinator.async_request_refresh()

    async def async_will_remove_from_hass(self) -> None:
        if self.is_on:
            _LOGGER.info(
                "Master valve switch removed while open – closing valve"
            )
            self.coordinator.safety.cancel_master_deadman()
            try:
                await self.coordinator.sequencer._close_master()
            except Exception:
                _LOGGER.exception(
                    "Master valve switch: failed to close master on remove"
                )
        await super().async_will_remove_from_hass()
