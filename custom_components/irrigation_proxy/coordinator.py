"""DataUpdateCoordinator for Irrigation Proxy."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import DEFAULT_UPDATE_INTERVAL_SECONDS, DOMAIN
from .safety import SafetyManager
from .zone import Zone

_LOGGER = logging.getLogger(__name__)


class IrrigationCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Central coordinator that polls valve states and runs safety checks."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        zones: dict[str, Zone],
        safety: SafetyManager,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=DEFAULT_UPDATE_INTERVAL_SECONDS),
        )
        self.entry = entry
        self.zones = zones
        self.safety = safety

    async def _async_update_data(self) -> dict[str, Any]:
        """Poll valve states, run safety checks, return zone status dict."""
        data: dict[str, Any] = {}

        for valve_id, zone in self.zones.items():
            state = self.hass.states.get(valve_id)
            if state is not None:
                zone.update_state(state.state)
            else:
                _LOGGER.debug(
                    "Coordinator: entity %s unavailable during poll", valve_id
                )

            # Orphan detection: zone is on but has no deadman timer
            if (
                zone.is_on
                and valve_id not in self.safety.zone_start_times
            ):
                _LOGGER.warning(
                    "Coordinator: zone '%s' is on with no deadman timer – forcing close",
                    zone.name,
                )
                await zone.force_close(self.hass)

            data[valve_id] = {
                "is_on": zone.is_on,
                "name": zone.name,
                "valve_entity_id": valve_id,
                "expected_state": zone.expected_state,
                "state_mismatch": zone.state_mismatch,
                "remaining_seconds": self.safety.remaining_seconds(valve_id),
                "duration_minutes": zone.duration_minutes,
            }

        # Backup safety check for overruns
        await self.safety.check_overruns(list(self.zones.values()))

        return data
