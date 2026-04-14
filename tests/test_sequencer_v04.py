"""Tests for v0.4.0 sequencer additions: per-zone durations, totals, multiplier."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from custom_components.irrigation_proxy.safety import SafetyManager
from custom_components.irrigation_proxy.sequencer import Sequencer, SequencerState
from custom_components.irrigation_proxy.zone import Zone

from .conftest import FakeState, make_mock_hass


def _make_zone(name: str, valve: str, minutes: int) -> Zone:
    return Zone(name=name, valve_entity_id=valve, duration_minutes=minutes)


def _make_sequencer(
    zones: list[Zone], *, pause_seconds: int = 30
) -> Sequencer:
    hass = make_mock_hass({z.valve_entity_id: FakeState("off") for z in zones})
    safety = SafetyManager(hass, max_runtime_minutes=60)
    return Sequencer(
        hass=hass,
        zones=zones,
        safety=safety,
        pause_seconds=pause_seconds,
    )


class TestZoneDurationSeconds:
    def test_duration_seconds_matches_minutes(self) -> None:
        z = _make_zone("Z1", "switch.v1", 7)
        assert z.duration_seconds == 7 * 60


class TestTotalProgramSecondsIdle:
    def test_single_zone_no_gap(self) -> None:
        zones = [_make_zone("Z1", "switch.v1", 5)]
        seq = _make_sequencer(zones, pause_seconds=60)
        # One zone → no inter-zone gap
        assert seq.total_program_seconds_idle == 5 * 60

    def test_multiple_zones_with_gaps(self) -> None:
        zones = [
            _make_zone("Z1", "switch.v1", 5),
            _make_zone("Z2", "switch.v2", 10),
            _make_zone("Z3", "switch.v3", 7),
        ]
        seq = _make_sequencer(zones, pause_seconds=60)
        # 5+10+7 min + 2 gaps × 60 s = 22*60 + 120 = 1440
        assert seq.total_program_seconds_idle == 22 * 60 + 120

    def test_zero_pause(self) -> None:
        zones = [
            _make_zone("Z1", "switch.v1", 3),
            _make_zone("Z2", "switch.v2", 4),
        ]
        seq = _make_sequencer(zones, pause_seconds=0)
        assert seq.total_program_seconds_idle == 7 * 60


class TestTotalRemainingSeconds:
    def test_idle_equals_total_program(self) -> None:
        zones = [
            _make_zone("Z1", "switch.v1", 5),
            _make_zone("Z2", "switch.v2", 5),
        ]
        seq = _make_sequencer(zones, pause_seconds=30)
        assert seq.total_remaining_seconds == seq.total_program_seconds_idle

    def test_running_sums_current_plus_pending(self) -> None:
        zones = [
            _make_zone("Z1", "switch.v1", 10),
            _make_zone("Z2", "switch.v2", 5),
        ]
        seq = _make_sequencer(zones, pause_seconds=30)

        seq._state = SequencerState.RUNNING
        seq._current_index = 0
        seq._current_zone = zones[0]
        seq._current_zone_duration_seconds = 10 * 60
        # Started 2 min ago → 8 min remaining on zone 1
        seq._zone_started_at = datetime.now(timezone.utc) - timedelta(minutes=2)
        seq._duration_multiplier = 1.0

        total = seq.total_remaining_seconds
        assert total is not None
        # 8 min (480s) + 30 s gap + 5 min zone 2 (300s) = 810s (±2s tolerance)
        assert 808 <= total <= 812


class TestDurationMultiplierInProgress:
    def test_progress_reports_multiplier(self) -> None:
        zones = [_make_zone("Z1", "switch.v1", 10)]
        seq = _make_sequencer(zones, pause_seconds=0)
        seq._duration_multiplier = 0.5
        p = seq.progress
        assert p["duration_multiplier"] == 0.5
        assert p["pause_seconds"] == 0
        assert p["zones"][0]["duration_minutes"] == 10
        assert p["zones"][0]["duration_seconds"] == 600


class TestPauseSecondsSetter:
    def test_update_pause_seconds(self) -> None:
        zones = [_make_zone("Z1", "switch.v1", 10)]
        seq = _make_sequencer(zones, pause_seconds=30)
        seq.pause_seconds = 120
        assert seq.pause_seconds == 120

    def test_negative_pause_clamped_to_zero(self) -> None:
        zones = [_make_zone("Z1", "switch.v1", 10)]
        seq = _make_sequencer(zones, pause_seconds=30)
        seq.pause_seconds = -5
        assert seq.pause_seconds == 0
