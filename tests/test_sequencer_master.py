"""Tests for the sequencer's master-valve handling (v0.5.0)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.irrigation_proxy.safety import SafetyManager
from custom_components.irrigation_proxy.sequencer import Sequencer, SequencerState
from custom_components.irrigation_proxy.zone import Zone

from .conftest import FakeState, make_mock_hass


MASTER = "switch.master_valve"
MASTER_VALVE = "valve.master_valve"


def _zones(n: int = 2) -> list[Zone]:
    return [
        Zone(
            name=f"Zone {i + 1}",
            valve_entity_id=f"switch.valve_{i + 1}",
            duration_minutes=1,
        )
        for i in range(n)
    ]


def _make_tracking_hass(zones: list[Zone]) -> tuple[MagicMock, dict[str, FakeState], list[tuple[str, str]]]:
    """Hass mock that records every switch call and mirrors state changes."""
    state_map: dict[str, FakeState] = {
        z.valve_entity_id: FakeState("off") for z in zones
    }
    state_map[MASTER] = FakeState("off")
    call_log: list[tuple[str, str]] = []

    hass = make_mock_hass(state_map)
    hass.async_create_task = MagicMock(
        side_effect=lambda coro, *a, **kw: asyncio.ensure_future(coro)
    )

    async def _track_call(domain, service, data, **kwargs):
        eid = data["entity_id"]
        call_log.append((service, eid))
        if service == "turn_on":
            state_map[eid] = FakeState("on")
        elif service == "turn_off":
            state_map[eid] = FakeState("off")

    hass.services.async_call = AsyncMock(side_effect=_track_call)
    hass.states.get = MagicMock(side_effect=lambda eid: state_map.get(eid))
    return hass, state_map, call_log


class TestMasterValveFlow:
    """Verify the zone-open → master-open → zone-close → master-close order."""

    @pytest.mark.asyncio
    async def test_order_for_single_zone(self) -> None:
        zones = _zones(1)
        hass, _, call_log = _make_tracking_hass(zones)
        safety = SafetyManager(hass, max_runtime_minutes=60)

        seq = Sequencer(
            hass=hass,
            zones=zones,
            safety=safety,
            pause_seconds=0,
            master_valve_entity_id=MASTER,
            depressurize_seconds=3,
        )

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await seq.start()
            if seq._task:
                await seq._task

        # Expected order:
        #   1. zone.turn_on  (valve_1)
        #   2. master.turn_on
        #   3. master.turn_off
        #   4. zone.turn_off (valve_1)
        assert call_log == [
            ("turn_on", "switch.valve_1"),
            ("turn_on", MASTER),
            ("turn_off", MASTER),
            ("turn_off", "switch.valve_1"),
        ]
        assert seq.state == SequencerState.IDLE

    @pytest.mark.asyncio
    async def test_order_for_two_zones_with_pause(self) -> None:
        zones = _zones(2)
        hass, _, call_log = _make_tracking_hass(zones)
        safety = SafetyManager(hass, max_runtime_minutes=60)

        seq = Sequencer(
            hass=hass,
            zones=zones,
            safety=safety,
            pause_seconds=10,
            master_valve_entity_id=MASTER,
            depressurize_seconds=2,
        )

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await seq.start()
            if seq._task:
                await seq._task

        # Each zone cycle: zone_on → master_on → master_off → zone_off
        assert call_log == [
            ("turn_on", "switch.valve_1"),
            ("turn_on", MASTER),
            ("turn_off", MASTER),
            ("turn_off", "switch.valve_1"),
            ("turn_on", "switch.valve_2"),
            ("turn_on", MASTER),
            ("turn_off", MASTER),
            ("turn_off", "switch.valve_2"),
        ]

    @pytest.mark.asyncio
    async def test_master_closed_first_on_stop(self) -> None:
        """Cancelling mid-run must close master before zone."""
        zones = _zones(1)
        # Use a long-enough duration so we can cancel during the wait.
        zones[0].duration_minutes = 60
        hass, _, call_log = _make_tracking_hass(zones)
        safety = SafetyManager(hass, max_runtime_minutes=60)

        seq = Sequencer(
            hass=hass,
            zones=zones,
            safety=safety,
            pause_seconds=0,
            master_valve_entity_id=MASTER,
            depressurize_seconds=0,
        )

        _real_sleep = asyncio.sleep
        sleep_event = asyncio.Event()

        async def _blocking_sleep(seconds):
            if seconds >= 60:
                await sleep_event.wait()
            else:
                await _real_sleep(0)

        with patch(
            "asyncio.sleep", new_callable=AsyncMock, side_effect=_blocking_sleep
        ):
            await seq.start()
            # Let the task open zone + master
            await _real_sleep(0.01)
            assert seq.state == SequencerState.RUNNING
            await seq.stop()

        # The log must contain master turn_off BEFORE zone turn_off (after the
        # opens, for both of which we don't care about exact order of verify).
        turn_off_order = [eid for service, eid in call_log if service == "turn_off"]
        assert turn_off_order[0] == MASTER, (
            f"expected master to close first, got order {turn_off_order}"
        )
        assert "switch.valve_1" in turn_off_order
        assert seq.state == SequencerState.IDLE


class TestValveDomainMaster:
    """Master valve is a valve.* entity — sequencer must use open_valve/close_valve."""

    @pytest.mark.asyncio
    async def test_valve_master_open_close_services(self) -> None:
        zones = _zones(1)

        state_map: dict[str, FakeState] = {
            z.valve_entity_id: FakeState("off") for z in zones
        }
        state_map[MASTER_VALVE] = FakeState("closed")
        call_log: list[tuple[str, str, str]] = []  # (domain, service, entity_id)

        hass = make_mock_hass(state_map)
        hass.async_create_task = MagicMock(
            side_effect=lambda coro, *a, **kw: asyncio.ensure_future(coro)
        )

        async def _track_call(domain, service, data, **kwargs):
            eid = data["entity_id"]
            call_log.append((domain, service, eid))
            if service in ("turn_on", "open_valve"):
                state_map[eid] = FakeState("on" if domain == "switch" else "open")
            elif service in ("turn_off", "close_valve"):
                state_map[eid] = FakeState("off" if domain == "switch" else "closed")

        hass.services.async_call = AsyncMock(side_effect=_track_call)
        hass.states.get = MagicMock(side_effect=lambda eid: state_map.get(eid))

        safety = SafetyManager(hass, max_runtime_minutes=60)
        seq = Sequencer(
            hass=hass,
            zones=zones,
            safety=safety,
            pause_seconds=0,
            master_valve_entity_id=MASTER_VALVE,
            depressurize_seconds=0,
        )

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await seq.start()
            if seq._task:
                await seq._task

        # Zone uses switch services, master uses valve services.
        assert ("switch", "turn_on", "switch.valve_1") in call_log
        assert ("valve", "open_valve", MASTER_VALVE) in call_log
        assert ("valve", "close_valve", MASTER_VALVE) in call_log
        assert ("switch", "turn_off", "switch.valve_1") in call_log
        assert seq.state == SequencerState.IDLE


class TestNoMaster:
    """Without a master valve the sequencer behaves like v0.4.0 (minus multiplier)."""

    @pytest.mark.asyncio
    async def test_no_master_calls(self) -> None:
        zones = _zones(2)
        hass, _, call_log = _make_tracking_hass(zones)
        safety = SafetyManager(hass, max_runtime_minutes=60)

        seq = Sequencer(
            hass=hass,
            zones=zones,
            safety=safety,
            pause_seconds=0,
            master_valve_entity_id=None,
        )

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await seq.start()
            if seq._task:
                await seq._task

        # Only zone calls, no master
        assert all(eid != MASTER for _, eid in call_log)
        assert [(s, eid) for s, eid in call_log] == [
            ("turn_on", "switch.valve_1"),
            ("turn_off", "switch.valve_1"),
            ("turn_on", "switch.valve_2"),
            ("turn_off", "switch.valve_2"),
        ]
