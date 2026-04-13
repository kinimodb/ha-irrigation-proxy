"""Safety layer with deadman timers and emergency shutdown."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from homeassistant.core import HomeAssistant

from .const import DEFAULT_SAFETY_MARGIN_SECONDS

if TYPE_CHECKING:
    from .zone import Zone

_LOGGER = logging.getLogger(__name__)


class SafetyManager:
    """Manages deadman timers and emergency valve shutdown.

    Safety guarantees:
    1. No valve stays open longer than max_runtime (deadman timer).
    2. On HA restart/reload, all valves are force-closed.
    3. Backup overrun check runs every coordinator poll cycle.
    """

    def __init__(self, hass: HomeAssistant, max_runtime_minutes: int) -> None:
        self._hass = hass
        self._max_runtime_seconds = max_runtime_minutes * 60
        self._timers: dict[str, asyncio.TimerHandle] = {}
        self._zone_start_times: dict[str, datetime] = {}

    @property
    def zone_start_times(self) -> dict[str, datetime]:
        """Expose start times for orphan detection in coordinator."""
        return self._zone_start_times

    def start_deadman(self, zone: Zone) -> None:
        """Start a deadman timer for a zone. Cancels any existing timer first."""
        self.cancel_deadman(zone.valve_entity_id)

        timeout = self._max_runtime_seconds + DEFAULT_SAFETY_MARGIN_SECONDS
        _LOGGER.info(
            "Safety: deadman timer started for '%s' (%ds = %dm + %ds margin)",
            zone.name,
            timeout,
            self._max_runtime_seconds // 60,
            DEFAULT_SAFETY_MARGIN_SECONDS,
        )

        self._zone_start_times[zone.valve_entity_id] = datetime.now(timezone.utc)

        handle = self._hass.loop.call_later(
            timeout,
            lambda: self._hass.async_create_task(
                self._deadman_expired(zone),
                f"irrigation_proxy_deadman_{zone.valve_entity_id}",
            ),
        )
        self._timers[zone.valve_entity_id] = handle

    def cancel_deadman(self, valve_entity_id: str) -> None:
        """Cancel the deadman timer for a zone."""
        handle = self._timers.pop(valve_entity_id, None)
        if handle is not None:
            handle.cancel()
            _LOGGER.debug("Safety: deadman timer cancelled for %s", valve_entity_id)

        self._zone_start_times.pop(valve_entity_id, None)

    async def emergency_shutdown(self, zones: list[Zone]) -> None:
        """Force-close ALL valves and cancel all timers.

        Called on HA startup, shutdown, and integration unload.
        """
        _LOGGER.warning("Safety: EMERGENCY SHUTDOWN – closing all %d valves", len(zones))

        # Cancel all timers first
        for valve_id in list(self._timers):
            self.cancel_deadman(valve_id)

        # Force-close every zone
        for zone in zones:
            try:
                await self._hass.services.async_call(
                    "switch",
                    "turn_off",
                    {"entity_id": zone.valve_entity_id},
                    blocking=True,
                )
                zone.expected_state = False
                zone.is_on = False
                _LOGGER.info(
                    "Safety: valve %s closed (emergency)", zone.valve_entity_id
                )
            except Exception:
                _LOGGER.exception(
                    "Safety: FAILED to close valve %s during emergency shutdown",
                    zone.valve_entity_id,
                )

    async def check_overruns(self, zones: list[Zone]) -> None:
        """Backup check: force-close zones that exceeded max_runtime.

        Called by the coordinator every poll cycle as a belt-and-suspenders
        safety measure in case call_later timers were lost.
        """
        now = datetime.now(timezone.utc)

        for zone in zones:
            if not zone.is_on:
                continue

            start_time = self._zone_start_times.get(zone.valve_entity_id)
            if start_time is None:
                continue

            elapsed = (now - start_time).total_seconds()
            if elapsed > self._max_runtime_seconds:
                _LOGGER.warning(
                    "Safety: overrun detected for '%s' (%.0fs > %ds) – forcing close",
                    zone.name,
                    elapsed,
                    self._max_runtime_seconds,
                )
                await zone.force_close(self._hass)
                self.cancel_deadman(zone.valve_entity_id)

    def remaining_seconds(self, valve_entity_id: str) -> int | None:
        """Return seconds remaining on a zone's deadman timer, or None."""
        start_time = self._zone_start_times.get(valve_entity_id)
        if start_time is None:
            return None

        elapsed = (datetime.now(timezone.utc) - start_time).total_seconds()
        remaining = self._max_runtime_seconds + DEFAULT_SAFETY_MARGIN_SECONDS - elapsed
        return max(0, int(remaining))

    async def _deadman_expired(self, zone: Zone) -> None:
        """Called when a deadman timer fires. Force-close the zone."""
        _LOGGER.warning(
            "Safety: DEADMAN TIMER EXPIRED for '%s' (%s) – forcing close!",
            zone.name,
            zone.valve_entity_id,
        )
        await zone.force_close(self._hass)
        self._timers.pop(zone.valve_entity_id, None)
        self._zone_start_times.pop(zone.valve_entity_id, None)
