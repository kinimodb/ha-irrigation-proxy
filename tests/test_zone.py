"""Tests for the Zone domain model."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from custom_components.irrigation_proxy.zone import Zone

from .conftest import FakeState, make_mock_hass


@pytest.fixture
def zone():
    return Zone(
        name="Front Lawn",
        valve_entity_id="switch.valve_front",
        duration_minutes=15,
    )


class TestTurnOn:
    """Tests for Zone.turn_on()."""

    @pytest.mark.asyncio
    async def test_calls_switch_service(self, zone: Zone) -> None:
        hass = make_mock_hass({"switch.valve_front": FakeState("on")})

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await zone.turn_on(hass)

        hass.services.async_call.assert_called_once_with(
            "switch",
            "turn_on",
            {"entity_id": "switch.valve_front"},
            blocking=True,
        )

    @pytest.mark.asyncio
    async def test_sets_expected_state(self, zone: Zone) -> None:
        hass = make_mock_hass({"switch.valve_front": FakeState("on")})

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await zone.turn_on(hass)

        assert zone.expected_state is True

    @pytest.mark.asyncio
    async def test_returns_true_on_verified(self, zone: Zone) -> None:
        hass = make_mock_hass({"switch.valve_front": FakeState("on")})

        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await zone.turn_on(hass)

        assert result is True
        assert zone.state_mismatch is False

    @pytest.mark.asyncio
    async def test_returns_false_on_mismatch(self, zone: Zone) -> None:
        hass = make_mock_hass({"switch.valve_front": FakeState("off")})

        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await zone.turn_on(hass)

        assert result is False
        assert zone.state_mismatch is True


class TestTurnOff:
    """Tests for Zone.turn_off()."""

    @pytest.mark.asyncio
    async def test_calls_switch_service(self, zone: Zone) -> None:
        hass = make_mock_hass({"switch.valve_front": FakeState("off")})

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await zone.turn_off(hass)

        hass.services.async_call.assert_called_once_with(
            "switch",
            "turn_off",
            {"entity_id": "switch.valve_front"},
            blocking=True,
        )

    @pytest.mark.asyncio
    async def test_returns_true_when_confirmed_off(self, zone: Zone) -> None:
        hass = make_mock_hass({"switch.valve_front": FakeState("off")})

        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await zone.turn_off(hass)

        assert result is True
        assert zone.expected_state is False


class TestVerifyState:
    """Tests for Zone.verify_state()."""

    @pytest.mark.asyncio
    async def test_match_returns_true(self, zone: Zone) -> None:
        hass = make_mock_hass({"switch.valve_front": FakeState("off")})
        zone.expected_state = False

        result = await zone.verify_state(hass)

        assert result is True
        assert zone.state_mismatch is False

    @pytest.mark.asyncio
    async def test_mismatch_returns_false(self, zone: Zone) -> None:
        hass = make_mock_hass({"switch.valve_front": FakeState("on")})
        zone.expected_state = False

        result = await zone.verify_state(hass)

        assert result is False
        assert zone.state_mismatch is True

    @pytest.mark.asyncio
    async def test_unavailable_entity(self, zone: Zone) -> None:
        hass = make_mock_hass({})  # Entity not found
        zone.expected_state = False

        result = await zone.verify_state(hass)

        # "unavailable" != STATE_ON, so expected_state=False matches
        assert result is True


class TestForceClose:
    """Tests for Zone.force_close()."""

    @pytest.mark.asyncio
    async def test_succeeds_first_attempt(self, zone: Zone) -> None:
        hass = make_mock_hass({"switch.valve_front": FakeState("off")})

        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await zone.force_close(hass)

        assert result is True
        assert zone.is_on is False

    @pytest.mark.asyncio
    async def test_succeeds_on_retry(self, zone: Zone) -> None:
        """Valve stays on for 2 attempts, closes on 3rd."""
        call_count = 0

        def _get_state(entity_id: str) -> FakeState:
            nonlocal call_count
            call_count += 1
            # First 2 reads: still on. Third: off.
            if call_count <= 2:
                return FakeState("on")
            return FakeState("off")

        hass = make_mock_hass()
        hass.states.get = _get_state

        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await zone.force_close(hass)

        assert result is True
        assert hass.services.async_call.call_count == 3

    @pytest.mark.asyncio
    async def test_fails_after_all_retries(self, zone: Zone) -> None:
        """All 3 retries fail – valve stays on."""
        hass = make_mock_hass({"switch.valve_front": FakeState("on")})

        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await zone.force_close(hass)

        assert result is False
        assert zone.state_mismatch is True


class TestUpdateState:
    """Tests for Zone.update_state()."""

    def test_on(self, zone: Zone) -> None:
        zone.update_state("on")
        assert zone.is_on is True

    def test_off(self, zone: Zone) -> None:
        zone.update_state("off")
        assert zone.is_on is False

    def test_unavailable(self, zone: Zone) -> None:
        zone.update_state("unavailable")
        assert zone.is_on is False
