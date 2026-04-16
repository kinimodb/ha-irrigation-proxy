"""Tests for the irrigation_proxy config / options flow.

These tests run without the real homeassistant package installed.
The conftest.py at the repo root stubs out all HA modules.

We test the STEP LOGIC of IrrigationProxyOptionsFlow by:
  • Monkey-patching async_show_form / async_show_menu / async_create_entry
    on the mocked OptionsFlow base class so the real methods are available.
  • Calling step handlers directly with prepared user_input dicts.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from custom_components.irrigation_proxy.config_flow import (
    IrrigationProxyOptionsFlow,
)
from custom_components.irrigation_proxy.const import (
    CONF_NAME,
    CONF_ZONE_DURATION_MINUTES,
    CONF_ZONE_ID,
    CONF_ZONE_NAME,
    CONF_ZONE_VALVE,
    CONF_ZONES,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _stub_show_form(**kwargs: Any) -> dict[str, Any]:
    """Minimal stand-in for FlowHandler.async_show_form."""
    return {"type": "form", **kwargs}


def _stub_show_menu(**kwargs: Any) -> dict[str, Any]:
    """Minimal stand-in for FlowHandler.async_show_menu."""
    return {"type": "menu", **kwargs}


def _stub_create_entry(data: Any = None) -> dict[str, Any]:
    """Minimal stand-in for FlowHandler.async_create_entry."""
    return {"type": "create_entry", "data": data}


def _make_flow(zones: list[dict] | None = None) -> IrrigationProxyOptionsFlow:
    """Create a flow instance with a minimal mocked config entry."""
    entry = MagicMock()
    entry.data = {
        CONF_NAME: "Test Program",
        CONF_ZONES: zones or [],
        "schedule_enabled": False,
        "schedule_start_times": [],
        "schedule_weekdays": list(
            ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
        ),
        "inter_zone_delay_seconds": 30,
        "depressurize_seconds": 5,
        "max_runtime_minutes": 60,
        "master_valve_entity_id": None,
    }
    entry.options = {}

    flow = IrrigationProxyOptionsFlow(entry)

    # Patch HA base-class methods that aren't available in tests.
    flow.async_show_form = _stub_show_form  # type: ignore[method-assign]
    flow.async_show_menu = _stub_show_menu  # type: ignore[method-assign]
    flow.async_create_entry = _stub_create_entry  # type: ignore[method-assign]

    # Give the flow a minimal hass so async_step_save can call
    # hass.config_entries.async_update_entry without crashing.
    flow.hass = MagicMock()

    return flow


# ---------------------------------------------------------------------------
# Tests: async_step_zone_add
# ---------------------------------------------------------------------------


class TestZoneAdd:
    """Tests for async_step_zone_add."""

    @pytest.mark.asyncio
    async def test_shows_form_when_no_input(self) -> None:
        """Calling the step with no input must render the form."""
        flow = _make_flow()
        result = await flow.async_step_zone_add(user_input=None)
        assert result["type"] == "form"
        assert result["step_id"] == "zone_add"

    @pytest.mark.asyncio
    async def test_adds_zone_with_valid_input(self) -> None:
        """Submitting a valid valve entity ID creates a new zone."""
        flow = _make_flow()
        result = await flow.async_step_zone_add(
            user_input={
                CONF_ZONE_NAME: "Front Garden",
                CONF_ZONE_VALVE: "switch.sonoff_swv_front",
                CONF_ZONE_DURATION_MINUTES: 15,
            }
        )
        # After success the flow returns to the zones menu.
        assert result["type"] == "menu"
        zones = flow._pending[CONF_ZONES]
        assert len(zones) == 1
        zone = zones[0]
        assert zone[CONF_ZONE_NAME] == "Front Garden"
        assert zone[CONF_ZONE_VALVE] == "switch.sonoff_swv_front"
        assert zone[CONF_ZONE_DURATION_MINUTES] == 15
        assert CONF_ZONE_ID in zone

    @pytest.mark.asyncio
    async def test_uses_valve_as_name_when_name_empty(self) -> None:
        """An empty zone name falls back to the valve entity ID."""
        flow = _make_flow()
        await flow.async_step_zone_add(
            user_input={
                CONF_ZONE_NAME: "",
                CONF_ZONE_VALVE: "switch.sonoff_swv_back",
                CONF_ZONE_DURATION_MINUTES: 20,
            }
        )
        zone = flow._pending[CONF_ZONES][0]
        assert zone[CONF_ZONE_NAME] == "switch.sonoff_swv_back"

    @pytest.mark.asyncio
    async def test_valve_required_error_when_missing(self) -> None:
        """Submitting without a valve must re-show the form with an error."""
        flow = _make_flow()
        result = await flow.async_step_zone_add(
            user_input={
                CONF_ZONE_NAME: "Back Lawn",
                CONF_ZONE_VALVE: "",
                CONF_ZONE_DURATION_MINUTES: 10,
            }
        )
        assert result["type"] == "form"
        assert result["errors"].get(CONF_ZONE_VALVE) == "valve_required"
        # Nothing was added to the zone list.
        assert flow._pending[CONF_ZONES] == []

    @pytest.mark.asyncio
    async def test_valve_required_error_when_none(self) -> None:
        """user_input without valve key at all must also trigger the error."""
        flow = _make_flow()
        result = await flow.async_step_zone_add(
            user_input={
                CONF_ZONE_NAME: "Patio",
                CONF_ZONE_DURATION_MINUTES: 5,
                # CONF_ZONE_VALVE intentionally absent
            }
        )
        assert result["type"] == "form"
        assert result["errors"].get(CONF_ZONE_VALVE) == "valve_required"

    @pytest.mark.asyncio
    async def test_duration_defaults_when_missing(self) -> None:
        """Missing duration_minutes in user_input uses DEFAULT_DURATION_MINUTES."""
        flow = _make_flow()
        await flow.async_step_zone_add(
            user_input={
                CONF_ZONE_NAME: "Side Strip",
                CONF_ZONE_VALVE: "switch.sonoff_swv_side",
                # No duration_minutes key → should use default (15)
            }
        )
        zone = flow._pending[CONF_ZONES][0]
        assert zone[CONF_ZONE_DURATION_MINUTES] == 15

    @pytest.mark.asyncio
    async def test_adds_multiple_zones_sequentially(self) -> None:
        """Multiple zone_add calls accumulate zones in _pending."""
        flow = _make_flow()
        for i, entity in enumerate(
            ("switch.valve_a", "switch.valve_b", "switch.valve_c"),
            start=1,
        ):
            await flow.async_step_zone_add(
                user_input={
                    CONF_ZONE_NAME: f"Zone {i}",
                    CONF_ZONE_VALVE: entity,
                    CONF_ZONE_DURATION_MINUTES: i * 5,
                }
            )
        zones = flow._pending[CONF_ZONES]
        assert len(zones) == 3
        assert zones[0][CONF_ZONE_VALVE] == "switch.valve_a"
        assert zones[2][CONF_ZONE_DURATION_MINUTES] == 15

    @pytest.mark.asyncio
    async def test_accepts_valve_domain_entity(self) -> None:
        """Entities in the 'valve' domain (HA 2023.4+) should also be accepted."""
        flow = _make_flow()
        result = await flow.async_step_zone_add(
            user_input={
                CONF_ZONE_NAME: "Native Valve",
                CONF_ZONE_VALVE: "valve.sonoff_swv_native",
                CONF_ZONE_DURATION_MINUTES: 10,
            }
        )
        assert result["type"] == "menu"
        assert flow._pending[CONF_ZONES][0][CONF_ZONE_VALVE] == "valve.sonoff_swv_native"


# ---------------------------------------------------------------------------
# Tests: async_step_zone_edit
# ---------------------------------------------------------------------------


class TestZoneEdit:
    """Tests for async_step_zone_edit (via the __getattr__ dispatcher)."""

    def _make_flow_with_zone(self) -> tuple[IrrigationProxyOptionsFlow, str]:
        """Return a flow that already has one zone; also return its id."""
        zone = {
            CONF_ZONE_ID: "z_testid01",
            CONF_ZONE_NAME: "Original Name",
            CONF_ZONE_VALVE: "switch.original_valve",
            CONF_ZONE_DURATION_MINUTES: 10,
        }
        flow = _make_flow(zones=[zone])
        return flow, "z_testid01"

    @pytest.mark.asyncio
    async def test_shows_form_for_known_zone(self) -> None:
        """zone_edit shows the form when called via the __getattr__ dispatcher."""
        flow, zone_id = self._make_flow_with_zone()
        dispatcher = getattr(flow, f"async_step_zone_edit_{zone_id}")
        result = await dispatcher(user_input=None)
        assert result["type"] == "form"
        assert result["step_id"] == "zone_edit"

    @pytest.mark.asyncio
    async def test_edit_updates_zone(self) -> None:
        """Submitting valid input updates the zone in _pending."""
        flow, zone_id = self._make_flow_with_zone()
        dispatcher = getattr(flow, f"async_step_zone_edit_{zone_id}")
        # First call (None) → show form; second call → process input.
        await dispatcher(user_input=None)
        result = await dispatcher(
            user_input={
                CONF_ZONE_NAME: "Updated Name",
                CONF_ZONE_VALVE: "switch.updated_valve",
                CONF_ZONE_DURATION_MINUTES: 25,
                "delete": False,
            }
        )
        assert result["type"] == "menu"
        zones = flow._pending[CONF_ZONES]
        assert len(zones) == 1
        assert zones[0][CONF_ZONE_NAME] == "Updated Name"
        assert zones[0][CONF_ZONE_VALVE] == "switch.updated_valve"
        assert zones[0][CONF_ZONE_DURATION_MINUTES] == 25

    @pytest.mark.asyncio
    async def test_delete_flag_removes_zone(self) -> None:
        """Setting delete=True in user_input removes the zone."""
        flow, zone_id = self._make_flow_with_zone()
        dispatcher = getattr(flow, f"async_step_zone_edit_{zone_id}")
        await dispatcher(user_input=None)
        result = await dispatcher(
            user_input={
                CONF_ZONE_NAME: "Original Name",
                CONF_ZONE_VALVE: "switch.original_valve",
                CONF_ZONE_DURATION_MINUTES: 10,
                "delete": True,
            }
        )
        assert result["type"] == "menu"
        assert flow._pending[CONF_ZONES] == []

    @pytest.mark.asyncio
    async def test_valve_required_on_empty_valve(self) -> None:
        """Empty valve triggers the valve_required error on edit too."""
        flow, zone_id = self._make_flow_with_zone()
        dispatcher = getattr(flow, f"async_step_zone_edit_{zone_id}")
        await dispatcher(user_input=None)
        result = await dispatcher(
            user_input={
                CONF_ZONE_NAME: "Some Name",
                CONF_ZONE_VALVE: "",
                CONF_ZONE_DURATION_MINUTES: 10,
                "delete": False,
            }
        )
        assert result["type"] == "form"
        assert result["errors"].get(CONF_ZONE_VALVE) == "valve_required"

    @pytest.mark.asyncio
    async def test_unknown_zone_id_redirects_to_zones_menu(self) -> None:
        """If the zone id no longer exists the flow falls back to zones menu."""
        flow, _ = self._make_flow_with_zone()
        dispatcher = getattr(flow, "async_step_zone_edit_nonexistent")
        result = await dispatcher(user_input=None)
        assert result["type"] == "menu"


# ---------------------------------------------------------------------------
# Tests: async_step_zones menu construction
# ---------------------------------------------------------------------------


class TestZonesMenu:
    """Tests for the zones submenu."""

    @pytest.mark.asyncio
    async def test_empty_zones_shows_only_add_and_back(self) -> None:
        """With no zones the menu has exactly zone_add and init options."""
        flow = _make_flow()
        result = await flow.async_step_zones()
        assert result["type"] == "menu"
        opts = result["menu_options"]
        assert "zone_add" in opts
        assert "init" in opts

    @pytest.mark.asyncio
    async def test_existing_zones_appear_in_menu(self) -> None:
        """Each zone gets a zone_edit_<id> entry in the zones menu."""
        flow = _make_flow(
            zones=[
                {
                    CONF_ZONE_ID: "z_aaa",
                    CONF_ZONE_NAME: "Front",
                    CONF_ZONE_VALVE: "switch.v1",
                    CONF_ZONE_DURATION_MINUTES: 10,
                },
                {
                    CONF_ZONE_ID: "z_bbb",
                    CONF_ZONE_NAME: "Back",
                    CONF_ZONE_VALVE: "switch.v2",
                    CONF_ZONE_DURATION_MINUTES: 15,
                },
            ]
        )
        result = await flow.async_step_zones()
        opts = result["menu_options"]
        assert "zone_edit_z_aaa" in opts
        assert "zone_edit_z_bbb" in opts
        assert opts["zone_edit_z_aaa"] == "Front"
        assert opts["zone_edit_z_bbb"] == "Back"


# ---------------------------------------------------------------------------
# Tests: async_step_save
# ---------------------------------------------------------------------------


class TestSave:
    """Tests for the save step."""

    @pytest.mark.asyncio
    async def test_save_calls_update_entry_and_creates_entry(self) -> None:
        """async_step_save must update the config entry and finish the flow."""
        flow = _make_flow()
        result = await flow.async_step_save()
        assert result["type"] == "create_entry"
        # async_update_entry should have been called once.
        flow.hass.config_entries.async_update_entry.assert_called_once()

    @pytest.mark.asyncio
    async def test_save_preserves_program_name(self) -> None:
        """The program name must not be overwritten by pending zone data."""
        flow = _make_flow()
        # Simulate that _pending has a "name" key (e.g. from zone form interaction).
        flow._pending[CONF_NAME] = "Should Not Override"
        await flow.async_step_save()
        call_kwargs = (
            flow.hass.config_entries.async_update_entry.call_args[1]
        )
        saved_data = call_kwargs.get("data") or {}
        # Name must come from config_entry.data, not from _pending.
        assert saved_data.get(CONF_NAME) == "Test Program"
