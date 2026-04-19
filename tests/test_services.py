"""Tests for irrigation_proxy domain services (start_program / stop_program).

W3: services now accept an optional entry_id parameter. Without it they
broadcast to all entries (legacy) and emit a deprecation warning.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.irrigation_proxy.__init__ import _async_register_services
from custom_components.irrigation_proxy.const import (
    DOMAIN,
    SERVICE_START_PROGRAM,
    SERVICE_STOP_PROGRAM,
)
from custom_components.irrigation_proxy.coordinator import IrrigationCoordinator
from custom_components.irrigation_proxy.safety import SafetyManager
from custom_components.irrigation_proxy.sequencer import Sequencer
from custom_components.irrigation_proxy.zone import Zone

from .conftest import make_mock_hass


def _make_coordinator(hass: MagicMock, entry_id: str = "entry_1") -> IrrigationCoordinator:
    zones = [Zone(name="Z1", valve_entity_id="switch.z1", duration_minutes=1)]
    safety = SafetyManager(hass, max_runtime_minutes=60)
    sequencer = Sequencer(hass=hass, zones=zones, safety=safety, pause_seconds=0)
    sequencer.start = AsyncMock()
    sequencer.stop = AsyncMock()

    entry = MagicMock()
    entry.entry_id = entry_id

    coord = IrrigationCoordinator(
        hass=hass,
        entry=entry,
        zones=zones,
        safety=safety,
        sequencer=sequencer,
    )
    coord.notify_sequencer_state_changed = MagicMock()
    coord.async_request_refresh = AsyncMock()
    return coord


def _make_service_call(data: dict) -> MagicMock:
    call = MagicMock()
    call.data = data
    return call


def _setup_hass(coordinators: dict[str, IrrigationCoordinator]) -> MagicMock:
    hass = make_mock_hass()
    hass.data = {DOMAIN: dict(coordinators)}
    registered: dict = {}

    def _async_register(domain, service, handler):
        registered[(domain, service)] = handler

    hass.services.async_register = MagicMock(side_effect=_async_register)
    hass._registered = registered
    return hass


class TestServiceEntryIdScoping:

    @pytest.mark.asyncio
    async def test_with_entry_id_targets_one_entry(self) -> None:
        hass = make_mock_hass()
        coord1 = _make_coordinator(hass, "entry_1")
        coord2 = _make_coordinator(hass, "entry_2")
        hass.data = {DOMAIN: {"entry_1": coord1, "entry_2": coord2}}

        handlers: dict = {}
        hass.services.async_register = MagicMock(
            side_effect=lambda d, s, h: handlers.__setitem__(s, h)
        )
        _async_register_services(hass)

        await handlers[SERVICE_START_PROGRAM](_make_service_call({"entry_id": "entry_1"}))

        coord1.sequencer.start.assert_called_once()
        coord2.sequencer.start.assert_not_called()

    @pytest.mark.asyncio
    async def test_without_entry_id_targets_all(self) -> None:
        hass = make_mock_hass()
        coord1 = _make_coordinator(hass, "entry_1")
        coord2 = _make_coordinator(hass, "entry_2")
        hass.data = {DOMAIN: {"entry_1": coord1, "entry_2": coord2}}

        handlers: dict = {}
        hass.services.async_register = MagicMock(
            side_effect=lambda d, s, h: handlers.__setitem__(s, h)
        )
        _async_register_services(hass)

        await handlers[SERVICE_START_PROGRAM](_make_service_call({}))

        coord1.sequencer.start.assert_called_once()
        coord2.sequencer.start.assert_called_once()

    @pytest.mark.asyncio
    async def test_without_entry_id_multiple_entries_logs_warning(self) -> None:
        hass = make_mock_hass()
        coord1 = _make_coordinator(hass, "entry_1")
        coord2 = _make_coordinator(hass, "entry_2")
        hass.data = {DOMAIN: {"entry_1": coord1, "entry_2": coord2}}

        handlers: dict = {}
        hass.services.async_register = MagicMock(
            side_effect=lambda d, s, h: handlers.__setitem__(s, h)
        )
        _async_register_services(hass)

        with patch(
            "custom_components.irrigation_proxy.__init__._LOGGER"
        ) as mock_logger:
            await handlers[SERVICE_START_PROGRAM](_make_service_call({}))
            warning_calls = [str(c) for c in mock_logger.warning.call_args_list]
            assert any("deprecated" in w.lower() for w in warning_calls), (
                "expected a deprecation warning when entry_id is omitted "
                f"with multiple entries; got: {warning_calls}"
            )

    @pytest.mark.asyncio
    async def test_unknown_entry_id_is_a_noop(self) -> None:
        hass = make_mock_hass()
        coord1 = _make_coordinator(hass, "entry_1")
        hass.data = {DOMAIN: {"entry_1": coord1}}

        handlers: dict = {}
        hass.services.async_register = MagicMock(
            side_effect=lambda d, s, h: handlers.__setitem__(s, h)
        )
        _async_register_services(hass)

        await handlers[SERVICE_START_PROGRAM](
            _make_service_call({"entry_id": "does_not_exist"})
        )
        coord1.sequencer.start.assert_not_called()

    @pytest.mark.asyncio
    async def test_stop_with_entry_id_targets_one_entry(self) -> None:
        hass = make_mock_hass()
        coord1 = _make_coordinator(hass, "entry_1")
        coord2 = _make_coordinator(hass, "entry_2")
        hass.data = {DOMAIN: {"entry_1": coord1, "entry_2": coord2}}

        handlers: dict = {}
        hass.services.async_register = MagicMock(
            side_effect=lambda d, s, h: handlers.__setitem__(s, h)
        )
        _async_register_services(hass)

        await handlers[SERVICE_STOP_PROGRAM](_make_service_call({"entry_id": "entry_2"}))

        coord1.sequencer.stop.assert_not_called()
        coord2.sequencer.stop.assert_called_once()

    @pytest.mark.asyncio
    async def test_single_entry_no_warning_without_entry_id(self) -> None:
        """With exactly one entry, omitting entry_id must NOT trigger a warning."""
        hass = make_mock_hass()
        coord1 = _make_coordinator(hass, "entry_1")
        hass.data = {DOMAIN: {"entry_1": coord1}}

        handlers: dict = {}
        hass.services.async_register = MagicMock(
            side_effect=lambda d, s, h: handlers.__setitem__(s, h)
        )
        _async_register_services(hass)

        with patch(
            "custom_components.irrigation_proxy.__init__._LOGGER"
        ) as mock_logger:
            await handlers[SERVICE_START_PROGRAM](_make_service_call({}))
            warning_calls = [str(c) for c in mock_logger.warning.call_args_list]
            assert not any("deprecated" in w.lower() for w in warning_calls), (
                "no deprecation warning expected for single-entry setup"
            )
        coord1.sequencer.start.assert_called_once()
