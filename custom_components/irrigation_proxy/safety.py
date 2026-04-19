"""Safety layer with deadman timers and emergency shutdown."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
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
        # Separate registry for the master valve's manual-override deadman.
        # Independent from zone timers so it can fire even when no zone is
        # tracked (e.g. user opened the master alone for a maintenance test).
        self._master_timer: asyncio.TimerHandle | None = None
        self._master_start_time: datetime | None = None
        self._master_entity_id: str | None = None

    @property
    def zone_start_times(self) -> dict[str, datetime]:
        """Expose start times for orphan detection in coordinator."""
        return self._zone_start_times

    @property
    def max_runtime_minutes(self) -> int:
        """Current max runtime in minutes."""
        return self._max_runtime_seconds // 60

    @max_runtime_minutes.setter
    def max_runtime_minutes(self, value: int) -> None:
        self._max_runtime_seconds = max(1, int(value)) * 60

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

        # Force-close every zone (zone.force_close uses the correct service domain
        # for both switch and valve entities).
        for zone in zones:
            try:
                await zone.force_close(self._hass)
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

    # -- Master valve manual-override deadman ---------------------------

    def start_master_deadman(
        self,
        entity_id: str,
        on_expired: Callable[[], Awaitable[None]],
    ) -> None:
        """Arm a deadman for a manually-opened master valve.

        Independent from zone timers because the master can be opened
        without any zone being tracked (maintenance / line bleed). Uses
        the same max_runtime budget as zones so users have one knob.
        """
        self.cancel_master_deadman()

        timeout = self._max_runtime_seconds + DEFAULT_SAFETY_MARGIN_SECONDS
        _LOGGER.info(
            "Safety: master deadman started for '%s' (%ds)",
            entity_id,
            timeout,
        )
        self._master_entity_id = entity_id
        self._master_start_time = datetime.now(timezone.utc)

        def _fire() -> None:
            self._hass.async_create_task(
                self._master_deadman_expired(on_expired),
                f"irrigation_proxy_master_deadman_{entity_id}",
            )

        self._master_timer = self._hass.loop.call_later(timeout, _fire)

    def cancel_master_deadman(self) -> None:
        """Cancel the master-valve deadman timer."""
        if self._master_timer is not None:
            self._master_timer.cancel()
            _LOGGER.debug("Safety: master deadman cancelled")
        self._master_timer = None
        self._master_start_time = None
        self._master_entity_id = None

    def master_remaining_seconds(self) -> int | None:
        """Seconds left on the master deadman, or None if not armed."""
        if self._master_start_time is None:
            return None
        elapsed = (
            datetime.now(timezone.utc) - self._master_start_time
        ).total_seconds()
        remaining = (
            self._max_runtime_seconds + DEFAULT_SAFETY_MARGIN_SECONDS - elapsed
        )
        return max(0, int(remaining))

    async def _master_deadman_expired(
        self, on_expired: Callable[[], Awaitable[None]]
    ) -> None:
        entity_id = self._master_entity_id or "<unknown>"
        _LOGGER.warning(
            "Safety: MASTER DEADMAN EXPIRED for '%s' – forcing close!",
            entity_id,
        )
        try:
            await on_expired()
        except Exception:  # noqa: BLE001 – defensive
            _LOGGER.exception(
                "Safety: master deadman close callback failed for %s",
                entity_id,
            )
        self._master_timer = None
        self._master_start_time = None
        self._master_entity_id = None
