"""Root conftest: patch sys.modules to mock homeassistant dependencies.

This allows running unit tests for zone.py and safety.py without
installing the full homeassistant package and all its native deps.
We only mock the specific HA modules our code actually imports.
"""

from __future__ import annotations

import sys
from types import ModuleType
from unittest.mock import MagicMock

# Create minimal stub modules for homeassistant imports.
# Our code uses:
#   - homeassistant.const (STATE_ON)
#   - homeassistant.core (HomeAssistant)
#   - homeassistant.config_entries (ConfigEntry, ConfigFlow, OptionsFlow)
#   - homeassistant.helpers.update_coordinator (DataUpdateCoordinator)
#   - homeassistant.helpers.selector (EntitySelector, NumberSelector)
#   - homeassistant.helpers.entity_platform (AddEntitiesCallback)
#   - homeassistant.helpers.device_registry (DeviceInfo)
#   - homeassistant.components.switch (SwitchEntity, SwitchDeviceClass)
#   - homeassistant.data_entry_flow
#   - voluptuous

_MOCKED_MODULES: dict[str, ModuleType | MagicMock] = {}


def _make_module(name: str) -> ModuleType:
    """Create a stub module and register it."""
    mod = ModuleType(name)
    _MOCKED_MODULES[name] = mod
    return mod


def _ensure_parent(name: str) -> None:
    """Ensure all parent packages exist as modules."""
    parts = name.split(".")
    for i in range(1, len(parts)):
        parent = ".".join(parts[:i])
        if parent not in sys.modules and parent not in _MOCKED_MODULES:
            _make_module(parent)


# Only mock if homeassistant is not already importable
try:
    import homeassistant  # noqa: F401

    _HA_AVAILABLE = True
except ImportError:
    _HA_AVAILABLE = False

