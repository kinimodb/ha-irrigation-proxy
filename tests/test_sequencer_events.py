"""Tests for HA bus events fired by the Sequencer."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.irrigation_proxy.const import (
    EVENT_PROGRAM_ABORTED,
    EVENT_PROGRAM_COMPLETED,
    EVENT_PROGRAM_STARTED,
    EVENT_ZONE_COMPLETED,
    EVENT_ZONE_ERROR,
    EVENT_ZONE_STARTED,
)
from custom_components.irrigation_proxy.safety import SafetyManager
from custom_components.irrigation_proxy.sequencer import Sequencer, SequencerState
from custom_components.irrigation_proxy.zone import Zone

from .conftest import FakeState, make_mock_hass


def _make_zone(
    name: str = "Zone 1",
    valve_entity_id: str = "switch.valve_1",
    duration_minutes: int = 1,
) -> Zone:
    return Zone(name=name, valve_entity_id=valve_entity_id, duration_minutes=duration_minutes)


def _make_zones(count: int = 3, duration: int = 1) -> list[Zone]:
    return [
        _make_zone(
            name=f"Zone {i + 1}",
            valve_entity_id=f"switch.valve_{i + 1}",
            duration_minutes=duration,
        )
        for i in range(count)
    ]


def _make_hass_with_bus(state_map: dict[str, FakeState]) -> MagicMock:
    """Create mock hass with a tracked bus."""
    hass = make_mock_hass(state_map)
    hass.async_create_task = MagicMock(
        side_effect=lambda coro, *a, **kw: asyncio.ensure_future(coro)
    )
    hass.bus = MagicMock()
    hass.bus.async_fire = MagicMock()
    return hass


def _wire_valve_tracking(
    hass: MagicMock, state_map: dict[str, FakeState],
) -> None:
    """Make valve service calls update the state map."""

    async def _track_call(domain, service, data, **kwargs):
        eid = data["entity_id"]
        if service == "turn_on":
            state_map[eid] = FakeState("on")
        elif service == "turn_off":
            state_map[eid] = FakeState("off")

    hass.services.async_call = AsyncMock(side_effect=_track_call)
    hass.states.get = MagicMock(side_effect=lambda eid: state_map.get(eid))


def _fired_events(hass: MagicMock, event_type: str) -> list[dict]:
    """Return all event_data dicts fired for a given event type."""
    return [
        call.args[1]
        for call in hass.bus.async_fire.call_args_list
        if call.args[0] == event_type
    ]


class TestProgramEvents:
    """Tests for program-level events."""

    @pytest.mark.asyncio
    async def test_program_started_event(self) -> None:
        state_map = {"switch.valve_1": FakeState("off")}
        hass = _make_hass_with_bus(state_map)
        _wire_valve_tracking(hass, state_map)
        zones = _make_zones(1)

        seq = Sequencer(
            hass=hass, zones=zones,
            safety=SafetyManager(hass, max_runtime_minutes=60),
            pause_seconds=0,
        )

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await seq.start()
            if seq._task:
                await seq._task

        events = _fired_events(hass, EVENT_PROGRAM_STARTED)
        assert len(events) == 1
        assert events[0]["total_zones"] == 1
        assert events[0]["zones"] == ["Zone 1"]

    @pytest.mark.asyncio
    async def test_program_completed_event(self) -> None:
        state_map = {f"switch.valve_{i + 1}": FakeState("off") for i in range(2)}
        hass = _make_hass_with_bus(state_map)
        _wire_valve_tracking(hass, state_map)
        zones = _make_zones(2)

        seq = Sequencer(
            hass=hass, zones=zones,
            safety=SafetyManager(hass, max_runtime_minutes=60),
            pause_seconds=0,
        )

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await seq.start()
            if seq._task:
                await seq._task

        events = _fired_events(hass, EVENT_PROGRAM_COMPLETED)
        assert len(events) == 1
        assert events[0]["zones_completed"] == 2
        assert events[0]["total_zones"] == 2

    @pytest.mark.asyncio
    async def test_program_aborted_on_stop(self) -> None:
        state_map = {"switch.valve_1": FakeState("off")}
        hass = _make_hass_with_bus(state_map)
        _wire_valve_tracking(hass, state_map)
        zones = _make_zones(1, duration=60)

        seq = Sequencer(
            hass=hass, zones=zones,
            safety=SafetyManager(hass, max_runtime_minutes=60),
            pause_seconds=0,
        )

        sleep_event = asyncio.Event()

        async def _blocking_sleep(seconds):
            if seconds > 5:
                await sleep_event.wait()

        with patch("asyncio.sleep", new_callable=AsyncMock, side_effect=_blocking_sleep):
            await seq.start()
            await asyncio.sleep(0)
            await asyncio.sleep(0)

            await seq.stop()

        events = _fired_events(hass, EVENT_PROGRAM_ABORTED)
        assert len(events) == 1
        assert events[0]["reason"] == "stopped"


class TestZoneEvents:
    """Tests for zone-level events."""

    @pytest.mark.asyncio
    async def test_zone_started_and_completed_events(self) -> None:
        state_map = {f"switch.valve_{i + 1}": FakeState("off") for i in range(3)}
        hass = _make_hass_with_bus(state_map)
        _wire_valve_tracking(hass, state_map)
        zones = _make_zones(3)

        seq = Sequencer(
            hass=hass, zones=zones,
            safety=SafetyManager(hass, max_runtime_minutes=60),
            pause_seconds=0,
        )

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await seq.start()
            if seq._task:
                await seq._task

        started = _fired_events(hass, EVENT_ZONE_STARTED)
        completed = _fired_events(hass, EVENT_ZONE_COMPLETED)

        assert len(started) == 3
        assert len(completed) == 3

        # Verify order and data
        for i in range(3):
            assert started[i]["zone_name"] == f"Zone {i + 1}"
            assert started[i]["zone_index"] == i
            assert started[i]["valve_entity_id"] == f"switch.valve_{i + 1}"
            assert started[i]["duration_seconds"] == 60  # 1 min

            assert completed[i]["zone_name"] == f"Zone {i + 1}"
            assert completed[i]["zone_index"] == i

    @pytest.mark.asyncio
    async def test_zone_error_when_valve_fails_to_open(self) -> None:
        """A zone that fails to open should fire a zone_error event."""
        state_map = {
            "switch.valve_1": FakeState("off"),  # stays off = fail
            "switch.valve_2": FakeState("off"),
        }
        hass = _make_hass_with_bus(state_map)

        async def _track_call(domain, service, data, **kwargs):
            eid = data["entity_id"]
            if service == "turn_on" and eid == "switch.valve_2":
                state_map[eid] = FakeState("on")
            elif service == "turn_off":
                state_map[eid] = FakeState("off")

        hass.services.async_call = AsyncMock(side_effect=_track_call)
        hass.states.get = MagicMock(side_effect=lambda eid: state_map.get(eid))

        zones = _make_zones(2)

        seq = Sequencer(
            hass=hass, zones=zones,
            safety=SafetyManager(hass, max_runtime_minutes=60),
            pause_seconds=0,
        )

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await seq.start()
            if seq._task:
                await seq._task

        errors = _fired_events(hass, EVENT_ZONE_ERROR)
        assert len(errors) == 1
        assert errors[0]["zone_name"] == "Zone 1"
        assert errors[0]["reason"] == "failed_to_open"

        # Zone 2 should still have started and completed
        started = _fired_events(hass, EVENT_ZONE_STARTED)
        assert len(started) == 1
        assert started[0]["zone_name"] == "Zone 2"


class TestEventSequence:
    """Tests for correct event ordering."""

    @pytest.mark.asyncio
    async def test_full_event_sequence(self) -> None:
        """Verify the full event order for a 2-zone program."""
        state_map = {f"switch.valve_{i + 1}": FakeState("off") for i in range(2)}
        hass = _make_hass_with_bus(state_map)
        _wire_valve_tracking(hass, state_map)
        zones = _make_zones(2)

        seq = Sequencer(
            hass=hass, zones=zones,
            safety=SafetyManager(hass, max_runtime_minutes=60),
            pause_seconds=0,
        )

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await seq.start()
            if seq._task:
                await seq._task

        event_types = [call.args[0] for call in hass.bus.async_fire.call_args_list]
        assert event_types == [
            EVENT_PROGRAM_STARTED,
            EVENT_ZONE_STARTED,
            EVENT_ZONE_COMPLETED,
            EVENT_ZONE_STARTED,
            EVENT_ZONE_COMPLETED,
            EVENT_PROGRAM_COMPLETED,
        ]
