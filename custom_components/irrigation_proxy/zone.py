"""Zone model for a single irrigation valve."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from homeassistant.const import STATE_ON
from homeassistant.core import HomeAssistant

from .const import DEFAULT_CLOSE_RETRY_MAX, DEFAULT_STATE_VERIFY_DELAY_SECONDS

_LOGGER = logging.getLogger(__name__)


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
    ) -> None:
        self.name = name
        self.valve_entity_id = valve_entity_id
        self.duration_minutes = duration_minutes

        self.is_on: bool = False
        self.expected_state: bool = False
        self.state_mismatch: bool = False
        self.last_state_change: datetime | None = None

    async def turn_on(self, hass: HomeAssistant) -> bool:
        """Open the valve and verify state.

        Returns True if the valve confirmed open, False on mismatch.
        """
        _LOGGER.info("Zone '%s': opening valve %s", self.name, self.valve_entity_id)
        self.expected_state = True

        await hass.services.async_call(
            "switch",
            "turn_on",
            {"entity_id": self.valve_entity_id},
            blocking=True,
        )

        await asyncio.sleep(DEFAULT_STATE_VERIFY_DELAY_SECONDS)
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

        await hass.services.async_call(
            "switch",
            "turn_off",
            {"entity_id": self.valve_entity_id},
            blocking=True,
        )

        await asyncio.sleep(DEFAULT_STATE_VERIFY_DELAY_SECONDS)
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
        actual_on = actual == STATE_ON
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

            await hass.services.async_call(
                "switch",
                "turn_off",
                {"entity_id": self.valve_entity_id},
                blocking=True,
            )

            await asyncio.sleep(DEFAULT_STATE_VERIFY_DELAY_SECONDS)

            actual = self._get_actual_state(hass)
            if actual != STATE_ON:
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
        self.is_on = state_str == STATE_ON

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
