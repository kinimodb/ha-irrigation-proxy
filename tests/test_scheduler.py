"""Tests for the scheduler."""

from __future__ import annotations

from datetime import datetime, time, timezone
from unittest.mock import MagicMock

import pytest

from custom_components.irrigation_proxy.scheduler import (
    ProgramScheduler,
    ScheduleConfig,
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
        monday = datetime(2026, 4, 13, 6, 0, tzinfo=timezone.utc)
        tuesday = datetime(2026, 4, 14, 6, 0, tzinfo=timezone.utc)
        wednesday = datetime(2026, 4, 15, 6, 0, tzinfo=timezone.utc)
        assert cfg.matches_today(monday)
        assert not cfg.matches_today(tuesday)
        assert cfg.matches_today(wednesday)


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
        now = datetime(2026, 4, 13, 7, 0, tzinfo=timezone.utc)
        nxt = next_fire_time(now, cfg)
        assert nxt is not None
        assert nxt.day == 15 and nxt.hour == 6


class _StubSequencer:
    def __init__(self) -> None:
        self.started = 0

    async def start(self) -> None:
        self.started += 1


@pytest.mark.asyncio
async def test_scheduler_skips_on_non_matching_weekday() -> None:
    sequencer = _StubSequencer()
    cfg = ScheduleConfig(
        enabled=True,
        start_times=[time(6, 0)],
        weekdays={"wed"},
    )
    sched = ProgramScheduler(
        hass=MagicMock(),
        sequencer=sequencer,  # type: ignore[arg-type]
        get_config=lambda: cfg,
    )
    # Monday – not in mask
    await sched._handle_fire(datetime(2026, 4, 13, 6, 0, tzinfo=timezone.utc))
    assert sequencer.started == 0


@pytest.mark.asyncio
async def test_scheduler_fires_on_matching_weekday() -> None:
    sequencer = _StubSequencer()
    cfg = ScheduleConfig(
        enabled=True,
        start_times=[time(6, 0)],
        weekdays={"mon"},
    )
    sched = ProgramScheduler(
        hass=MagicMock(),
        sequencer=sequencer,  # type: ignore[arg-type]
        get_config=lambda: cfg,
    )
    await sched._handle_fire(datetime(2026, 4, 13, 6, 0, tzinfo=timezone.utc))
    assert sequencer.started == 1
    assert sched.last_fire is not None


@pytest.mark.asyncio
async def test_scheduler_noop_when_disabled() -> None:
    sequencer = _StubSequencer()
    cfg = ScheduleConfig(
        enabled=False,
        start_times=[time(6, 0)],
        weekdays={"mon"},
    )
    sched = ProgramScheduler(
        hass=MagicMock(),
        sequencer=sequencer,  # type: ignore[arg-type]
        get_config=lambda: cfg,
    )
    await sched._handle_fire(datetime(2026, 4, 13, 6, 0, tzinfo=timezone.utc))
    assert sequencer.started == 0
