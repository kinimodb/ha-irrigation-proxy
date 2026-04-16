"""Tests for config entry migration (v0.4.x → v0.5.0)."""

from __future__ import annotations

from custom_components.irrigation_proxy.const import (
    CONF_ZONE_DURATION_MINUTES,
    CONF_ZONE_ID,
    CONF_ZONE_NAME,
    CONF_ZONE_VALVE,
    CONF_ZONES,
    DEFAULT_DURATION_MINUTES,
)
from custom_components.irrigation_proxy.migration import migrate_v1_zones


def test_migrate_string_zones() -> None:
    """Old v0.4.x string zones are converted to v0.5.0 dicts."""
    data = {
        CONF_ZONES: ["switch.valve_front", "switch.valve_back"],
        "zone_durations": {"switch.valve_front": 20, "switch.valve_back": 10},
    }
    result = migrate_v1_zones(data)
    zones = result[CONF_ZONES]

    assert len(zones) == 2
    assert all(isinstance(z, dict) for z in zones)

    assert zones[0][CONF_ZONE_VALVE] == "switch.valve_front"
    assert zones[0][CONF_ZONE_DURATION_MINUTES] == 20
    assert zones[0][CONF_ZONE_NAME] == "switch.valve_front"
    assert zones[0][CONF_ZONE_ID].startswith("z_")

    assert zones[1][CONF_ZONE_VALVE] == "switch.valve_back"
    assert zones[1][CONF_ZONE_DURATION_MINUTES] == 10


def test_migrate_string_zones_without_durations() -> None:
    """Zones without zone_durations fall back to DEFAULT_DURATION_MINUTES."""
    data = {CONF_ZONES: ["switch.valve1"]}
    result = migrate_v1_zones(data)
    zones = result[CONF_ZONES]

    assert len(zones) == 1
    assert zones[0][CONF_ZONE_VALVE] == "switch.valve1"
    assert zones[0][CONF_ZONE_DURATION_MINUTES] == DEFAULT_DURATION_MINUTES


def test_migrate_preserves_global_default_duration() -> None:
    """If duration_minutes was set globally in v0.4.x, use it as fallback."""
    data = {
        CONF_ZONES: ["switch.valve1"],
        "duration_minutes": 25,
    }
    result = migrate_v1_zones(data)
    assert result[CONF_ZONES][0][CONF_ZONE_DURATION_MINUTES] == 25


def test_dict_zones_pass_through() -> None:
    """Already-migrated v0.5.0 dict zones are not modified."""
    zone = {
        CONF_ZONE_ID: "z_abc12345",
        CONF_ZONE_NAME: "Front",
        CONF_ZONE_VALVE: "switch.valve_front",
        CONF_ZONE_DURATION_MINUTES: 15,
    }
    data = {CONF_ZONES: [zone]}
    result = migrate_v1_zones(data)

    assert result[CONF_ZONES] == [zone]


def test_empty_zones_pass_through() -> None:
    """An empty zones list is left unchanged."""
    data = {CONF_ZONES: []}
    result = migrate_v1_zones(data)
    assert result[CONF_ZONES] == []


def test_no_zones_key() -> None:
    """Missing CONF_ZONES key is handled gracefully."""
    data = {"name": "Test"}
    result = migrate_v1_zones(data)
    assert CONF_ZONES not in result or result.get(CONF_ZONES) is None


def test_stale_keys_removed() -> None:
    """Old v0.4.x keys that no longer exist in v0.5.0 are cleaned up."""
    data = {
        CONF_ZONES: ["switch.valve1"],
        "zone_durations": {"switch.valve1": 10},
        "rain_threshold_mm": 5.0,
        "rain_adjust_mode": "hard",
    }
    result = migrate_v1_zones(data)

    assert "zone_durations" not in result
    assert "rain_threshold_mm" not in result
    assert "rain_adjust_mode" not in result
