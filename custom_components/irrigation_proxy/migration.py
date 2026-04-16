"""Migrate config entries from older schema versions."""

from __future__ import annotations

import secrets
from typing import Any

from .const import (
    CONF_ZONE_DURATION_MINUTES,
    CONF_ZONE_ID,
    CONF_ZONE_NAME,
    CONF_ZONE_VALVE,
    CONF_ZONES,
    DEFAULT_DURATION_MINUTES,
)

# Keys that existed in v0.4.x but were removed in v0.5.0.
_STALE_KEYS = {"zone_durations", "rain_threshold_mm", "rain_adjust_mode"}


def migrate_v1_zones(data: dict[str, Any]) -> dict[str, Any]:
    """Convert v0.4.x zone format (list of entity-ID strings) to v0.5.0 dicts.

    If the data is already in v0.5.0 format (or has no zones) this is a no-op.
    """
    zones_raw = data.get(CONF_ZONES) or []
    if not zones_raw:
        return data

    # Nothing to do if the first element is already a dict.
    if isinstance(zones_raw[0], dict):
        return data

    durations: dict[str, Any] = data.pop("zone_durations", None) or {}
    default_duration = int(data.pop("duration_minutes", DEFAULT_DURATION_MINUTES))

    migrated: list[dict[str, Any]] = []
    for item in zones_raw:
        if not isinstance(item, str):
            continue
        duration = durations.get(item, default_duration)
        migrated.append(
            {
                CONF_ZONE_ID: f"z_{secrets.token_hex(4)}",
                CONF_ZONE_NAME: item,
                CONF_ZONE_VALVE: item,
                CONF_ZONE_DURATION_MINUTES: int(duration),
            }
        )

    data[CONF_ZONES] = migrated

    # Clean up stale keys that no longer exist in v0.5.0.
    for key in _STALE_KEYS:
        data.pop(key, None)

    return data
