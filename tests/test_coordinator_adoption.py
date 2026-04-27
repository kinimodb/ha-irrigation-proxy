"""Tests for the adopt-orphan-valve path in the coordinator.

Background: when a zone valve is observed ``on`` but the SafetyManager
has no deadman timer for it (i.e. the valve was opened outside of the
proxy – e.g. via the underlying ``switch.*`` entity in the HA UI for a
manual flow test), the coordinator must NOT force-close it. Instead it
must arm a normal deadman so ``max_runtime`` still bounds the open
window.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.irrigation_proxy.coordinator import IrrigationCoordinator
from custom_components.irrigation_proxy.safety import SafetyManager
from custom_components.irrigation_proxy.sequencer import Sequencer
from custom_components.irrigation_proxy.zone import Zone

from .conftest import FakeState, make_mock_hass


def _make_zones(count: int = 1) -> list[Zone]:
    return [
        Zone(
            name=f"Zone {i + 1}",
            valve_entity_id=f"switch.valve_{i + 1}",
            duration_minutes=5,
        )
        for i in range(count)
    ]


def _make_hass(state_map: dict[str, FakeState]) -> MagicMock:
    hass = make_mock_hass(state_map)
    hass.async_create_task = MagicMock(
        side_effect=lambda coro, *a, **kw: asyncio.ensure_future(coro)
    )
    hass.bus = MagicMock()
    hass.bus.async_fire = MagicMock()
    return hass


def _build(hass: MagicMock, zones: list[Zone]) -> IrrigationCoordinator:
    safety = SafetyManager(hass, max_runtime_minutes=60)
    sequencer = Sequencer(
        hass=hass,
        zones=zones,
        safety=safety,
        pause_seconds=0,
        master_valve_entity_id=None,
    )
    entry = MagicMock()
    entry.entry_id = "test_entry"
    coord = IrrigationCoordinator(
        hass=hass,
        entry=entry,
        zones=zones,
        safety=safety,
        sequencer=sequencer,
    )
    coord.notify_sequencer_state_changed = MagicMock()
    coord.async_request_refresh = AsyncMock()
    coord.async_set_updated_data = MagicMock()
    return coord


def _make_event(
    entity_id: str,
    new_state: str,
    old_state: str = "off",
) -> MagicMock:
    event = MagicMock()
    event.data = {
        "entity_id": entity_id,
        "new_state": FakeState(new_state),
        "old_state": FakeState(old_state),
    }
    return event


@pytest.fixture(autouse=True)
def _no_sleep():
    with patch("asyncio.sleep", new_callable=AsyncMock):
        yield


class TestAdoptOrphanValve:
    """Coordinator must adopt valves opened outside of the proxy."""

    @pytest.mark.asyncio
    async def test_poll_adopts_valve_with_deadman(self) -> None:
        """The 30 s coordinator poll arms a deadman for an orphan-open valve."""
        state_map = {"switch.valve_1": FakeState("on")}
        hass = _make_hass(state_map)
        zones = _make_zones(1)
        coord = _build(hass, zones)

        with patch.object(
            coord.safety, "start_deadman", wraps=coord.safety.start_deadman
        ) as start_deadman, patch.object(
            zones[0], "force_close", new_callable=AsyncMock
        ) as force_close:
            await coord._async_update_data()

        start_deadman.assert_called_once_with(zones[0])
        force_close.assert_not_called()
        assert "switch.valve_1" in coord.safety.zone_start_times

    @pytest.mark.asyncio
    async def test_state_change_adopts_immediately(self) -> None:
        """An off→on state change adopts the zone without waiting for poll."""
        hass = _make_hass({"switch.valve_1": FakeState("off")})
        zones = _make_zones(1)
        coord = _build(hass, zones)

        with patch.object(
            coord.safety, "start_deadman", wraps=coord.safety.start_deadman
        ) as start_deadman:
            coord._on_valve_state_change(_make_event("switch.valve_1", "on"))

        start_deadman.assert_called_once_with(zones[0])
        assert "switch.valve_1" in coord.safety.zone_start_times

    @pytest.mark.asyncio
    async def test_state_change_off_does_not_adopt(self) -> None:
        """An on→off state change must not arm a deadman."""
        hass = _make_hass({"switch.valve_1": FakeState("off")})
        zones = _make_zones(1)
        coord = _build(hass, zones)

        with patch.object(
            coord.safety, "start_deadman", wraps=coord.safety.start_deadman
        ) as start_deadman:
            coord._on_valve_state_change(
                _make_event("switch.valve_1", "off", old_state="on")
            )

        start_deadman.assert_not_called()
        assert "switch.valve_1" not in coord.safety.zone_start_times

    @pytest.mark.asyncio
    async def test_already_adopted_zone_is_not_re_adopted_on_poll(self) -> None:
        """When a deadman is already running the poll leaves it alone."""
        state_map = {"switch.valve_1": FakeState("on")}
        hass = _make_hass(state_map)
        zones = _make_zones(1)
        coord = _build(hass, zones)

        coord.safety.start_deadman(zones[0])
        first_handle = coord.safety._timers["switch.valve_1"]

        with patch.object(
            coord.safety, "start_deadman", wraps=coord.safety.start_deadman
        ) as start_deadman:
            await coord._async_update_data()

        start_deadman.assert_not_called()
        # Handle untouched – no needless cancel/restart.
        assert coord.safety._timers["switch.valve_1"] is first_handle
