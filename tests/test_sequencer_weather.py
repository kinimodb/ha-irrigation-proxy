"""Tests for the weather-based runtime factor in the Sequencer."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.irrigation_proxy.const import (
    DEFAULT_WEATHER_FACTOR,
    EVENT_ZONE_STARTED,
    WEATHER_FACTOR_MAX,
)
from custom_components.irrigation_proxy.coordinator import _parse_weather_factor
from custom_components.irrigation_proxy.safety import SafetyManager
from custom_components.irrigation_proxy.sequencer import Sequencer, SequencerState
from custom_components.irrigation_proxy.zone import Zone

from .conftest import FakeState, make_mock_hass


def _make_zone(
    name: str = "Zone 1",
    valve_entity_id: str = "switch.valve_1",
    duration_minutes: int = 10,
) -> Zone:
    return Zone(
        name=name,
        valve_entity_id=valve_entity_id,
        duration_minutes=duration_minutes,
    )


def _make_hass_with_tracking(
    state_map: dict[str, FakeState],
) -> tuple[MagicMock, list[str], list[tuple[str, object]]]:
    """HASS mock that tracks open/close calls and records fired events."""
    hass = make_mock_hass(state_map)
    hass.async_create_task = MagicMock(
        side_effect=lambda coro, *a, **kw: asyncio.ensure_future(coro)
    )

    opened: list[str] = []
    fired: list[tuple[str, object]] = []

    async def _track_call(domain: str, service: str, data: dict, **_: object) -> None:
        eid = data["entity_id"]
        if service == "turn_on":
            opened.append(eid)
            state_map[eid] = FakeState("on")
        elif service == "turn_off":
            state_map[eid] = FakeState("off")

    hass.services.async_call = AsyncMock(side_effect=_track_call)
    hass.states.get = MagicMock(side_effect=lambda eid: state_map.get(eid))
    hass.bus = MagicMock()
    hass.bus.async_fire = MagicMock(
        side_effect=lambda event, data=None: fired.append((event, data or {}))
    )
    return hass, opened, fired


def _make_sequencer(
    hass: MagicMock,
    zones: list[Zone],
    provider: object | None,
) -> Sequencer:
    safety = SafetyManager(hass, max_runtime_minutes=60)
    return Sequencer(
        hass=hass,
        zones=zones,
        safety=safety,
        pause_seconds=0,
        adjustment_provider=provider,
    )


class TestParseWeatherFactor:
    """Tests for the coordinator's parse/clamp helper."""

    def test_none_defaults_to_one(self) -> None:
        assert _parse_weather_factor(None) == DEFAULT_WEATHER_FACTOR

    def test_unknown_defaults_to_one(self) -> None:
        assert _parse_weather_factor("unknown") == DEFAULT_WEATHER_FACTOR

    def test_unavailable_defaults_to_one(self) -> None:
        assert _parse_weather_factor("unavailable") == DEFAULT_WEATHER_FACTOR

    def test_empty_string_defaults_to_one(self) -> None:
        assert _parse_weather_factor("") == DEFAULT_WEATHER_FACTOR

    def test_non_numeric_defaults_to_one(self) -> None:
        assert _parse_weather_factor("not-a-number") == DEFAULT_WEATHER_FACTOR

    def test_valid_float_passed_through(self) -> None:
        assert _parse_weather_factor("0.8") == 0.8

    def test_upper_clamp(self) -> None:
        assert _parse_weather_factor("5.0") == WEATHER_FACTOR_MAX

    def test_lower_clamp(self) -> None:
        assert _parse_weather_factor("-1.0") == 0.0