if not _HA_AVAILABLE:
    # --- homeassistant.const ---
    ha_const = _make_module("homeassistant.const")
    ha_const.STATE_ON = "on"  # type: ignore[attr-defined]
    ha_const.STATE_OFF = "off"  # type: ignore[attr-defined]
    ha_const.EVENT_HOMEASSISTANT_STOP = "homeassistant_stop"  # type: ignore[attr-defined]

    # --- homeassistant (root) ---
    _ensure_parent("homeassistant")

    # --- homeassistant.core ---
    ha_core = _make_module("homeassistant.core")
    ha_core.HomeAssistant = MagicMock  # type: ignore[attr-defined]
    ha_core.Event = MagicMock  # type: ignore[attr-defined]
    ha_core.ServiceCall = MagicMock  # type: ignore[attr-defined]
    ha_core.callback = lambda f: f  # type: ignore[attr-defined]

    # --- homeassistant.config_entries ---
    ha_config_entries = _make_module("homeassistant.config_entries")
    ha_config_entries.ConfigEntry = MagicMock  # type: ignore[attr-defined]

    # ConfigFlow uses `class MyFlow(ConfigFlow, domain="foo")` syntax which
    # requires __init_subclass__ to accept keyword arguments.
    class _StubConfigFlow:
        VERSION = 1

        def __init_subclass__(cls, domain: str = "", **kwargs: object) -> None:
            super().__init_subclass__(**kwargs)

    ha_config_entries.ConfigFlow = _StubConfigFlow  # type: ignore[attr-defined]

    # OptionsFlow needs async_show_form / async_show_menu / async_create_entry
    # so that flow step handlers can call them.  These are no-ops here; tests
    # that need real return values should override them on the instance.
    class _StubOptionsFlow:
        def __init_subclass__(cls, **kwargs: object) -> None:
            super().__init_subclass__(**kwargs)

    ha_config_entries.OptionsFlow = _StubOptionsFlow  # type: ignore[attr-defined]

    # --- homeassistant.helpers ---
    _make_module("homeassistant.helpers")

    # --- homeassistant.helpers.update_coordinator ---
    ha_coordinator = _make_module("homeassistant.helpers.update_coordinator")

    class _StubCoordinator:
        def __init__(self, *args, **kwargs):
            self.hass = kwargs.get("hass") or (args[0] if args else None)
            self.data = {}

        def __class_getitem__(cls, item):
            return cls

        async def async_config_entry_first_refresh(self):
            pass

        async def async_request_refresh(self):
            pass

    ha_coordinator.DataUpdateCoordinator = _StubCoordinator  # type: ignore[attr-defined]

    class _StubCoordinatorEntity:
        def __init__(self, *args, **kwargs):
            pass

        def __init_subclass__(cls, **kwargs):
            pass

        def __class_getitem__(cls, item):
            return cls

    ha_coordinator.CoordinatorEntity = _StubCoordinatorEntity  # type: ignore[attr-defined]

    # --- homeassistant.helpers.entity_platform ---
    ha_entity_platform = _make_module("homeassistant.helpers.entity_platform")
    ha_entity_platform.AddEntitiesCallback = MagicMock  # type: ignore[attr-defined]

    # --- homeassistant.helpers.device_registry ---
    ha_device_registry = _make_module("homeassistant.helpers.device_registry")
    ha_device_registry.DeviceInfo = dict  # type: ignore[attr-defined]

    # --- homeassistant.helpers.entity_registry ---
    ha_entity_registry = _make_module("homeassistant.helpers.entity_registry")
    ha_entity_registry.async_get = MagicMock()  # type: ignore[attr-defined]
    ha_entity_registry.async_entries_for_config_entry = MagicMock(return_value=[])  # type: ignore[attr-defined]

    # --- homeassistant.helpers.selector ---
    ha_selector = _make_module("homeassistant.helpers.selector")

    # Selector *Config classes just store their kwargs as a plain dict so that
    # nesting them inside the Selector class works without mock-spec conflicts.
    class _SelectorConfig:
        def __new__(cls, **kwargs: object) -> dict:  # type: ignore[misc]
            return kwargs

    # Selector classes accept a config dict and are callable no-ops (they just
    # return the value unchanged). voluptuous uses them as validators.
    class _Selector:
        def __init__(self, config: dict | None = None) -> None:
            self._config = config or {}

        def __call__(self, value: object) -> object:
            return value

    ha_selector.EntitySelector = _Selector  # type: ignore[attr-defined]
    ha_selector.EntitySelectorConfig = _SelectorConfig  # type: ignore[attr-defined]
    ha_selector.NumberSelector = _Selector  # type: ignore[attr-defined]
    ha_selector.NumberSelectorConfig = _SelectorConfig  # type: ignore[attr-defined]
    ha_selector.NumberSelectorMode = MagicMock()  # type: ignore[attr-defined]
    ha_selector.SelectSelector = _Selector  # type: ignore[attr-defined]
    ha_selector.SelectSelectorConfig = _SelectorConfig  # type: ignore[attr-defined]
    ha_selector.SelectSelectorMode = MagicMock()  # type: ignore[attr-defined]
    ha_selector.BooleanSelector = _Selector  # type: ignore[attr-defined]
    ha_selector.TextSelector = _Selector  # type: ignore[attr-defined]

    # --- homeassistant.components ---
    _make_module("homeassistant.components")

    # --- homeassistant.components.switch ---
    ha_switch = _make_module("homeassistant.components.switch")
    ha_switch.SwitchEntity = type("SwitchEntity", (), {})  # type: ignore[attr-defined]
    ha_switch.SwitchDeviceClass = MagicMock  # type: ignore[attr-defined]

    # --- homeassistant.components.sensor ---
    ha_sensor = _make_module("homeassistant.components.sensor")
    ha_sensor.SensorEntity = type("SensorEntity", (), {})  # type: ignore[attr-defined]
    ha_sensor.SensorDeviceClass = MagicMock  # type: ignore[attr-defined]

    # --- homeassistant.components.binary_sensor ---
    ha_binary_sensor = _make_module("homeassistant.components.binary_sensor")
    ha_binary_sensor.BinarySensorEntity = type("BinarySensorEntity", (), {})  # type: ignore[attr-defined]
    ha_binary_sensor.BinarySensorDeviceClass = MagicMock  # type: ignore[attr-defined]

    # --- homeassistant.helpers.aiohttp_client ---
    ha_aiohttp = _make_module("homeassistant.helpers.aiohttp_client")
    ha_aiohttp.async_get_clientsession = MagicMock  # type: ignore[attr-defined]

    # --- homeassistant.helpers.event ---
    ha_event = _make_module("homeassistant.helpers.event")
    ha_event.async_track_time_change = MagicMock(return_value=lambda: None)  # type: ignore[attr-defined]
    ha_event.async_track_time_interval = MagicMock(return_value=lambda: None)  # type: ignore[attr-defined]
    ha_event.async_track_state_change_event = MagicMock(return_value=lambda: None)  # type: ignore[attr-defined]

    # --- homeassistant.data_entry_flow ---
    ha_data_flow = _make_module("homeassistant.data_entry_flow")

    # --- voluptuous (may or may not be installed) ---
    try:
        import voluptuous  # noqa: F401
    except ImportError:
        vol_mod = _make_module("voluptuous")
        vol_mod.Schema = MagicMock  # type: ignore[attr-defined]
        vol_mod.Required = MagicMock  # type: ignore[attr-defined]
        vol_mod.Optional = MagicMock  # type: ignore[attr-defined]
        vol_mod.Invalid = Exception  # type: ignore[attr-defined]

    # Register all mocked modules
    sys.modules.update(_MOCKED_MODULES)
