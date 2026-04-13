"""Shared test fixtures for irrigation_proxy tests."""

from __future__ import annotations

from collections.abc import Callable
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.irrigation_proxy.zone import Zone


class FakeState:
    """Minimal HA state object for testing."""

    def __init__(self, state: str = "off", friendly_name: str = "Test Valve") -> None:
        self.state = state
        self.attributes = {"friendly_name": friendly_name}


def make_mock_hass(
    states: dict[str, FakeState] | None = None,
) -> MagicMock:
    """Create a mock HomeAssistant object."""
    hass = MagicMock()
    hass.services = MagicMock()
    hass.services.async_call = AsyncMock()

    state_map = states or {}

    def _get_state(entity_id: str) -> FakeState | None:
        return state_map.get(entity_id)

    hass.states = MagicMock()
    hass.states.get = MagicMock(side_effect=_get_state)

    # Event loop for call_later (used by safety)
    hass.loop = MagicMock()
    hass.loop.call_later = MagicMock()
    hass.async_create_task = MagicMock()

    return hass


@pytest.fixture
def mock_hass() -> Callable[..., MagicMock]:
    """Factory fixture that returns a mock hass builder."""
    return make_mock_hass


@pytest.fixture
def make_zone() -> Callable[..., Zone]:
    """Factory fixture to create Zone instances."""

    def _make(
        name: str = "Test Zone",
        valve_entity_id: str = "switch.test_valve",
        duration_minutes: int = 15,
    ) -> Zone:
        return Zone(
            name=name,
            valve_entity_id=valve_entity_id,
            duration_minutes=duration_minutes,
        )

    return _make


@pytest.fixture
def no_sleep():
    """Patch asyncio.sleep to be instant in tests."""
    with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        yield mock_sleep
