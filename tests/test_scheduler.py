"""Tests for the scheduler."""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
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


class TestNextFireTimeDST:
    """W2: next_fire_time must use .astimezone() not tzinfo=now.tzinfo.

    When `now` carries a fixed UTC offset (e.g. from DST summer time),
    the old datetime.combine(..., tzinfo=now.tzinfo) attached that fixed
    offset to every candidate, even dates that would have a different
    offset after DST changeover.  .astimezone() lets Python apply the
    correct local DST offset for each candidate date.
    """

    def _cfg(self) -> ScheduleConfig:
        return ScheduleConfig(
            enabled=True,
            start_times=[time(6, 0)],
            weekdays={"mon"},
        )

    def test_candidate_uses_local_astimezone_not_now_tzinfo(self) -> None:
        """Returned datetime must derive its tzinfo from .astimezone(), not
        from the fixed-offset tzinfo attached to `now`."""
        # Simulate `now` arriving with a fixed UTC+2 offset (e.g. CEST).
        utc_plus_2 = timezone(timedelta(hours=2))
        # Monday 2026-04-13 05:00 UTC+2 = 03:00 UTC
        now = datetime(2026, 4, 13, 5, 0, tzinfo=utc_plus_2)

        result = next_fire_time(now, self._cfg())
        assert result is not None

        # The fix: result's tzinfo must NOT be the fixed UTC+2 from `now`.
        # .astimezone() gives the local system timezone (UTC in the test env).
        # We verify by checking that the result's UTC offset is not forced to +2.
        result_utcoffset = result.utcoffset()
        assert result_utcoffset is not None
        # The system-local offset (UTC+0 in CI) must be used, not UTC+2.
        # If the old buggy code ran, utcoffset() would always equal timedelta(hours=2).
        system_utcoffset = datetime.now().astimezone().utcoffset()
        assert result_utcoffset == system_utcoffset, (
            f"Expected result to use system UTC offset ({system_utcoffset}) "
            f"not the fixed offset from `now` ({result_utcoffset})"
        )

    def test_result_is_timezone_aware(self) -> None:
        now = datetime(2026, 4, 13, 5, 0, tzinfo=timezone.utc)
        result = next_fire_time(now, self._cfg())
        assert result is not None
        assert result.tzinfo is not None, "result must be timezone-aware"

    def test_comparison_with_fixed_offset_now_still_correct(self) -> None:
        """Comparison across different tzinfo objects works because Python
        normalises to UTC; verify the returned date/hour are sensible."""
        utc_plus_2 = timezone(timedelta(hours=2))
        # 05:00 UTC+2 → Monday, 6:00 still ahead today.
        now = datetime(2026, 4, 13, 5, 0, tzinfo=utc_plus_2)
        result = next_fire_time(now, self._cfg())
        assert result is not None
        assert result.day == 13
        assert result.hour == 6


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
