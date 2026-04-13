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
    ha_config_entries.ConfigFlow = type("ConfigFlow", (), {"VERSION": 1})  # type: ignore[attr-defined]
    ha_config_entries.OptionsFlow = type("OptionsFlow", (), {})  # type: ignore[attr-defined]

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

    # --- homeassistant.helpers.selector ---
    ha_selector = _make_module("homeassistant.helpers.selector")
    ha_selector.EntitySelector = MagicMock  # type: ignore[attr-defined]
    ha_selector.EntitySelectorConfig = MagicMock  # type: ignore[attr-defined]
    ha_selector.NumberSelector = MagicMock  # type: ignore[attr-defined]
    ha_selector.NumberSelectorConfig = MagicMock  # type: ignore[attr-defined]
    ha_selector.NumberSelectorMode = MagicMock  # type: ignore[attr-defined]

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

    # --- homeassistant.data_entry_flow ---
    ha_data_flow = _make_module("homeassistant.data_entry_flow")

    # --- voluptuous (may or may not be installed) ---
    try:
        import voluptuous  # noqa: F401
    except ImportError:
        vol_mod = _make_module("voluptuous")
        vol_mod.Schema = MagicMock  # type: ignore[attr-defined]
        vol_mod.Required = MagicMock  # type: ignore[attr-defined]

    # Register all mocked modules
    sys.modules.update(_MOCKED_MODULES)
