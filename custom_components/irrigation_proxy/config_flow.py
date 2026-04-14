"""Config flow for Irrigation Proxy."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    CONF_DURATION_MINUTES,
    CONF_INTER_ZONE_DELAY_SECONDS,
    CONF_MAX_RUNTIME_MINUTES,
    CONF_NAME,
    CONF_RAIN_ADJUST_MODE,
    CONF_RAIN_THRESHOLD_MM,
    CONF_SCHEDULE_ENABLED,
    CONF_SCHEDULE_START_TIMES,
    CONF_SCHEDULE_WEEKDAYS,
    CONF_ZONE_DURATIONS,
    CONF_ZONES,
    DEFAULT_DURATION_MINUTES,
    DEFAULT_MAX_RUNTIME_MINUTES,
    DEFAULT_PAUSE_BETWEEN_ZONES_SECONDS,
    DEFAULT_RAIN_ADJUST_MODE,
    DEFAULT_RAIN_THRESHOLD_MM,
    DEFAULT_SCHEDULE_ENABLED,
    DOMAIN,
    RAIN_ADJUST_MODES,
    WEEKDAYS,
)
from .scheduler import format_start_times, parse_start_times


def _duration_field() -> Any:
    return selector.NumberSelector(
        selector.NumberSelectorConfig(
            min=1,
            max=120,
            step=1,
            mode=selector.NumberSelectorMode.BOX,
            unit_of_measurement="min",
        )
    )


def _rain_threshold_field() -> Any:
    return selector.NumberSelector(
        selector.NumberSelectorConfig(
            min=1.0,
            max=50.0,
            step=0.5,
            mode=selector.NumberSelectorMode.BOX,
            unit_of_measurement="mm",
        )
    )


def _max_runtime_field() -> Any:
    return selector.NumberSelector(
        selector.NumberSelectorConfig(
            min=5,
            max=180,
            step=5,
            mode=selector.NumberSelectorMode.BOX,
            unit_of_measurement="min",
        )
    )


def _inter_zone_delay_field() -> Any:
    return selector.NumberSelector(
        selector.NumberSelectorConfig(
            min=0,
            max=600,
            step=5,
            mode=selector.NumberSelectorMode.BOX,
            unit_of_measurement="s",
        )
    )


def _weekday_field() -> Any:
    return selector.SelectSelector(
        selector.SelectSelectorConfig(
            multiple=True,
            options=list(WEEKDAYS),
            mode=selector.SelectSelectorMode.DROPDOWN,
            translation_key="weekdays",
        )
    )


def _rain_adjust_field() -> Any:
    return selector.SelectSelector(
        selector.SelectSelectorConfig(
            multiple=False,
            options=list(RAIN_ADJUST_MODES),
            mode=selector.SelectSelectorMode.DROPDOWN,
            translation_key="rain_adjust_mode",
        )
    )


def _validate_start_times(raw: str) -> list[str]:
    """Return HH:MM strings parsed from raw input, or raise vol.Invalid."""
    if raw is None:
        return []
    parsed = parse_start_times(raw)
    return [t.strftime("%H:%M") for t in parsed]


class IrrigationProxyConfigFlow(ConfigFlow, domain=DOMAIN):
    """Multi-step config flow: name → zones → safety → schedule."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._user_input: dict[str, Any] = {}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> Any:
        """Step 1: Program name."""
        if user_input is not None:
            self._user_input.update(user_input)

            # Set unique ID based on name to prevent duplicates
            await self.async_set_unique_id(
                user_input[CONF_NAME].lower().replace(" ", "_")
            )
            self._abort_if_unique_id_configured()

            return await self.async_step_zones()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_NAME, default="Irrigation"): str,
                }
            ),
        )

    async def async_step_zones(
        self, user_input: dict[str, Any] | None = None
    ) -> Any:
        """Step 2: Select valve entities and default duration."""
        errors: dict[str, str] = {}

        if user_input is not None:
            zones = user_input.get(CONF_ZONES, [])
            if not zones:
                errors["base"] = "no_zones_selected"
            else:
                self._user_input.update(user_input)
                return await self.async_step_safety()

        return self.async_show_form(
            step_id="zones",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_ZONES): selector.EntitySelector(
                        selector.EntitySelectorConfig(
                            domain="switch",
                            multiple=True,
                        )
                    ),
                    vol.Required(
                        CONF_DURATION_MINUTES,
                        default=DEFAULT_DURATION_MINUTES,
                    ): _duration_field(),
                }
            ),
            errors=errors,
        )

    async def async_step_safety(
        self, user_input: dict[str, Any] | None = None
    ) -> Any:
        """Step 3: Safety settings."""
        if user_input is not None:
            self._user_input.update(user_input)
            return await self.async_step_schedule()

        return self.async_show_form(
            step_id="safety",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_MAX_RUNTIME_MINUTES,
                        default=DEFAULT_MAX_RUNTIME_MINUTES,
                    ): _max_runtime_field(),
                    vol.Required(
                        CONF_RAIN_THRESHOLD_MM,
                        default=DEFAULT_RAIN_THRESHOLD_MM,
                    ): _rain_threshold_field(),
                }
            ),
        )

    async def async_step_schedule(
        self, user_input: dict[str, Any] | None = None
    ) -> Any:
        """Step 4: Schedule (optional)."""
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                user_input[CONF_SCHEDULE_START_TIMES] = _validate_start_times(
                    user_input.get(CONF_SCHEDULE_START_TIMES, "")
                )
            except vol.Invalid:
                errors[CONF_SCHEDULE_START_TIMES] = "invalid_start_times"

            if not errors:
                self._user_input.update(user_input)
                return self.async_create_entry(
                    title=self._user_input[CONF_NAME],
                    data=self._user_input,
                )

        return self.async_show_form(
            step_id="schedule",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_SCHEDULE_ENABLED,
                        default=DEFAULT_SCHEDULE_ENABLED,
                    ): selector.BooleanSelector(),
                    vol.Optional(
                        CONF_SCHEDULE_START_TIMES,
                        default="",
                    ): selector.TextSelector(),
                    vol.Optional(
                        CONF_SCHEDULE_WEEKDAYS,
                        default=list(WEEKDAYS),
                    ): _weekday_field(),
                    vol.Required(
                        CONF_INTER_ZONE_DELAY_SECONDS,
                        default=DEFAULT_PAUSE_BETWEEN_ZONES_SECONDS,
                    ): _inter_zone_delay_field(),
                    vol.Required(
                        CONF_RAIN_ADJUST_MODE,
                        default=DEFAULT_RAIN_ADJUST_MODE,
                    ): _rain_adjust_field(),
                }
            ),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Return the options flow handler."""
        return IrrigationProxyOptionsFlow(config_entry)


class IrrigationProxyOptionsFlow(OptionsFlow):
    """Multi-step options flow: zones/safety → per-zone durations → schedule."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        """Initialize options flow."""
        self._config_entry = config_entry
        self._pending: dict[str, Any] = {}

    # ---- Step 1: zones + safety ---------------------------------------

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> Any:
        """Show basic options (zones, default duration, safety)."""
        errors: dict[str, str] = {}
        current = {**self._config_entry.data, **self._config_entry.options}

        if user_input is not None:
            zones = user_input.get(CONF_ZONES, [])
            if not zones:
                errors["base"] = "no_zones_selected"
            else:
                self._pending.update(user_input)
                return await self.async_step_durations()

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_ZONES,
                        default=current.get(CONF_ZONES, []),
                    ): selector.EntitySelector(
                        selector.EntitySelectorConfig(
                            domain="switch",
                            multiple=True,
                        )
                    ),
                    vol.Required(
                        CONF_DURATION_MINUTES,
                        default=current.get(
                            CONF_DURATION_MINUTES, DEFAULT_DURATION_MINUTES
                        ),
                    ): _duration_field(),
                    vol.Required(
                        CONF_MAX_RUNTIME_MINUTES,
                        default=current.get(
                            CONF_MAX_RUNTIME_MINUTES,
                            DEFAULT_MAX_RUNTIME_MINUTES,
                        ),
                    ): _max_runtime_field(),
                    vol.Required(
                        CONF_RAIN_THRESHOLD_MM,
                        default=current.get(
                            CONF_RAIN_THRESHOLD_MM,
                            DEFAULT_RAIN_THRESHOLD_MM,
                        ),
                    ): _rain_threshold_field(),
                }
            ),
            errors=errors,
        )

    # ---- Step 2: per-zone durations -----------------------------------

    async def async_step_durations(
        self, user_input: dict[str, Any] | None = None
    ) -> Any:
        """Show one duration slider per currently-selected zone."""
        current = {**self._config_entry.data, **self._config_entry.options}
        selected_zones: list[str] = self._pending.get(CONF_ZONES, [])
        stored_overrides: dict[str, Any] = current.get(CONF_ZONE_DURATIONS, {}) or {}
        default_minutes = int(
            self._pending.get(
                CONF_DURATION_MINUTES,
                current.get(CONF_DURATION_MINUTES, DEFAULT_DURATION_MINUTES),
            )
        )

        if user_input is not None:
            overrides: dict[str, int] = {}
            for valve_id in selected_zones:
                key = _zone_duration_key(valve_id)
                if key in user_input:
                    overrides[valve_id] = int(user_input[key])
            self._pending[CONF_ZONE_DURATIONS] = overrides
            return await self.async_step_schedule()

        schema: dict[Any, Any] = {}
        for valve_id in selected_zones:
            default_val = int(stored_overrides.get(valve_id, default_minutes))
            schema[
                vol.Required(
                    _zone_duration_key(valve_id), default=default_val
                )
            ] = _duration_field()

        return self.async_show_form(
            step_id="durations",
            data_schema=vol.Schema(schema),
            description_placeholders={
                "zone_count": str(len(selected_zones)),
            },
        )

    # ---- Step 3: schedule ---------------------------------------------

    async def async_step_schedule(
        self, user_input: dict[str, Any] | None = None
    ) -> Any:
        """Show the schedule settings and persist everything."""
        errors: dict[str, str] = {}
        current = {**self._config_entry.data, **self._config_entry.options}

        if user_input is not None:
            try:
                user_input[CONF_SCHEDULE_START_TIMES] = _validate_start_times(
                    user_input.get(CONF_SCHEDULE_START_TIMES, "")
                )
            except vol.Invalid:
                errors[CONF_SCHEDULE_START_TIMES] = "invalid_start_times"

            if not errors:
                self._pending.update(user_input)
                # Persist: keep name in data, write everything else to data
                # (we don't split into options here for v0.4.0 simplicity – a
                # reload is triggered by the update listener anyway).
                new_data = {**self._config_entry.data, **self._pending}
                self.hass.config_entries.async_update_entry(
                    self._config_entry, data=new_data
                )
                return self.async_create_entry(data={})

        existing_times = current.get(CONF_SCHEDULE_START_TIMES, [])
        default_times = format_start_times(existing_times)

        return self.async_show_form(
            step_id="schedule",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_SCHEDULE_ENABLED,
                        default=current.get(
                            CONF_SCHEDULE_ENABLED, DEFAULT_SCHEDULE_ENABLED
                        ),
                    ): selector.BooleanSelector(),
                    vol.Optional(
                        CONF_SCHEDULE_START_TIMES,
                        default=default_times,
                    ): selector.TextSelector(),
                    vol.Optional(
                        CONF_SCHEDULE_WEEKDAYS,
                        default=current.get(
                            CONF_SCHEDULE_WEEKDAYS, list(WEEKDAYS)
                        ),
                    ): _weekday_field(),
                    vol.Required(
                        CONF_INTER_ZONE_DELAY_SECONDS,
                        default=current.get(
                            CONF_INTER_ZONE_DELAY_SECONDS,
                            DEFAULT_PAUSE_BETWEEN_ZONES_SECONDS,
                        ),
                    ): _inter_zone_delay_field(),
                    vol.Required(
                        CONF_RAIN_ADJUST_MODE,
                        default=current.get(
                            CONF_RAIN_ADJUST_MODE, DEFAULT_RAIN_ADJUST_MODE
                        ),
                    ): _rain_adjust_field(),
                }
            ),
            errors=errors,
        )


def _zone_duration_key(valve_entity_id: str) -> str:
    """Translate a valve entity_id into a stable form-field key."""
    return f"duration__{valve_entity_id}"
