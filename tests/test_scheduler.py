"""Tests for the scheduler / rain-adjust logic."""

from __future__ import annotations

from datetime import datetime, time, timezone
from unittest.mock import MagicMock

import pytest

from custom_components.irrigation_proxy.const import (
    ASSUMED_FLOW_MM_PER_MIN,
    RAIN_ADJUST_HARD,
    RAIN_ADJUST_OFF,
    RAIN_ADJUST_SCALE,
)
from custom_components.irrigation_proxy.scheduler import (
    ProgramScheduler,
    ScheduleConfig,
    compute_duration_multiplier,
    format_start_times,
    next_fire_time,
    parse_start_times,
)


class TestParseStartTimes:
    def test_empty_input(self) -> None:
        assert parse_start_times(None) == []
        assert parse_start_times("") == []
        assert parse_start_times([]) == []

    def test_comma_separated_string(self) -> None:
        assert parse_start_times("06:00, 20:30") == [time(6, 0), time(20, 30)]

    def test_list_input(self) -> None:
        assert parse_start_times(["06:00", "07:30"]) == [
            time(6, 0),
            time(7, 30),
        ]

    def test_ignores_invalid_entries(self) -> None:
        assert parse_start_times("06:00, notatime, 25:00, 07:45") == [
            time(6, 0),
            time(7, 45),
        ]

    def test_deduplicates_and_sorts(self) -> None:
        assert parse_start_times("20:00, 06:00, 06:00") == [
            time(6, 0),
            time(20, 0),
        ]


class TestFormatStartTimes:
    def test_round_trip(self) -> None:
        parsed = parse_start_times("06:00, 20:30")
        assert format_start_times(parsed) == "06:00, 20:30"

    def test_handles_strings(self) -> None:
        assert format_start_times(["06:00", "07:45"]) == "06:00, 07:45"

    def test_empty(self) -> None:
        assert format_start_times(None) == ""
        assert format_start_times([]) == ""


class TestScheduleConfigMatching:
    def test_disabled_never_matches(self) -> None:
        cfg = ScheduleConfig(enabled=False, weekdays={"mon"})
        now = datetime(2026, 4, 13, 6, 0, tzinfo=timezone.utc)  # Mon
        assert cfg.matches_today(now) is False

    def test_matches_configured_weekday(self) -> None:
        cfg = ScheduleConfig(enabled=True, weekdays={"mon", "wed"})
        monday = datetime(2026, 4, 13, 6, 0, tzinfo=timezone.utc)  # Mon
        tuesday = datetime(2026, 4, 14, 6, 0, tzinfo=timezone.utc)  # Tue
        wednesday = datetime(2026, 4, 15, 6, 0, tzinfo=timezone.utc)  # Wed
        assert cfg.matches_today(monday)
        assert not cfg.matches_today(tuesday)
        assert cfg.matches_today(wednesday)


class TestComputeDurationMultiplier:
    def test_off_mode_is_always_full(self) -> None:
        assert (
            compute_duration_multiplier(
                RAIN_ADJUST_OFF,
                zones_total_minutes=30,
                rain_mm=99.0,
                rain_threshold_mm=5.0,
            )
            == 1.0
        )

    def test_hard_mode_skip_above_threshold(self) -> None:
        assert (
            compute_duration_multiplier(
                RAIN_ADJUST_HARD,
                zones_total_minutes=30,
                rain_mm=5.5,
                rain_threshold_mm=5.0,
            )
            == 0.0
        )

    def test_hard_mode_full_below_threshold(self) -> None:
        assert (
            compute_duration_multiplier(
                RAIN_ADJUST_HARD,
                zones_total_minutes=30,
                rain_mm=4.0,
                rain_threshold_mm=5.0,
            )
            == 1.0
        )

    def test_scale_mode_skip_when_rain_matches_plan(self) -> None:
        # 30 min × 0.25 mm/min = 7.5 mm planned → 8 mm rain means skip
        assert (
            compute_duration_multiplier(
                RAIN_ADJUST_SCALE,
                zones_total_minutes=30,
                rain_mm=8.0,
                rain_threshold_mm=5.0,
            )
            == 0.0
        )

    def test_scale_mode_partial(self) -> None:
        # 30 min × 0.25 = 7.5 mm planned. 3 mm rain leaves 4.5/7.5 = 0.6
        mult = compute_duration_multiplier(
            RAIN_ADJUST_SCALE,
            zones_total_minutes=30,
            rain_mm=3.0,
            rain_threshold_mm=5.0,
        )
        assert 0.59 < mult < 0.61

    def test_scale_mode_full_when_no_rain(self) -> None:
        assert (
            compute_duration_multiplier(
                RAIN_ADJUST_SCALE,
                zones_total_minutes=30,
                rain_mm=0.0,
                rain_threshold_mm=5.0,
            )
            == 1.0
        )

    def test_assumed_flow_constant_sane(self) -> None:
        assert ASSUMED_FLOW_MM_PER_MIN > 0


