"""Tests for number entities (in-memory parameter tuning)."""

from __future__ import annotations

import pytest

from custom_components.irrigation_proxy.safety import SafetyManager
from custom_components.irrigation_proxy.sequencer import Sequencer
from custom_components.irrigation_proxy.zone import Zone

from .conftest import make_mock_hass


def _make_zone(
    name: str = "Rasen",
    valve_entity_id: str = "switch.valve_1",
    duration_minutes: int = 15,
) -> Zone:
    return Zone(name=name, valve_entity_id=valve_entity_id, duration_minutes=duration_minutes)


class TestZoneDuration:
    """Tests for in-memory zone duration changes."""

    def test_initial_value(self) -> None:
        zone = _make_zone(duration_minutes=20)
        assert zone.duration_minutes == 20
        assert zone.duration_seconds == 1200

    def test_set_duration(self) -> None:
        zone = _make_zone(duration_minutes=15)
        zone.duration_minutes = 30
        assert zone.duration_minutes == 30
        assert zone.duration_seconds == 1800

    def test_duration_affects_sequencer_total(self) -> None:
        hass = make_mock_hass({})
        zones = [
            _make_zone("Z1", "switch.v1", 10),
            _make_zone("Z2", "switch.v2", 20),
        ]
        safety = SafetyManager(hass, max_runtime_minutes=60)
        seq = Sequencer(hass=hass, zones=zones, safety=safety, pause_seconds=30)

        assert seq.total_program_seconds_idle == (10 * 60) + (20 * 60) + 30

        # Change zone 1 duration
        zones[0].duration_minutes = 5
        assert seq.total_program_seconds_idle == (5 * 60) + (20 * 60) + 30


class TestInterZoneDelay:
    """Tests for in-memory inter-zone delay changes."""

    def test_initial_value(self) -> None:
        hass = make_mock_hass({})
        safety = SafetyManager(hass, max_runtime_minutes=60)
        seq = Sequencer(hass=hass, zones=[], safety=safety, pause_seconds=30)
        assert seq.pause_seconds == 30

    def test_set_pause_seconds(self) -> None:
        hass = make_mock_hass({})
        safety = SafetyManager(hass, max_runtime_minutes=60)
        seq = Sequencer(hass=hass, zones=[], safety=safety, pause_seconds=30)
        seq.pause_seconds = 60
        assert seq.pause_seconds == 60

    def test_negative_clamped_to_zero(self) -> None:
        hass = make_mock_hass({})
        safety = SafetyManager(hass, max_runtime_minutes=60)
        seq = Sequencer(hass=hass, zones=[], safety=safety, pause_seconds=30)
        seq.pause_seconds = -10
        assert seq.pause_seconds == 0

    def test_delay_affects_total_program(self) -> None:
        hass = make_mock_hass({})
        zones = [_make_zone("Z1", "switch.v1", 10), _make_zone("Z2", "switch.v2", 10)]
        safety = SafetyManager(hass, max_runtime_minutes=60)
        seq = Sequencer(hass=hass, zones=zones, safety=safety, pause_seconds=30)

        total_with_30 = seq.total_program_seconds_idle
        seq.pause_seconds = 60
        total_with_60 = seq.total_program_seconds_idle

        assert total_with_60 - total_with_30 == 30  # 1 gap, 30s increase


class TestMaxRuntime:
    """Tests for in-memory max runtime (deadman timer) changes."""

    def test_initial_value(self) -> None:
        hass = make_mock_hass({})
        safety = SafetyManager(hass, max_runtime_minutes=60)
        assert safety.max_runtime_minutes == 60

    def test_set_max_runtime(self) -> None:
        hass = make_mock_hass({})
        safety = SafetyManager(hass, max_runtime_minutes=60)
        safety.max_runtime_minutes = 90
        assert safety.max_runtime_minutes == 90
        # Internal seconds should match
        assert safety._max_runtime_seconds == 5400

    def test_minimum_clamped(self) -> None:
        hass = make_mock_hass({})
        safety = SafetyManager(hass, max_runtime_minutes=60)
        safety.max_runtime_minutes = 0
        assert safety.max_runtime_minutes == 1  # clamped to 1 min minimum
