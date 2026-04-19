"""Tests for the leak / water-shortage emergency handler in the coordinator."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.irrigation_proxy.const import EVENT_LEAK_DETECTED
from custom_components.irrigation_proxy.coordinator import IrrigationCoordinator
from custom_components.irrigation_proxy.safety import SafetyManager
from custom_components.irrigation_proxy.sequencer import Sequencer
from custom_components.irrigation_proxy.zone import Zone

from .conftest import FakeState, make_mock_hass


def _make_zones(count: int = 2) -> list[Zone]:
    return [
        Zone(
            name=f"Zone {i + 1}",
            valve_entity_id=f"switch.valve_{i + 1}",
            duration_minutes=5,
        )
        for i in range(count)
    ]


def _make_hass_with_bus(state_map: dict[str, FakeState]) -> MagicMock:
    hass = make_mock_hass(state_map)
    hass.async_create_task = MagicMock(
        side_effect=lambda coro, *a, **kw: asyncio.ensure_future(coro)
    )
    hass.bus = MagicMock()
    hass.bus.async_fire = MagicMock()
    return hass


def _build(
    hass: MagicMock,
    zones: list[Zone],
    leak_sensors: list[str] | None = None,
    master: str | None = None,
) -> IrrigationCoordinator:
    safety = SafetyManager(hass, max_runtime_minutes=60)
    sequencer = Sequencer(
        hass=hass,
        zones=zones,
        safety=safety,
        pause_seconds=0,
        master_valve_entity_id=master,
    )
    entry = MagicMock()
    entry.entry_id = "test_entry"
    coord = IrrigationCoordinator(
        hass=hass,
        entry=entry,
        zones=zones,
        safety=safety,
        sequencer=sequencer,
        leak_sensors=leak_sensors or [],
    )
    # The stub DataUpdateCoordinator does not implement these, but the
    # leak handler calls them at the end to refresh the UI.
    coord.notify_sequencer_state_changed = MagicMock()
    coord.async_request_refresh = AsyncMock()
    return coord


def _make_event(
    entity_id: str,
    new_state: str | None,
    old_state: str | None = "off",
) -> MagicMock:
    event = MagicMock()
    event.data = {
        "entity_id": entity_id,
        "new_state": FakeState(new_state) if new_state is not None else None,
        "old_state": FakeState(old_state) if old_state is not None else None,
    }
    return event


@pytest.fixture(autouse=True)
def _no_sleep():
    """Skip real sleeps so force_close retries and state waits are instant."""
    with patch("asyncio.sleep", new_callable=AsyncMock):
        yield


class TestLeakHandling:
    """Behavioural tests for the leak-detection → emergency-shutdown path."""

    @pytest.mark.asyncio
    async def test_leak_triggers_emergency_shutdown(self) -> None:
        state_map = {
            "switch.valve_1": FakeState("off"),
            "switch.valve_2": FakeState("off"),
        }
        hass = _make_hass_with_bus(state_map)
        zones = _make_zones(2)
        coord = _build(
            hass,
            zones,
            leak_sensors=["binary_sensor.sonoff_swv_1_water_leak"],
        )

        await coord._trigger_leak_emergency(
            "binary_sensor.sonoff_swv_1_water_leak", "on"
        )

        # Every zone valve got a close call.
        entity_ids = {
            call.args[2]["entity_id"]
            for call in hass.services.async_call.call_args_list
        }
        assert "switch.valve_1" in entity_ids
        assert "switch.valve_2" in entity_ids

    @pytest.mark.asyncio
    async def test_leak_fires_event(self) -> None:
        hass = _make_hass_with_bus({})
        coord = _build(
            hass,
            _make_zones(1),
            leak_sensors=["binary_sensor.x"],
        )

        await coord._trigger_leak_emergency("binary_sensor.x", "on")

        fired = [
            call.args
            for call in hass.bus.async_fire.call_args_list
            if call.args[0] == EVENT_LEAK_DETECTED
        ]
        assert len(fired) == 1
        assert fired[0][1]["sensor_entity_id"] == "binary_sensor.x"

    @pytest.mark.asyncio
    async def test_leak_closes_master_valve(self) -> None:
        hass = _make_hass_with_bus({})
        coord = _build(
            hass,
            _make_zones(1),
            leak_sensors=["binary_sensor.x"],
            master="switch.master",
        )

        await coord._trigger_leak_emergency("binary_sensor.x", "on")

        master_calls = [
            call
            for call in hass.services.async_call.call_args_list
            if call.args[2]["entity_id"] == "switch.master"
        ]
        assert len(master_calls) >= 1, (
            "Master valve must be explicitly closed during leak emergency"
        )

    @pytest.mark.asyncio
    async def test_leak_calls_sequencer_stop(self) -> None:
        hass = _make_hass_with_bus({})
        coord = _build(
            hass, _make_zones(1), leak_sensors=["binary_sensor.x"]
        )
        coord.sequencer.stop = AsyncMock()

        await coord._trigger_leak_emergency("binary_sensor.x", "on")

        coord.sequencer.stop.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_second_leak_while_active_is_ignored(self) -> None:
        hass = _make_hass_with_bus({})
        coord = _build(
            hass, _make_zones(1), leak_sensors=["binary_sensor.x"]
        )
        coord.sequencer.stop = AsyncMock()

        coord._leak_emergency_active = True
        await coord._trigger_leak_emergency("binary_sensor.x", "on")

        coord.sequencer.stop.assert_not_called()

    def test_state_change_ignores_non_on_transition(self) -> None:
        hass = _make_hass_with_bus({})
        coord = _build(
            hass, _make_zones(1), leak_sensors=["binary_sensor.x"]
        )

        # on -> off: no emergency
        coord._on_leak_state_change(
            _make_event("binary_sensor.x", new_state="off", old_state="on")
        )

        hass.async_create_task.assert_not_called()

    def test_state_change_ignores_on_to_on(self) -> None:
        """A reported 'on' when we already knew the sensor was 'on'
        must not retrigger the emergency."""
        hass = _make_hass_with_bus({})
        coord = _build(
            hass, _make_zones(1), leak_sensors=["binary_sensor.x"]
        )

        coord._on_leak_state_change(
            _make_event("binary_sensor.x", new_state="on", old_state="on")
        )

        hass.async_create_task.assert_not_called()

    def test_state_change_schedules_emergency_on_off_to_on(self) -> None:
        hass = _make_hass_with_bus({})
        coord = _build(
            hass, _make_zones(1), leak_sensors=["binary_sensor.x"]
        )

        coord._on_leak_state_change(
            _make_event("binary_sensor.x", new_state="on", old_state="off")
        )

        hass.async_create_task.assert_called_once()

    def test_start_leak_tracking_no_sensors_is_noop(self) -> None:
        hass = _make_hass_with_bus({})
        coord = _build(hass, _make_zones(1), leak_sensors=[])

        coord.start_leak_tracking()

        assert coord._leak_unsub is None

    def test_start_leak_tracking_subscribes_when_sensors_configured(self) -> None:
        hass = _make_hass_with_bus({})
        coord = _build(
            hass,
            _make_zones(1),
            leak_sensors=["binary_sensor.x", "binary_sensor.y"],
        )

        with patch(
            "custom_components.irrigation_proxy.coordinator."
            "async_track_state_change_event",
            return_value=lambda: None,
        ) as track:
            coord.start_leak_tracking()

        track.assert_called_once()
        args = track.call_args.args
        # args: (hass, entity_ids, callback)
        assert set(args[1]) == {"binary_sensor.x", "binary_sensor.y"}

    def test_start_leak_tracking_triggers_on_already_on_sensor(self) -> None:
        """If a sensor is already 'on' at startup we must still fire the
        emergency shutdown – otherwise a reboot while a leak is active
        would silently allow the next scheduled run."""
        state_map = {
            "binary_sensor.already_leaking": FakeState("on"),
        }
        hass = _make_hass_with_bus(state_map)
        coord = _build(
            hass,
            _make_zones(1),
            leak_sensors=["binary_sensor.already_leaking"],
        )

        with patch(
            "custom_components.irrigation_proxy.coordinator."
            "async_track_state_change_event",
            return_value=lambda: None,
        ):
            coord.start_leak_tracking()

        hass.async_create_task.assert_called_once()

    def test_stop_leak_tracking_unsubscribes(self) -> None:
        hass = _make_hass_with_bus({})
        coord = _build(
            hass, _make_zones(1), leak_sensors=["binary_sensor.x"]
        )
        unsub = MagicMock()
        with patch(
            "custom_components.irrigation_proxy.coordinator."
            "async_track_state_change_event",
            return_value=unsub,
        ):
            coord.start_leak_tracking()

        coord.stop_leak_tracking()

        unsub.assert_called_once()
        assert coord._leak_unsub is None