class TestSequencerWeatherFactor:
    """Tests for the sequencer's use of the weather factor."""

    @pytest.mark.asyncio
    async def test_factor_shortens_sleep(self) -> None:
        state_map = {"switch.valve_1": FakeState("off")}
        hass, opened, _ = _make_hass_with_tracking(state_map)
        zones = [_make_zone(duration_minutes=10)]  # 600 s base

        seq = _make_sequencer(hass, zones, provider=lambda: (0.5, False))

        sleeps: list[float] = []

        async def _record_sleep(seconds: float) -> None:
            sleeps.append(float(seconds))

        with patch("asyncio.sleep", side_effect=_record_sleep):
            await seq.start()
            if seq._task:
                await seq._task

        # 10 min × 0.5 = 300 s must show up among the sleeps.
        assert 300 in [int(s) for s in sleeps]
        # Base duration must NOT have been used.
        assert 600 not in [int(s) for s in sleeps]
        assert opened == ["switch.valve_1"]

    @pytest.mark.asyncio
    async def test_factor_ignored_uses_full_duration(self) -> None:
        state_map = {"switch.valve_1": FakeState("off")}
        hass, opened, _ = _make_hass_with_tracking(state_map)
        zones = [_make_zone(duration_minutes=10)]

        # ignored=True – provider still returns 0.5 but must be bypassed.
        seq = _make_sequencer(hass, zones, provider=lambda: (0.5, True))

        sleeps: list[float] = []

        async def _record_sleep(seconds: float) -> None:
            sleeps.append(float(seconds))

        with patch("asyncio.sleep", side_effect=_record_sleep):
            await seq.start()
            if seq._task:
                await seq._task

        assert 600 in [int(s) for s in sleeps]
        assert 300 not in [int(s) for s in sleeps]
        assert opened == ["switch.valve_1"]

    @pytest.mark.asyncio
    async def test_factor_zero_skips_zone(self) -> None:
        state_map = {
            "switch.valve_1": FakeState("off"),
            "switch.valve_2": FakeState("off"),
        }
        hass, opened, fired = _make_hass_with_tracking(state_map)
        zones = [
            _make_zone("Zone 1", "switch.valve_1", duration_minutes=10),
            _make_zone("Zone 2", "switch.valve_2", duration_minutes=10),
        ]

        seq = _make_sequencer(hass, zones, provider=lambda: (0.0, False))

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await seq.start()
            if seq._task:
                await seq._task

        # No valve should have been opened.
        assert opened == []

        # Each zone fires a ZONE_STARTED event with skipped=True.
        zone_started_events = [
            data for name, data in fired if name == EVENT_ZONE_STARTED
        ]
        assert len(zone_started_events) == 2
        assert all(evt.get("skipped") is True for evt in zone_started_events)
        assert all(
            evt.get("reason") == "weather_factor_zero"
            for evt in zone_started_events
        )
        assert seq.state == SequencerState.IDLE

    @pytest.mark.asyncio
    async def test_no_provider_runs_full_duration(self) -> None:
        state_map = {"switch.valve_1": FakeState("off")}
        hass, opened, _ = _make_hass_with_tracking(state_map)
        zones = [_make_zone(duration_minutes=10)]

        seq = _make_sequencer(hass, zones, provider=None)

        sleeps: list[float] = []

        async def _record_sleep(seconds: float) -> None:
            sleeps.append(float(seconds))

        with patch("asyncio.sleep", side_effect=_record_sleep):
            await seq.start()
            if seq._task:
                await seq._task

        assert 600 in [int(s) for s in sleeps]
        assert opened == ["switch.valve_1"]

    @pytest.mark.asyncio
    async def test_provider_exception_falls_back_to_one(self) -> None:
        state_map = {"switch.valve_1": FakeState("off")}
        hass, opened, _ = _make_hass_with_tracking(state_map)
        zones = [_make_zone(duration_minutes=10)]

        def _broken() -> tuple[float, bool]:
            raise RuntimeError("sensor disappeared")

        seq = _make_sequencer(hass, zones, provider=_broken)

        sleeps: list[float] = []

        async def _record_sleep(seconds: float) -> None:
            sleeps.append(float(seconds))

        with patch("asyncio.sleep", side_effect=_record_sleep):
            await seq.start()
            if seq._task:
                await seq._task

        # Provider raised → sequencer must not crash and must use base duration.
        assert 600 in [int(s) for s in sleeps]
        assert opened == ["switch.valve_1"]

    def test_current_factor_idle_reads_provider(self) -> None:
        hass = make_mock_hass({"switch.valve_1": FakeState("off")})
        zones = [_make_zone(duration_minutes=10)]
        seq = _make_sequencer(hass, zones, provider=lambda: (0.7, False))

        assert seq.state == SequencerState.IDLE
        assert seq.current_factor == pytest.approx(0.7)

    def test_current_factor_idle_ignored_returns_one(self) -> None:
        hass = make_mock_hass({"switch.valve_1": FakeState("off")})
        zones = [_make_zone(duration_minutes=10)]
        seq = _make_sequencer(hass, zones, provider=lambda: (0.3, True))

        assert seq.current_factor == pytest.approx(DEFAULT_WEATHER_FACTOR)

    def test_zone_effective_seconds_rounds(self) -> None:
        hass = make_mock_hass({"switch.valve_1": FakeState("off")})
        zones = [_make_zone(duration_minutes=10)]  # 600 s
        seq = _make_sequencer(hass, zones, provider=lambda: (0.333, False))

        # 600 × 0.333 = 199.8 → rounds to 200.
        assert seq._zone_effective_seconds(0) == 200

    def test_progress_exposes_weather_factor(self) -> None:
        hass = make_mock_hass({"switch.valve_1": FakeState("off")})
        zones = [_make_zone(duration_minutes=10)]
        seq = _make_sequencer(hass, zones, provider=lambda: (0.5, False))

        p = seq.progress
        assert p["weather_factor"] == pytest.approx(0.5)
        assert p["zones"][0]["duration_seconds"] == 600
        assert p["zones"][0]["effective_duration_seconds"] == 300