class TestNextFireTime:
    def test_returns_none_when_disabled(self) -> None:
        cfg = ScheduleConfig(
            enabled=False,
            start_times=[time(6, 0)],
            weekdays={"mon"},
        )
        now = datetime(2026, 4, 13, 5, 0, tzinfo=timezone.utc)
        assert next_fire_time(now, cfg) is None

    def test_returns_today_later_time(self) -> None:
        cfg = ScheduleConfig(
            enabled=True,
            start_times=[time(6, 0), time(20, 0)],
            weekdays={"mon"},
        )
        # 2026-04-13 is a Monday
        now = datetime(2026, 4, 13, 7, 0, tzinfo=timezone.utc)
        nxt = next_fire_time(now, cfg)
        assert nxt is not None
        assert nxt.hour == 20 and nxt.day == 13

    def test_skips_disallowed_weekdays(self) -> None:
        cfg = ScheduleConfig(
            enabled=True,
            start_times=[time(6, 0)],
            weekdays={"wed"},
        )
        # 2026-04-13 is Monday → first Wednesday = 2026-04-15
        now = datetime(2026, 4, 13, 7, 0, tzinfo=timezone.utc)
        nxt = next_fire_time(now, cfg)
        assert nxt is not None
        assert nxt.day == 15 and nxt.hour == 6


class _StubZone:
    def __init__(self, minutes: int) -> None:
        self.duration_minutes = minutes


class _StubSequencer:
    def __init__(self, zones: list[_StubZone]) -> None:
        self.zones = zones
        self.started_with: float | None = None
        self.state = MagicMock()

    async def start(self, duration_multiplier: float = 1.0) -> None:
        self.started_with = duration_multiplier


class _StubWeather:
    def __init__(self, total_rain: float, threshold: float) -> None:
        self.total_rain_mm = total_rain
        self.rain_threshold_mm = threshold


@pytest.mark.asyncio
async def test_scheduler_skips_on_non_matching_weekday() -> None:
    zones = [_StubZone(15), _StubZone(15)]
    sequencer = _StubSequencer(zones)
    cfg = ScheduleConfig(
        enabled=True,
        start_times=[time(6, 0)],
        weekdays={"wed"},
        rain_adjust_mode=RAIN_ADJUST_OFF,
    )
    sched = ProgramScheduler(
        hass=MagicMock(),
        sequencer=sequencer,  # type: ignore[arg-type]
        weather=None,
        get_config=lambda: cfg,
    )
    # 2026-04-13 is Monday → should NOT fire (weekday mismatch)
    await sched._handle_fire(datetime(2026, 4, 13, 6, 0, tzinfo=timezone.utc))
    assert sequencer.started_with is None


@pytest.mark.asyncio
async def test_scheduler_fires_with_scale_multiplier() -> None:
    zones = [_StubZone(30)]  # 30 min planned → 7.5 mm planned
    sequencer = _StubSequencer(zones)
    weather = _StubWeather(total_rain=3.0, threshold=5.0)
    cfg = ScheduleConfig(
        enabled=True,
        start_times=[time(6, 0)],
        weekdays={"mon"},
        rain_adjust_mode=RAIN_ADJUST_SCALE,
    )
    sched = ProgramScheduler(
        hass=MagicMock(),
        sequencer=sequencer,  # type: ignore[arg-type]
        weather=weather,  # type: ignore[arg-type]
        get_config=lambda: cfg,
    )
    # Monday 2026-04-13 06:00
    await sched._handle_fire(datetime(2026, 4, 13, 6, 0, tzinfo=timezone.utc))
    assert sequencer.started_with is not None
    assert 0.59 < sequencer.started_with < 0.61


@pytest.mark.asyncio
async def test_scheduler_skips_with_hard_rain() -> None:
    zones = [_StubZone(15)]
    sequencer = _StubSequencer(zones)
    weather = _StubWeather(total_rain=10.0, threshold=5.0)
    cfg = ScheduleConfig(
        enabled=True,
        start_times=[time(6, 0)],
        weekdays={"mon"},
        rain_adjust_mode=RAIN_ADJUST_HARD,
    )
    sched = ProgramScheduler(
        hass=MagicMock(),
        sequencer=sequencer,  # type: ignore[arg-type]
        weather=weather,  # type: ignore[arg-type]
        get_config=lambda: cfg,
    )
    await sched._handle_fire(datetime(2026, 4, 13, 6, 0, tzinfo=timezone.utc))
    assert sequencer.started_with is None
    assert sched.last_skip_reason is not None
    assert "rain" in sched.last_skip_reason
