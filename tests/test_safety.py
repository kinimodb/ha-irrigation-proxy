"""Tests for the SafetyManager."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.irrigation_proxy.const import DEFAULT_SAFETY_MARGIN_SECONDS
from custom_components.irrigation_proxy.safety import SafetyManager
from custom_components.irrigation_proxy.zone import Zone

from .conftest import FakeState, make_mock_hass


@pytest.fixture
def hass():
    return make_mock_hass(
        {
            "switch.valve_1": FakeState("off"),
            "switch.valve_2": FakeState("off"),
        }
    )


@pytest.fixture
def safety(hass: MagicMock) -> SafetyManager:
    return SafetyManager(hass, max_runtime_minutes=60)


@pytest.fixture
def zone_1() -> Zone:
    return Zone(name="Zone 1", valve_entity_id="switch.valve_1", duration_minutes=15)


@pytest.fixture
def zone_2() -> Zone:
    return Zone(name="Zone 2", valve_entity_id="switch.valve_2", duration_minutes=20)


class TestStartDeadman:
    """Tests for SafetyManager.start_deadman()."""

    def test_registers_timer(self, safety: SafetyManager, zone_1: Zone) -> None:
        safety.start_deadman(zone_1)

        safety._hass.loop.call_later.assert_called_once()
        args = safety._hass.loop.call_later.call_args
        timeout = args[0][0]

        expected_timeout = 60 * 60 + DEFAULT_SAFETY_MARGIN_SECONDS
        assert timeout == expected_timeout

    def test_records_start_time(self, safety: SafetyManager, zone_1: Zone) -> None:
        safety.start_deadman(zone_1)

        assert zone_1.valve_entity_id in safety.zone_start_times

    def test_cancels_existing_timer(
        self, safety: SafetyManager, zone_1: Zone
    ) -> None:
        """Starting a new deadman cancels any existing one for that zone."""
        safety.start_deadman(zone_1)
        first_handle = safety._hass.loop.call_later.return_value

        safety.start_deadman(zone_1)
        first_handle.cancel.assert_called_once()


class TestCancelDeadman:
    """Tests for SafetyManager.cancel_deadman()."""

    def test_cancels_timer(self, safety: SafetyManager, zone_1: Zone) -> None:
        safety.start_deadman(zone_1)
        handle = safety._hass.loop.call_later.return_value

        safety.cancel_deadman(zone_1.valve_entity_id)

        handle.cancel.assert_called_once()
        assert zone_1.valve_entity_id not in safety._timers
        assert zone_1.valve_entity_id not in safety.zone_start_times

    def test_no_error_if_no_timer(self, safety: SafetyManager) -> None:
        """Cancelling a non-existent timer should not raise."""
        safety.cancel_deadman("switch.nonexistent")


class TestEmergencyShutdown:
    """Tests for SafetyManager.emergency_shutdown()."""

    @pytest.mark.asyncio
    async def test_closes_all_valves(
        self,
        safety: SafetyManager,
        zone_1: Zone,
        zone_2: Zone,
    ) -> None:
        await safety.emergency_shutdown([zone_1, zone_2])

        assert safety._hass.services.async_call.call_count == 2
        calls = safety._hass.services.async_call.call_args_list

        entity_ids = {call[0][2]["entity_id"] for call in calls}
        assert entity_ids == {"switch.valve_1", "switch.valve_2"}

    @pytest.mark.asyncio
    async def test_cancels_all_timers(
        self,
        safety: SafetyManager,
        zone_1: Zone,
        zone_2: Zone,
    ) -> None:
        safety.start_deadman(zone_1)
        safety.start_deadman(zone_2)

        await safety.emergency_shutdown([zone_1, zone_2])

        assert len(safety._timers) == 0
        assert len(safety.zone_start_times) == 0

    @pytest.mark.asyncio
    async def test_continues_on_single_failure(
        self,
        safety: SafetyManager,
        zone_1: Zone,
        zone_2: Zone,
    ) -> None:
        """If one valve fails to close, the others should still be attempted."""
        call_count = 0

        async def _failing_call(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise Exception("Zigbee timeout")

        safety._hass.services.async_call = AsyncMock(side_effect=_failing_call)

        await safety.emergency_shutdown([zone_1, zone_2])

        # Both valves should have been attempted
        assert call_count == 2


class TestCheckOverruns:
    """Tests for SafetyManager.check_overruns()."""

    @pytest.mark.asyncio
    async def test_no_action_when_within_limit(
        self,
        safety: SafetyManager,
        zone_1: Zone,
    ) -> None:
        zone_1.is_on = True
        safety._zone_start_times[zone_1.valve_entity_id] = datetime.now(timezone.utc)

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await safety.check_overruns([zone_1])

        # No force_close should have been called (no extra service calls)
        safety._hass.services.async_call.assert_not_called()

    @pytest.mark.asyncio
    async def test_forces_close_on_overrun(
        self,
        safety: SafetyManager,
        zone_1: Zone,
    ) -> None:
        zone_1.is_on = True
        # Simulate zone started 2 hours ago (way past 60-min max)
        safety._zone_start_times[zone_1.valve_entity_id] = datetime.now(
            timezone.utc
        ) - timedelta(hours=2)

        # Make the valve appear to close after force_close
        safety._hass.states.get = lambda eid: FakeState("off")

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await safety.check_overruns([zone_1])

        # force_close should have been called (at least one service call)
        assert safety._hass.services.async_call.call_count >= 1

    @pytest.mark.asyncio
    async def test_skips_zones_that_are_off(
        self,
        safety: SafetyManager,
        zone_1: Zone,
    ) -> None:
        zone_1.is_on = False

        await safety.check_overruns([zone_1])

        safety._hass.services.async_call.assert_not_called()


class TestRemainingSeconds:
    """Tests for SafetyManager.remaining_seconds()."""

    def test_returns_none_when_not_running(self, safety: SafetyManager) -> None:
        assert safety.remaining_seconds("switch.nonexistent") is None

    def test_returns_positive_value(
        self, safety: SafetyManager, zone_1: Zone
    ) -> None:
        safety._zone_start_times[zone_1.valve_entity_id] = datetime.now(
            timezone.utc
        ) - timedelta(minutes=10)

        remaining = safety.remaining_seconds(zone_1.valve_entity_id)

        assert remaining is not None
        # 60min + 30s margin - 10min elapsed ≈ 50min + 30s
        assert 2900 < remaining < 3100

    def test_returns_zero_when_expired(
        self, safety: SafetyManager, zone_1: Zone
    ) -> None:
        safety._zone_start_times[zone_1.valve_entity_id] = datetime.now(
            timezone.utc
        ) - timedelta(hours=2)

        remaining = safety.remaining_seconds(zone_1.valve_entity_id)

        assert remaining == 0
