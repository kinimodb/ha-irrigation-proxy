"""Config flow for Irrigation Proxy.

v0.5.0 redesign:

* The initial **config flow** only asks for a program name and creates an
  entry with sensible defaults. All real configuration happens in the
  **options flow** afterwards.
* The **options flow** is menu-based (Irrigation v5 style). The user
  always lands on a menu where they can pick what to edit:

      Basics        – schedule (start times, weekdays), master valve
      Zones         – list / add / edit / remove zones
      Advanced      – inter-zone delay, depressurize delay, max runtime
      Save & Close  – persist everything, reload the entry

All edits are staged in `self._pending` and only written back to the
config entry when the user picks "Save & Close".
"""

from __future__ import annotations

import logging
import secrets
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    CONF_DEPRESSURIZE_SECONDS,
    CONF_INTER_ZONE_DELAY_SECONDS,
    CONF_LEAK_SENSORS,
    CONF_MASTER_VALVE,
    CONF_MAX_RUNTIME_MINUTES,
    CONF_NAME,
    CONF_SCHEDULE_ENABLED,
    CONF_SCHEDULE_START_TIMES,
    CONF_SCHEDULE_WEEKDAYS,
    CONF_WEATHER_FACTOR_SENSOR,
    CONF_ZONE_DURATION_MINUTES,
    CONF_ZONE_ID,
    CONF_ZONE_NAME,
    CONF_ZONE_VALVE,
    CONF_ZONES,
    DEFAULT_DEPRESSURIZE_SECONDS,
    DEFAULT_DURATION_MINUTES,
    DEFAULT_INTER_ZONE_DELAY_SECONDS,
    DEFAULT_MAX_RUNTIME_MINUTES,
    DEFAULT_SCHEDULE_ENABLED,
    DOMAIN,
    WEEKDAYS,
)
from .migration import migrate_v1_zones
from .scheduler import format_start_times, parse_start_times

_LOGGER = logging.getLogger(__name__)


# -- Selector helpers ------------------------------------------------------


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


def _depressurize_field() -> Any:
    return selector.NumberSelector(
        selector.NumberSelectorConfig(
            min=0,
            max=60,
            step=1,
            mode=selector.NumberSelectorMode.BOX,
            unit_of_measurement="s",
        )
    )


def _weekday_field() -> Any:
    # LIST mode renders one checkbox per option, which makes the field
    # idempotent across edits – the dropdown variant let users remove days
    # but not re-add them once the form was reopened with a populated default.
    return selector.SelectSelector(
        selector.SelectSelectorConfig(
            multiple=True,
            options=list(WEEKDAYS),
            mode=selector.SelectSelectorMode.LIST,
            translation_key="weekdays",
        )
    )


def _switch_entity_field() -> Any:
    return selector.EntitySelector(selector.EntitySelectorConfig())


def _leak_sensor_field() -> Any:
    """Entity selector restricted to binary_sensor, multiple selection."""
    return selector.EntitySelector(
        selector.EntitySelectorConfig(
            domain="binary_sensor",
            multiple=True,
        )
    )


def _weather_factor_sensor_field() -> Any:
    """Single sensor entity whose numeric state is used as runtime factor."""
    return selector.EntitySelector(
        selector.EntitySelectorConfig(domain="sensor")
    )


def _validate_start_times(raw: str | list[str] | None) -> list[str]:
    """Parse a schedule_start_times input to HH:MM strings."""
    parsed = parse_start_times(raw)
    return [t.strftime("%H:%M") for t in parsed]


# -- Default entry data ----------------------------------------------------


def _default_entry_data(name: str) -> dict[str, Any]:
    return {
        CONF_NAME: name,
        CONF_ZONES: [],
        CONF_MASTER_VALVE: None,
        CONF_SCHEDULE_ENABLED: DEFAULT_SCHEDULE_ENABLED,
        CONF_SCHEDULE_START_TIMES: [],
        CONF_SCHEDULE_WEEKDAYS: list(WEEKDAYS),
        CONF_INTER_ZONE_DELAY_SECONDS: DEFAULT_INTER_ZONE_DELAY_SECONDS,
        CONF_DEPRESSURIZE_SECONDS: DEFAULT_DEPRESSURIZE_SECONDS,
        CONF_MAX_RUNTIME_MINUTES: DEFAULT_MAX_RUNTIME_MINUTES,
        CONF_LEAK_SENSORS: [],
        CONF_WEATHER_FACTOR_SENSOR: None,
    }


# -- Config flow -----------------------------------------------------------


