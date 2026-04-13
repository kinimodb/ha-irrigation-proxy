"""Config flow for Irrigation Proxy."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    CONF_DURATION_MINUTES,
    CONF_MAX_RUNTIME_MINUTES,
    CONF_NAME,
    CONF_ZONES,
    DEFAULT_DURATION_MINUTES,
    DEFAULT_MAX_RUNTIME_MINUTES,
    DOMAIN,
)


class IrrigationProxyConfigFlow(ConfigFlow, domain=DOMAIN):
    """Multi-step config flow: name → zones → safety."""

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
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=1,
                            max=120,
                            step=1,
                            mode=selector.NumberSelectorMode.BOX,
                            unit_of_measurement="min",
                        )
                    ),
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

            return self.async_create_entry(
                title=self._user_input[CONF_NAME],
                data=self._user_input,
            )

        return self.async_show_form(
            step_id="safety",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_MAX_RUNTIME_MINUTES,
                        default=DEFAULT_MAX_RUNTIME_MINUTES,
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=5,
                            max=180,
                            step=5,
                            mode=selector.NumberSelectorMode.BOX,
                            unit_of_measurement="min",
                        )
                    ),
                }
            ),
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Return the options flow handler."""
        return IrrigationProxyOptionsFlow(config_entry)


class IrrigationProxyOptionsFlow(OptionsFlow):
    """Options flow to modify zones and safety settings after setup."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        """Initialize options flow."""
        self._config_entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> Any:
        """Show options form."""
        errors: dict[str, str] = {}

        if user_input is not None:
            zones = user_input.get(CONF_ZONES, [])
            if not zones:
                errors["base"] = "no_zones_selected"
            else:
                # Merge new options with existing data
                new_data = {**self._config_entry.data, **user_input}
                self.hass.config_entries.async_update_entry(
                    self._config_entry, data=new_data
                )
                return self.async_create_entry(data={})

        current_data = self._config_entry.data

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_ZONES,
                        default=current_data.get(CONF_ZONES, []),
                    ): selector.EntitySelector(
                        selector.EntitySelectorConfig(
                            domain="switch",
                            multiple=True,
                        )
                    ),
                    vol.Required(
                        CONF_DURATION_MINUTES,
                        default=current_data.get(
                            CONF_DURATION_MINUTES, DEFAULT_DURATION_MINUTES
                        ),
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=1,
                            max=120,
                            step=1,
                            mode=selector.NumberSelectorMode.BOX,
                            unit_of_measurement="min",
                        )
                    ),
                    vol.Required(
                        CONF_MAX_RUNTIME_MINUTES,
                        default=current_data.get(
                            CONF_MAX_RUNTIME_MINUTES,
                            DEFAULT_MAX_RUNTIME_MINUTES,
                        ),
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=5,
                            max=180,
                            step=5,
                            mode=selector.NumberSelectorMode.BOX,
                            unit_of_measurement="min",
                        )
                    ),
                }
            ),
            errors=errors,
        )
