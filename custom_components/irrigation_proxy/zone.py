"""Zone model for a single irrigation valve."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from homeassistant.core import HomeAssistant

from .const import (
    DEFAULT_CLOSE_RETRY_MAX,
    DEFAULT_STATE_VERIFY_TIMEOUT_SECONDS,
    STATE_VERIFY_POLL_INTERVAL_SECONDS,
)

_LOGGER = logging.getLogger(__name__)

# States that mean a valve/switch is open/on.
_ENTITY_ON_STATES: frozenset[str] = frozenset({"on", "open"})


async def wait_for_entity_state(
    hass: HomeAssistant,
    entity_id: str,
    expected_on: bool,
    timeout: float = DEFAULT_STATE_VERIFY_TIMEOUT_SECONDS,
    poll_interval: float = STATE_VERIFY_POLL_INTERVAL_SECONDS,
) -> bool:
    """Poll *entity_id* until its state matches *expected_on* or *timeout* expires.

    Returns True as soon as the state matches, False if the budget is exhausted.
    """
    max_iterations = max(1, int(timeout / poll_interval))
    for i in range(max_iterations + 1):
        state = hass.states.get(entity_id)
        actual = state.state if state is not None else "unavailable"
        if entity_state_is_on(actual) == expected_on:
            return True
        if i < max_iterations:
            await asyncio.sleep(poll_interval)
    return False


def entity_svc_open(entity_id: str) -> tuple[str, str]:
    """Return (service_domain, service_action) to open/turn-on an entity.

    Handles both ``switch`` (turn_on) and ``valve`` (open_valve) domains.
    """
    if entity_id.split(".", 1)[0] == "valve":
        return ("valve", "open_valve")
    return ("switch", "turn_on")


def entity_svc_close(entity_id: str) -> tuple[str, str]:
    """Return (service_domain, service_action) to close/turn-off an entity.

    Handles both ``switch`` (turn_off) and ``valve`` (close_valve) domains.
    """
    if entity_id.split(".", 1)[0] == "valve":
        return ("valve", "close_valve")
    return ("switch", "turn_off")


def entity_state_is_on(state: str) -> bool:
    """Return True if *state* means the entity is open/on (switch or valve)."""
    return state in _ENTITY_ON_STATES


class Zone:
    """Represents a single irrigation zone (one valve).

    This is a plain domain object, not an HA entity. It encapsulates
    valve control logic including state verification and retry-on-close.
    """

    def __init__(
        self,
        name: str,
        valve_entity_id: str,
        duration_minutes: int,
        zone_id: str | None = None,
        duration_extra_seconds: int = 0,
    ) -> None:
        self.name = name
        self.valve_entity_id = valve_entity_id
        self.duration_minutes = duration_minutes
        # Clamp to 0–59 – above 60 s belongs in `duration_minutes`.
        self.duration_extra_seconds = max(0, min(59, int(duration_extra_seconds)))
        self.zone_id = zone_id

        self.is_on: bool = False
        self.expected_state: bool = False
        self.state_mismatch: bool = False
        self.last_state_change: datetime | None = None

    @property
    def duration_seconds(self) -> int:
        """Configured per-zone duration in seconds (minutes + extra seconds)."""
        return int(self.duration_minutes * 60 + self.duration_extra_seconds)

    async def turn_on(self, hass: HomeAssistant) -> bool:
        """Open the valve and verify state.

        Returns True if the valve confirmed open, False on mismatch.
        """
        _LOGGER.info("Zone '%s': opening valve %s", self.name, self.valve_entity_id)
        self.expected_state = True

        svc_domain, svc_action = entity_svc_open(self.valve_entity_id)
        await hass.services.async_call(
            svc_domain,
            svc_action,
            {"entity_id": self.valve_entity_id},
            blocking=True,
        )

        await wait_for_entity_state(hass, self.valve_entity_id, expected_on=True)
        verified = await self.verify_state(hass)

        if verified:
            self.last_state_change = datetime.now(timezone.utc)
            _LOGGER.info("Zone '%s': valve confirmed open", self.name)
        else:
            _LOGGER.warning(
                "Zone '%s': valve state mismatch after turn_on (expected on, got %s)",
                self.name,
                self._get_actual_state(hass),
            )

        return verified

    async def turn_off(self, hass: HomeAssistant) -> bool:
        """Close the valve and verify state.

        Returns True if the valve confirmed closed, False on mismatch.
        """
        _LOGGER.info("Zone '%s': closing valve %s", self.name, self.valve_entity_id)
        self.expected_state = False

        svc_domain, svc_action = entity_svc_close(self.valve_entity_id)
        await hass.services.async_call(
            svc_domain,
            svc_action,
            {"entity_id": self.valve_entity_id},
            blocking=True,
        )

        await wait_for_entity_state(hass, self.valve_entity_id, expected_on=False)
        verified = await self.verify_state(hass)

        if verified:
            self.last_state_change = datetime.now(timezone.utc)
            _LOGGER.info("Zone '%s': valve confirmed closed", self.name)
        else:
            _LOGGER.warning(
                "Zone '%s': valve state mismatch after turn_off (expected off, got %s)",
                self.name,
                self._get_actual_state(hass),
            )

        return verified

    async def verify_state(self, hass: HomeAssistant) -> bool:
        """Check if the actual valve state matches the expected state."""
        actual = self._get_actual_state(hass)
        actual_on = entity_state_is_on(actual)
        self.state_mismatch = actual_on != self.expected_state

        if self.state_mismatch:
            _LOGGER.warning(
                "Zone '%s': state mismatch – expected %s, actual %s",
                self.name,
                "on" if self.expected_state else "off",
                actual,
            )

        return not self.state_mismatch

    async def force_close(self, hass: HomeAssistant) -> bool:
        """Force-close the valve with retries. Returns True if closed successfully."""
        for attempt in range(1, DEFAULT_CLOSE_RETRY_MAX + 1):
            _LOGGER.warning(
                "Zone '%s': force_close attempt %d/%d",
                self.name,
                attempt,
                DEFAULT_CLOSE_RETRY_MAX,
            )

            svc_domain, svc_action = entity_svc_close(self.valve_entity_id)
            await hass.services.async_call(
                svc_domain,
                svc_action,
                {"entity_id": self.valve_entity_id},
                blocking=True,
            )

            await wait_for_entity_state(hass, self.valve_entity_id, expected_on=False)

            actual = self._get_actual_state(hass)
            if not entity_state_is_on(actual):
                self.expected_state = False
                self.is_on = False
                self.state_mismatch = False
                self.last_state_change = datetime.now(timezone.utc)
                _LOGGER.info(
                    "Zone '%s': force_close succeeded on attempt %d",
                    self.name,
                    attempt,
                )
                return True

        _LOGGER.error(
            "Zone '%s': force_close FAILED after %d attempts – valve may still be open!",
            self.name,
            DEFAULT_CLOSE_RETRY_MAX,
        )
        self.state_mismatch = True
        return False

    def update_state(self, state_str: str) -> None:
        """Update the zone's known state from a coordinator poll."""
        self.is_on = entity_state_is_on(state_str)

    def _get_actual_state(self, hass: HomeAssistant) -> str:
        """Read the current valve state from HA."""
        state = hass.states.get(self.valve_entity_id)
        if state is None:
            _LOGGER.warning(
                "Zone '%s': valve entity %s not found",
                self.name,
                self.valve_entity_id,
            )
            return "unavailable"
        return state.state