class IrrigationProxyConfigFlow(ConfigFlow, domain=DOMAIN):
    """Create a new irrigation program (just ask for a name)."""

    VERSION = 2

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> Any:
        """Ask for a program name, then create the entry with defaults."""
        if user_input is not None:
            await self.async_set_unique_id(
                user_input[CONF_NAME].lower().replace(" ", "_")
            )
            self._abort_if_unique_id_configured()

            return self.async_create_entry(
                title=user_input[CONF_NAME],
                data=_default_entry_data(user_input[CONF_NAME]),
            )

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {vol.Required(CONF_NAME, default="Irrigation"): str}
            ),
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Return the options flow handler."""
        return IrrigationProxyOptionsFlow(config_entry)


# -- Options flow (menu-based) ---------------------------------------------


class IrrigationProxyOptionsFlow(OptionsFlow):
    """Menu-based options flow – the main editing surface for the program."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        self._config_entry = config_entry
        # Deep-ish working copy; we only touch top-level keys + the zones list.
        merged = migrate_v1_zones({**config_entry.data, **config_entry.options})
        self._pending: dict[str, Any] = {
            **merged,
            CONF_ZONES: [
                dict(z) for z in (merged.get(CONF_ZONES) or [])
            ],
        }
        # Scratchpad for "edit a specific zone" sub-step.
        self._editing_zone_id: str | None = None

    # ---- Main menu ----------------------------------------------------

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> Any:
        """Top-level menu – this is what the user lands on."""
        return self.async_show_menu(
            step_id="init",
            menu_options=["basics", "zones", "advanced", "safety", "save"],
        )

    # ---- Basics -------------------------------------------------------

    async def async_step_basics(
        self, user_input: dict[str, Any] | None = None
    ) -> Any:
        """Schedule + master-valve settings."""
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                times = _validate_start_times(
                    user_input.get(CONF_SCHEDULE_START_TIMES, "")
                )
            except vol.Invalid:
                errors[CONF_SCHEDULE_START_TIMES] = "invalid_start_times"
                times = []

            if not errors:
                self._pending[CONF_SCHEDULE_ENABLED] = bool(
                    user_input.get(CONF_SCHEDULE_ENABLED, False)
                )
                self._pending[CONF_SCHEDULE_START_TIMES] = times
                self._pending[CONF_SCHEDULE_WEEKDAYS] = list(
                    user_input.get(CONF_SCHEDULE_WEEKDAYS, [])
                )
                master = user_input.get(CONF_MASTER_VALVE) or None
                self._pending[CONF_MASTER_VALVE] = master
                return await self.async_step_init()

        existing_times = self._pending.get(CONF_SCHEDULE_START_TIMES, [])
        schema_dict: dict[Any, Any] = {
            vol.Required(
                CONF_SCHEDULE_ENABLED,
                default=self._pending.get(
                    CONF_SCHEDULE_ENABLED, DEFAULT_SCHEDULE_ENABLED
                ),
            ): selector.BooleanSelector(),
            vol.Optional(
                CONF_SCHEDULE_START_TIMES,
                default=format_start_times(existing_times),
            ): selector.TextSelector(),
            vol.Optional(
                CONF_SCHEDULE_WEEKDAYS,
                default=self._pending.get(
                    CONF_SCHEDULE_WEEKDAYS, list(WEEKDAYS)
                ),
            ): _weekday_field(),
            vol.Optional(
                CONF_MASTER_VALVE,
                description={
                    "suggested_value": self._pending.get(CONF_MASTER_VALVE)
                    or ""
                },
            ): _switch_entity_field(),
        }

        return self.async_show_form(
            step_id="basics",
            data_schema=vol.Schema(schema_dict),
            errors=errors,
        )

    # ---- Zone list ----------------------------------------------------

    async def async_step_zones(
        self, user_input: dict[str, Any] | None = None
    ) -> Any:
        """Submenu: add a new zone or edit/remove an existing one."""
        options: dict[str, str] = {"zone_add": "Add new zone"}
        for zone in self._pending.get(CONF_ZONES) or []:
            label = zone.get(CONF_ZONE_NAME) or zone.get(CONF_ZONE_VALVE, "?")
            options[f"zone_edit_{zone[CONF_ZONE_ID]}"] = label
        options["init"] = "Back to main menu"

        return self.async_show_menu(
            step_id="zones",
            menu_options=options,
        )

    async def async_step_zone_add(
        self, user_input: dict[str, Any] | None = None
    ) -> Any:
        """Form: add a new zone to the end of the list."""
        errors: dict[str, str] = {}

        if user_input is not None:
            _LOGGER.debug("zone_add user_input received: %s", user_input)
            valve = user_input.get(CONF_ZONE_VALVE)
            if not valve:
                _LOGGER.debug(
                    "zone_add: valve_entity_id missing or empty in user_input"
                )
                errors[CONF_ZONE_VALVE] = "valve_required"
            else:
                new_zone = {
                    CONF_ZONE_ID: _new_zone_id(),
                    CONF_ZONE_NAME: user_input.get(CONF_ZONE_NAME) or valve,
                    CONF_ZONE_VALVE: valve,
                    CONF_ZONE_DURATION_MINUTES: int(
                        user_input.get(
                            CONF_ZONE_DURATION_MINUTES,
                            DEFAULT_DURATION_MINUTES,
                        )
                    ),
                }
                _LOGGER.debug("zone_add: created zone %s", new_zone)
                zones = list(self._pending.get(CONF_ZONES) or [])
                zones.append(new_zone)
                self._pending[CONF_ZONES] = zones
                return await self.async_step_zones()

        return self.async_show_form(
            step_id="zone_add",
            data_schema=vol.Schema(
                {
                    vol.Optional(CONF_ZONE_NAME, default=""): str,
                    vol.Optional(
                        CONF_ZONE_VALVE,
                        description={"suggested_value": ""},
                    ): _switch_entity_field(),
                    vol.Required(
                        CONF_ZONE_DURATION_MINUTES,
                        default=DEFAULT_DURATION_MINUTES,
                    ): _duration_field(),
                }
            ),
            errors=errors,
        )

    async def async_step_zone_edit(
        self, user_input: dict[str, Any] | None = None
    ) -> Any:
        """Form: edit an existing zone, or delete it."""
        # The menu routes to zone_edit_<id>; we capture the id via
        # `async_step_*` dispatch – see __getattr__ below.
        zone_id = self._editing_zone_id
        zones = list(self._pending.get(CONF_ZONES) or [])
        idx = next(
            (i for i, z in enumerate(zones) if z.get(CONF_ZONE_ID) == zone_id),
            -1,
        )
        if idx < 0:
            self._editing_zone_id = None
            return await self.async_step_zones()

        zone = zones[idx]
        errors: dict[str, str] = {}

        if user_input is not None:
            _LOGGER.debug("zone_edit user_input received: %s", user_input)
            if user_input.get("delete"):
                zones.pop(idx)
                self._pending[CONF_ZONES] = zones
                self._editing_zone_id = None
                return await self.async_step_zones()

            valve = user_input.get(CONF_ZONE_VALVE)
            if not valve:
                _LOGGER.debug(
                    "zone_edit: valve_entity_id missing or empty in user_input"
                )
                errors[CONF_ZONE_VALVE] = "valve_required"
            else:
                zones[idx] = {
                    CONF_ZONE_ID: zone_id,
                    CONF_ZONE_NAME: user_input.get(CONF_ZONE_NAME) or valve,
                    CONF_ZONE_VALVE: valve,
                    CONF_ZONE_DURATION_MINUTES: int(
                        user_input.get(
                            CONF_ZONE_DURATION_MINUTES,
                            DEFAULT_DURATION_MINUTES,
                        )
                    ),
                }
                self._pending[CONF_ZONES] = zones
                self._editing_zone_id = None
                return await self.async_step_zones()

        return self.async_show_form(
            step_id="zone_edit",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_ZONE_NAME,
                        default=zone.get(CONF_ZONE_NAME, ""),
                    ): str,
                    vol.Optional(
                        CONF_ZONE_VALVE,
                        description={"suggested_value": zone.get(CONF_ZONE_VALVE) or ""},
                    ): _switch_entity_field(),
                    vol.Required(
                        CONF_ZONE_DURATION_MINUTES,
                        default=int(
                            zone.get(
                                CONF_ZONE_DURATION_MINUTES,
                                DEFAULT_DURATION_MINUTES,
                            )
                        ),
                    ): _duration_field(),
                    vol.Optional("delete", default=False): selector.BooleanSelector(),
                }
            ),
            errors=errors,
            description_placeholders={
                "zone_name": zone.get(CONF_ZONE_NAME) or "?",
            },
        )

    # The zone menu generates dynamic step_ids like "zone_edit_<id>".
    # HA dispatches these as `async_step_zone_edit_<id>` – route them all
    # through `async_step_zone_edit` after stashing the id.
    def __getattr__(self, name: str):
        if name.startswith("async_step_zone_edit_"):
            zone_id = name[len("async_step_zone_edit_") :]

            async def _dispatch(
                user_input: dict[str, Any] | None = None,
            ) -> Any:
                self._editing_zone_id = zone_id
                return await self.async_step_zone_edit(user_input)

            return _dispatch
        raise AttributeError(name)

    # ---- Advanced -----------------------------------------------------

    async def async_step_advanced(
        self, user_input: dict[str, Any] | None = None
    ) -> Any:
        """Inter-zone pause, depressurize, max runtime, weather factor."""
        if user_input is not None:
            self._pending[CONF_INTER_ZONE_DELAY_SECONDS] = int(
                user_input.get(
                    CONF_INTER_ZONE_DELAY_SECONDS,
                    DEFAULT_INTER_ZONE_DELAY_SECONDS,
                )
            )
            self._pending[CONF_DEPRESSURIZE_SECONDS] = int(
                user_input.get(
                    CONF_DEPRESSURIZE_SECONDS, DEFAULT_DEPRESSURIZE_SECONDS
                )
            )
            self._pending[CONF_MAX_RUNTIME_MINUTES] = int(
                user_input.get(
                    CONF_MAX_RUNTIME_MINUTES, DEFAULT_MAX_RUNTIME_MINUTES
                )
            )
            weather_sensor = user_input.get(CONF_WEATHER_FACTOR_SENSOR) or None
            self._pending[CONF_WEATHER_FACTOR_SENSOR] = weather_sensor
            return await self.async_step_init()

        current_weather_sensor = (
            self._pending.get(CONF_WEATHER_FACTOR_SENSOR) or ""
        )

        return self.async_show_form(
            step_id="advanced",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_INTER_ZONE_DELAY_SECONDS,
                        default=self._pending.get(
                            CONF_INTER_ZONE_DELAY_SECONDS,
                            DEFAULT_INTER_ZONE_DELAY_SECONDS,
                        ),
                    ): _inter_zone_delay_field(),
                    vol.Required(
                        CONF_DEPRESSURIZE_SECONDS,
                        default=self._pending.get(
                            CONF_DEPRESSURIZE_SECONDS,
                            DEFAULT_DEPRESSURIZE_SECONDS,
                        ),
                    ): _depressurize_field(),
                    vol.Required(
                        CONF_MAX_RUNTIME_MINUTES,
                        default=self._pending.get(
                            CONF_MAX_RUNTIME_MINUTES,
                            DEFAULT_MAX_RUNTIME_MINUTES,
                        ),
                    ): _max_runtime_field(),
                    vol.Optional(
                        CONF_WEATHER_FACTOR_SENSOR,
                        description={"suggested_value": current_weather_sensor},
                    ): _weather_factor_sensor_field(),
                }
            ),
        )

    # ---- Safety (submenu) --------------------------------------------

    async def async_step_safety(
        self, user_input: dict[str, Any] | None = None
    ) -> Any:
        """Submenu for safety features. Currently only leak sensors,
        but structured so additional safety features can be added later
        without rearranging the main options menu."""
        return self.async_show_menu(
            step_id="safety",
            menu_options=["safety_leak_sensors", "init"],
        )

    async def async_step_safety_leak_sensors(
        self, user_input: dict[str, Any] | None = None
    ) -> Any:
        """Configure water-leak / water-shortage sensors that trigger a full shutdown."""
        if user_input is not None:
            raw = user_input.get(CONF_LEAK_SENSORS) or []
            if isinstance(raw, str):
                raw = [raw]
            sensors = [
                str(s) for s in raw if isinstance(s, str) and s
            ]
            self._pending[CONF_LEAK_SENSORS] = sensors
            return await self.async_step_safety()

        return self.async_show_form(
            step_id="safety_leak_sensors",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_LEAK_SENSORS,
                        default=self._pending.get(CONF_LEAK_SENSORS) or [],
                    ): _leak_sensor_field(),
                }
            ),
        )

    # ---- Save & close -------------------------------------------------

    async def async_step_save(
        self, user_input: dict[str, Any] | None = None
    ) -> Any:
        """Persist `_pending` onto the config entry and close the flow."""
        new_data = {**self._config_entry.data, **self._pending}
        # Preserve the original program name – the user cannot rename it here.
        new_data[CONF_NAME] = self._config_entry.data.get(
            CONF_NAME, new_data.get(CONF_NAME, "Irrigation")
        )
        self.hass.config_entries.async_update_entry(
            self._config_entry, data=new_data
        )
        return self.async_create_entry(data={})


def _new_zone_id() -> str:
    """Return a short stable id for a zone."""
    return f"z_{secrets.token_hex(4)}"
