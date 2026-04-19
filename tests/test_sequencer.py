"""Tests for the Sequencer module."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.irrigation_proxy.safety import SafetyManager
from custom_components.irrigation_proxy.sequencer import Sequencer, SequencerState
from custom_components.irrigation_proxy.zone import Zone

from .conftest import FakeState, make_mock_hass


def _make_zone(
    name: str = "Zone 1",
    valve_entity_id: str = "switch.valve_1",
    duration_minutes: int = 15,
) -> Zone:
    return Zone(name=name, valve_entity_id=valve_entity_id, duration_minutes=duration_minutes)


def _make_zones(count: int = 3, duration: int = 15) -> list[Zone]:
    return [
        _make_zone(
            name=f"Zone {i + 1}",
            valve_entity_id=f"switch.valve_{i + 1}",
            duration_minutes=duration,
        )
        for i in range(count)
    ]


def _make_sequencer(
    hass: MagicMock | None = None,
    zones: list[Zone] | None = None,
    safety: SafetyManager | None = None,
    pause_seconds: int = 0,
    on_complete: MagicMock | None = None,
) -> Sequencer:
    """Create a Sequencer with sensible test defaults."""
    if hass is None:
        hass = make_mock_hass(
            {f"switch.valve_{i + 1}": FakeState("off") for i in range(5)}
        )
    if zones is None:
        zones = _make_zones()
    if safety is None:
        safety = SafetyManager(hass, max_runtime_minutes=60)
    return Sequencer(
        hass=hass,
        zones=zones,
        safety=safety,
        pause_seconds=pause_seconds,
        on_complete=on_complete,
    )


class TestSequencerInit:
    """Tests for initial Sequencer state."""

    def test_starts_idle(self) -> None:
        seq = _make_sequencer()
        assert seq.state == SequencerState.IDLE

    def test_no_current_zone(self) -> None:
        seq = _make_sequencer()
        assert seq.current_zone is None
        assert seq.current_zone_index == -1

    def test_remaining_seconds_none_when_idle(self) -> None:
        seq = _make_sequencer()
        assert seq.remaining_zone_seconds is None

    def test_total_zones(self) -> None:
        zones = _make_zones(4)
        seq = _make_sequencer(zones=zones)
        assert seq.total_zones == 4

    def test_next_zone_none_when_idle(self) -> None:
        seq = _make_sequencer()
        assert seq.next_zone is None

    def test_progress_snapshot_when_idle(self) -> None:
        seq = _make_sequencer()
        p = seq.progress
        assert p["state"] == "idle"
        assert p["current_zone"] is None
        assert p["total_zones"] == 3
        assert p["remaining_zone_seconds"] is None


class TestStart:
    """Tests for Sequencer.start()."""

    @pytest.mark.asyncio
    async def test_sets_state_to_running(self) -> None:
        hass = make_mock_hass({"switch.valve_1": FakeState("on")})
        # async_create_task muss den Task zurückgeben
        hass.async_create_task = MagicMock(side_effect=lambda coro, *a, **kw: asyncio.ensure_future(coro))
        zones = _make_zones(1, duration=0)  # 0 min = sofort fertig

        seq = _make_sequencer(hass=hass, zones=zones)

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await seq.start()
            # Kurz warten bis der Task startet
            await asyncio.sleep(0)

        assert seq.state in (SequencerState.RUNNING, SequencerState.IDLE)

    @pytest.mark.asyncio
    async def test_noop_when_already_running(self) -> None:
        hass = make_mock_hass({"switch.valve_1": FakeState("on")})
        hass.async_create_task = MagicMock(side_effect=lambda coro, *a, **kw: asyncio.ensure_future(coro))
        zones = _make_zones(1, duration=100)

        seq = _make_sequencer(hass=hass, zones=zones)
        seq._state = SequencerState.RUNNING

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await seq.start()

        # Should not create a second task
        hass.async_create_task.assert_not_called()

    @pytest.mark.asyncio
    async def test_noop_when_no_zones(self) -> None:
        hass = make_mock_hass({})
        seq = _make_sequencer(hass=hass, zones=[])

        await seq.start()

        assert seq.state == SequencerState.IDLE


class TestStop:
    """Tests for Sequencer.stop()."""

    @pytest.mark.asyncio
    async def test_noop_when_idle(self) -> None:
        seq = _make_sequencer()
        await seq.stop()
        assert seq.state == SequencerState.IDLE

    @pytest.mark.asyncio
    async def test_closes_current_zone(self) -> None:
        """Stopping while a zone is running should close it."""
        hass = make_mock_hass(
            {"switch.valve_1": FakeState("on"), "switch.valve_2": FakeState("off")}
        )
        hass.async_create_task = MagicMock(
            side_effect=lambda coro, *a, **kw: asyncio.ensure_future(coro)
        )
        zones = _make_zones(2, duration=60)  # Lange Dauer, wird gecancelt
        safety = SafetyManager(hass, max_runtime_minutes=60)

        seq = _make_sequencer(hass=hass, zones=zones, safety=safety, pause_seconds=0)

        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            # sleep blockiert beim ersten Aufruf (Zone-Duration), damit wir canceln können
            sleep_event = asyncio.Event()

            async def _blocking_sleep(seconds):
                if seconds > 5:  # Zone duration sleep (nicht verify delay)
                    await sleep_event.wait()

            mock_sleep.side_effect = _blocking_sleep

            await seq.start()
            await asyncio.sleep(0)  # Task starten lassen
            await asyncio.sleep(0)  # turn_on abwarten

            # Jetzt ist Zone 1 offen
            assert seq.state == SequencerState.RUNNING

            # stop() muss Zone schließen
            # Valve state auf "off" setzen für verify
            hass.states.get = MagicMock(
                side_effect=lambda eid: FakeState("off")
            )
            await seq.stop()

        assert seq.state == SequencerState.IDLE
        assert seq.current_zone is None

    @pytest.mark.asyncio
    async def test_cancels_deadman_timer(self) -> None:
        hass = make_mock_hass({"switch.valve_1": FakeState("on")})
        hass.async_create_task = MagicMock(
            side_effect=lambda coro, *a, **kw: asyncio.ensure_future(coro)
        )
        zones = _make_zones(1, duration=60)
        safety = SafetyManager(hass, max_runtime_minutes=60)

        seq = _make_sequencer(hass=hass, zones=zones, safety=safety)

        # asyncio.sleep wird gepatcht, daher echtes sleep vorher sichern
        _real_sleep = asyncio.sleep
        sleep_event = asyncio.Event()

        async def _blocking_sleep(seconds):
            if seconds > 5:
                await sleep_event.wait()
            else:
                await _real_sleep(0)

        with patch("asyncio.sleep", new_callable=AsyncMock, side_effect=_blocking_sleep):
            await seq.start()
            await _real_sleep(0.01)  # Task laufen lassen

            # Deadman should be started
            assert hass.loop.call_later.called

            hass.states.get = MagicMock(side_effect=lambda eid: FakeState("off"))
            await seq.stop()

        # Timer handle should have been cancelled (via cancel_deadman)
        assert "switch.valve_1" not in safety._timers


class TestRun:
    """Tests for the internal _run() loop."""

    @pytest.mark.asyncio
    async def test_runs_all_zones_in_order(self) -> None:
        """All zones should be opened and closed in sequence."""
        # Alle Ventile "bestätigen" das Öffnen/Schließen
        state_map = {f"switch.valve_{i + 1}": FakeState("off") for i in range(3)}
        hass = make_mock_hass(state_map)
        hass.async_create_task = MagicMock(
            side_effect=lambda coro, *a, **kw: asyncio.ensure_future(coro)
        )

        zones = _make_zones(3, duration=1)
        on_complete = MagicMock()

        # Track welche Ventile geöffnet/geschlossen werden
        opened: list[str] = []
        closed: list[str] = []

        original_call = hass.services.async_call

        async def _track_call(domain, service, data, **kwargs):
            eid = data["entity_id"]
            if service == "turn_on":
                opened.append(eid)
                # Ventil "öffnet" sich
                state_map[eid] = FakeState("on")
            elif service == "turn_off":
                closed.append(eid)
                state_map[eid] = FakeState("off")

        hass.services.async_call = AsyncMock(side_effect=_track_call)
        hass.states.get = MagicMock(side_effect=lambda eid: state_map.get(eid))

        seq = _make_sequencer(hass=hass, zones=zones, pause_seconds=0, on_complete=on_complete)

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await seq.start()
            # Warten bis der Task abgeschlossen ist
            if seq._task:
                await seq._task

        assert opened == [
            "switch.valve_1",
            "switch.valve_2",
            "switch.valve_3",
        ]
        assert closed == [
            "switch.valve_1",
            "switch.valve_2",
            "switch.valve_3",
        ]
        assert seq.state == SequencerState.IDLE
        on_complete.assert_called_once()

    @pytest.mark.asyncio
    async def test_skips_zone_that_fails_to_open(self) -> None:
        """If a zone fails to turn on, skip it and continue."""
        state_map = {
            "switch.valve_1": FakeState("off"),  # Bleibt off = fail
            "switch.valve_2": FakeState("off"),
        }
        hass = make_mock_hass(state_map)
        hass.async_create_task = MagicMock(
            side_effect=lambda coro, *a, **kw: asyncio.ensure_future(coro)
        )

        opened_successfully: list[str] = []

        async def _track_call(domain, service, data, **kwargs):
            eid = data["entity_id"]
            if service == "turn_on":
                # Valve 1 bleibt off (Mismatch), Valve 2 öffnet
                if eid == "switch.valve_2":
                    state_map[eid] = FakeState("on")
                    opened_successfully.append(eid)
            elif service == "turn_off":
                state_map[eid] = FakeState("off")

        hass.services.async_call = AsyncMock(side_effect=_track_call)
        hass.states.get = MagicMock(side_effect=lambda eid: state_map.get(eid))

        zones = _make_zones(2, duration=1)
        seq = _make_sequencer(hass=hass, zones=zones, pause_seconds=0)

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await seq.start()
            if seq._task:
                await seq._task

        # Zone 1 wurde übersprungen, Zone 2 lief normal
        assert opened_successfully == ["switch.valve_2"]
        assert seq.state == SequencerState.IDLE

    @pytest.mark.asyncio
    async def test_pause_between_zones(self) -> None:
        """Pause should be observed between zones."""
        state_map = {f"switch.valve_{i + 1}": FakeState("off") for i in range(2)}
        hass = make_mock_hass(state_map)
        hass.async_create_task = MagicMock(
            side_effect=lambda coro, *a, **kw: asyncio.ensure_future(coro)
        )

        async def _track_call(domain, service, data, **kwargs):
            eid = data["entity_id"]
            if service == "turn_on":
                state_map[eid] = FakeState("on")
            elif service == "turn_off":
                state_map[eid] = FakeState("off")

        hass.services.async_call = AsyncMock(side_effect=_track_call)
        hass.states.get = MagicMock(side_effect=lambda eid: state_map.get(eid))

        zones = _make_zones(2, duration=1)
        seq = _make_sequencer(hass=hass, zones=zones, pause_seconds=30)

        sleep_calls: list[float] = []

        async def _track_sleep(seconds):
            sleep_calls.append(seconds)

        with patch("asyncio.sleep", new_callable=AsyncMock, side_effect=_track_sleep):
            await seq.start()
            if seq._task:
                await seq._task

        # Es sollte eine 30s Pause zwischen den Zonen geben
        assert 30 in sleep_calls


class TestProgress:
    """Tests for progress reporting properties."""

    def test_remaining_seconds_while_running(self) -> None:
        zones = _make_zones(1, duration=15)
        seq = _make_sequencer(zones=zones)

        seq._state = SequencerState.RUNNING
        seq._current_index = 0
        seq._current_zone = zones[0]
        seq._zone_started_at = datetime.now(timezone.utc) - timedelta(minutes=5)

        remaining = seq.remaining_zone_seconds
        assert remaining is not None
        # 15 min - 5 min = ~600s
        assert 590 < remaining < 610

    def test_remaining_seconds_zero_when_expired(self) -> None:
        zones = _make_zones(1, duration=15)
        seq = _make_sequencer(zones=zones)

        seq._state = SequencerState.RUNNING
        seq._current_index = 0
        seq._current_zone = zones[0]
        seq._zone_started_at = datetime.now(timezone.utc) - timedelta(minutes=20)

        assert seq.remaining_zone_seconds == 0

    def test_next_zone_during_run(self) -> None:
        zones = _make_zones(3)
        seq = _make_sequencer(zones=zones)

        seq._current_index = 0
        assert seq.next_zone is zones[1]

        seq._current_index = 1
        assert seq.next_zone is zones[2]

        seq._current_index = 2
        assert seq.next_zone is None

    def test_progress_snapshot_while_running(self) -> None:
        zones = _make_zones(3)
        seq = _make_sequencer(zones=zones)

        seq._state = SequencerState.RUNNING
        seq._current_index = 1
        seq._current_zone = zones[1]
        seq._zone_started_at = datetime.now(timezone.utc)

        p = seq.progress
        assert p["state"] == "running"
        assert p["current_zone"] == "Zone 2"
        assert p["current_zone_index"] == 1
        assert p["total_zones"] == 3
        assert p["next_zone"] == "Zone 3"
        assert p["remaining_zone_seconds"] is not None


class TestSafetyIntegration:
    """Tests that the sequencer integrates correctly with SafetyManager."""

    @pytest.mark.asyncio
    async def test_deadman_started_for_each_zone(self) -> None:
        """Each zone should get a deadman timer when opened."""
        state_map = {f"switch.valve_{i + 1}": FakeState("off") for i in range(2)}
        hass = make_mock_hass(state_map)
        hass.async_create_task = MagicMock(
            side_effect=lambda coro, *a, **kw: asyncio.ensure_future(coro)
        )

        async def _track_call(domain, service, data, **kwargs):
            eid = data["entity_id"]
            if service == "turn_on":
                state_map[eid] = FakeState("on")
            elif service == "turn_off":
                state_map[eid] = FakeState("off")

        hass.services.async_call = AsyncMock(side_effect=_track_call)
        hass.states.get = MagicMock(side_effect=lambda eid: state_map.get(eid))

        zones = _make_zones(2, duration=1)
        safety = SafetyManager(hass, max_runtime_minutes=60)
        seq = _make_sequencer(hass=hass, zones=zones, safety=safety, pause_seconds=0)

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await seq.start()
            if seq._task:
                await seq._task

        # call_later sollte für jede Zone aufgerufen worden sein
        # (2 Zonen = 2 start_deadman Aufrufe)
        assert hass.loop.call_later.call_count == 2


class TestPauseCountdown:
    """Regression tests for the inter-zone pause countdown bug.

    Bug: total_remaining_seconds used to drop the currently running pause from
    its sum because _zone_started_at was cleared before asyncio.sleep() and
    remaining_zone_seconds fell through to 0. The fix tracks _pause_started_at
    explicitly so the property can account for the pause time left.
    """

    def test_total_remaining_during_pause(self) -> None:
        """During a pause, total_remaining = pause_left + pending zones + gaps."""
        zones = _make_zones(3, duration=1)  # 3 × 60s
        seq = _make_sequencer(zones=zones, pause_seconds=30)

        # Zone 1 just finished, 10s into the 30s inter-zone pause.
        seq._state = SequencerState.RUNNING
        seq._current_index = 0
        seq._current_zone = None
        seq._zone_started_at = None
        seq._pause_started_at = datetime.now(timezone.utc) - timedelta(seconds=10)
        seq._pause_duration_seconds = 30

        total = seq.total_remaining_seconds
        assert total is not None
        # Expected: pause_left(~20) + Z2(60) + Z3(60) + 1 future gap(30) = ~170s
        assert 168 <= total <= 172

    def test_pause_remaining_monotonically_decreases(self) -> None:
        """pause_remaining_seconds must tick down and clamp at 0."""
        seq = _make_sequencer(pause_seconds=30)
        seq._state = SequencerState.RUNNING
        seq._current_index = 0
        seq._pause_duration_seconds = 30

        now = datetime.now(timezone.utc)

        seq._pause_started_at = now - timedelta(seconds=5)
        assert 24 <= (seq.pause_remaining_seconds or -1) <= 26

        seq._pause_started_at = now - timedelta(seconds=15)
        assert 14 <= (seq.pause_remaining_seconds or -1) <= 16

        seq._pause_started_at = now - timedelta(seconds=25)
        assert 4 <= (seq.pause_remaining_seconds or -1) <= 6

        seq._pause_started_at = now - timedelta(seconds=40)
        assert seq.pause_remaining_seconds == 0

    def test_pause_remaining_none_when_not_pausing(self) -> None:
        seq = _make_sequencer()
        assert seq.pause_remaining_seconds is None

    def test_total_remaining_during_last_pause_has_no_future_gaps(self) -> None:
        """Pause before the last zone: only one pending zone, zero future gaps."""
        zones = _make_zones(3, duration=1)
        seq = _make_sequencer(zones=zones, pause_seconds=30)

        seq._state = SequencerState.RUNNING
        seq._current_index = 1  # Z2 just finished, now pausing before Z3
        seq._pause_started_at = datetime.now(timezone.utc) - timedelta(seconds=5)
        seq._pause_duration_seconds = 30

        total = seq.total_remaining_seconds
        assert total is not None
        # Expected: pause_left(~25) + Z3(60) + future_gaps(0) = ~85s
        assert 83 <= total <= 87

    def test_total_remaining_during_zone_unchanged(self) -> None:
        """Regression: running-zone path must still compute correctly."""
        zones = _make_zones(3, duration=1)  # 3 × 60s
        seq = _make_sequencer(zones=zones, pause_seconds=30)

        seq._state = SequencerState.RUNNING
        seq._current_index = 0
        seq._current_zone = zones[0]
        seq._zone_started_at = datetime.now(timezone.utc) - timedelta(seconds=2)
        # Explicitly NOT pausing
        seq._pause_started_at = None

        total = seq.total_remaining_seconds
        assert total is not None
        # Z1 remaining(~58) + Z2(60) + Z3(60) + 2 gaps(60) = ~238s
        assert 236 <= total <= 240

    def test_progress_exposes_phase_and_pause_fields(self) -> None:
        seq = _make_sequencer(pause_seconds=30)
        seq._state = SequencerState.RUNNING
        seq._current_index = 0
        seq._pause_started_at = datetime.now(timezone.utc) - timedelta(seconds=5)
        seq._pause_duration_seconds = 30

        p = seq.progress
        assert p["phase"] == "pausing"
        assert p["state"] == "running"  # backwards compatible
        assert isinstance(p["pause_remaining_seconds"], int)
        assert 0 < p["pause_remaining_seconds"] <= 30

    def test_progress_phase_running_when_zone_active(self) -> None:
        zones = _make_zones(1, duration=1)
        seq = _make_sequencer(zones=zones)
        seq._state = SequencerState.RUNNING
        seq._current_index = 0
        seq._current_zone = zones[0]
        seq._zone_started_at = datetime.now(timezone.utc)

        p = seq.progress
        assert p["phase"] == "running"
        assert p["pause_remaining_seconds"] is None

    def test_progress_phase_idle_when_idle(self) -> None:
        seq = _make_sequencer()
        p = seq.progress
        assert p["phase"] == "idle"
        assert p["pause_remaining_seconds"] is None

    def test_pause_flags_cleared_on_reset(self) -> None:
        seq = _make_sequencer()
        seq._pause_started_at = datetime.now(timezone.utc)
        seq._pause_duration_seconds = 30

        seq._reset()

        assert seq._pause_started_at is None
        assert seq._pause_duration_seconds == 0
        assert seq.pause_remaining_seconds is None

    def test_total_remaining_includes_depressurize_when_master_configured(
        self,
    ) -> None:
        """Idle total must include the per-zone depressurize wait."""
        hass = make_mock_hass(
            {f"switch.valve_{i + 1}": FakeState("off") for i in range(3)}
        )
        zones = _make_zones(3, duration=1)  # 3 × 60s
        safety = SafetyManager(hass, max_runtime_minutes=60)
        seq = Sequencer(
            hass=hass,
            zones=zones,
            safety=safety,
            pause_seconds=30,
            master_valve_entity_id="switch.master",
            depressurize_seconds=5,
        )

        # 3 zones × 60s + 3 × depressurize 5s + 2 gaps × 30s = 180 + 15 + 60 = 255
        assert seq.total_program_seconds_idle == 255

    def test_total_remaining_skips_depressurize_without_master(self) -> None:
        """No master valve → depressurize is not effective and not counted."""
        zones = _make_zones(3, duration=1)
        # default _make_sequencer has no master valve
        seq = _make_sequencer(zones=zones, pause_seconds=30)

        # 3 × 60 + 0 + 2 × 30 = 240
        assert seq.total_program_seconds_idle == 240

    def test_total_remaining_during_depressurize(self) -> None:
        """Mid-depressurize: depress_left + trailing pause + future blocks."""
        hass = make_mock_hass(
            {f"switch.valve_{i + 1}": FakeState("off") for i in range(3)}
        )
        zones = _make_zones(3, duration=1)  # 3 × 60s
        safety = SafetyManager(hass, max_runtime_minutes=60)
        seq = Sequencer(
            hass=hass,
            zones=zones,
            safety=safety,
            pause_seconds=30,
            master_valve_entity_id="switch.master",
            depressurize_seconds=10,
        )

        # Zone 1 done, 4s into 10s depressurize.
        seq._state = SequencerState.RUNNING
        seq._current_index = 0
        seq._depressurize_started_at = (
            datetime.now(timezone.utc) - timedelta(seconds=4)
        )
        seq._depressurize_duration_seconds = 10

        total = seq.total_remaining_seconds
        assert total is not None
        # depress_left(~6) + trailing pause(30) + block(Z2)=60+10+30=100 +
        # block(Z3)=60+10+0=70 → ~206
        assert 204 <= total <= 208

    def test_total_remaining_during_zone_includes_trailing_depressurize(
        self,
    ) -> None:
        """Running zone must add its own depressurize + pause to the total."""
        hass = make_mock_hass(
            {f"switch.valve_{i + 1}": FakeState("off") for i in range(2)}
        )
        zones = _make_zones(2, duration=1)  # 2 × 60s
        safety = SafetyManager(hass, max_runtime_minutes=60)
        seq = Sequencer(
            hass=hass,
            zones=zones,
            safety=safety,
            pause_seconds=20,
            master_valve_entity_id="switch.master",
            depressurize_seconds=5,
        )

        # 2s into Zone 1
        seq._state = SequencerState.RUNNING
        seq._current_index = 0
        seq._current_zone = zones[0]
        seq._zone_started_at = datetime.now(timezone.utc) - timedelta(seconds=2)

        total = seq.total_remaining_seconds
        assert total is not None
        # zone_left(~58) + depress(5) + pause(20) + block(Z2)=60+5+0=65 → ~148
        assert 146 <= total <= 150

    def test_total_remaining_pause_after_depressurize_consistent(self) -> None:
        """Transition depressurize → pause must not produce a value jump.

        The whole point of the bug fix: switching phase shouldn't make the
        countdown leap. Snapshot in late-depress then in early-pause for the
        same elapsed program time and check they are within ~1s.
        """
        hass = make_mock_hass(
            {f"switch.valve_{i + 1}": FakeState("off") for i in range(2)}
        )
        zones = _make_zones(2, duration=1)
        safety = SafetyManager(hass, max_runtime_minutes=60)
        seq = Sequencer(
            hass=hass,
            zones=zones,
            safety=safety,
            pause_seconds=30,
            master_valve_entity_id="switch.master",
            depressurize_seconds=10,
        )
        now = datetime.now(timezone.utc)

        # End of depressurize (~0s left)
        seq._state = SequencerState.RUNNING
        seq._current_index = 0
        seq._depressurize_started_at = now - timedelta(seconds=10)
        seq._depressurize_duration_seconds = 10
        end_of_depress = seq.total_remaining_seconds

        # Start of pause (~30s left)
        seq._depressurize_started_at = None
        seq._depressurize_duration_seconds = 0
        seq._pause_started_at = now
        seq._pause_duration_seconds = 30
        start_of_pause = seq.total_remaining_seconds

        assert end_of_depress is not None
        assert start_of_pause is not None
        assert abs(end_of_depress - start_of_pause) <= 1

    def test_depressurize_remaining_none_when_not_active(self) -> None:
        seq = _make_sequencer()
        assert seq.depressurize_remaining_seconds is None

    def test_progress_phase_depressurizing(self) -> None:
        seq = _make_sequencer()
        seq._state = SequencerState.RUNNING
        seq._current_index = 0
        seq._depressurize_started_at = datetime.now(timezone.utc)
        seq._depressurize_duration_seconds = 5

        p = seq.progress
        assert p["phase"] == "depressurizing"
        assert isinstance(p["depressurize_remaining_seconds"], int)

    def test_depressurize_flags_cleared_on_reset(self) -> None:
        seq = _make_sequencer()
        seq._depressurize_started_at = datetime.now(timezone.utc)
        seq._depressurize_duration_seconds = 5
        seq._reset()
        assert seq._depressurize_started_at is None
        assert seq._depressurize_duration_seconds == 0
        assert seq.depressurize_remaining_seconds is None

    @pytest.mark.asyncio
    async def test_pause_flags_cleared_on_cancel(self) -> None:
        """stop() during the inter-zone pause must clear pause tracking."""
        state_map = {f"switch.valve_{i + 1}": FakeState("off") for i in range(2)}
        hass = make_mock_hass(state_map)
        hass.async_create_task = MagicMock(
            side_effect=lambda coro, *a, **kw: asyncio.ensure_future(coro)
        )

        async def _track_call(domain, service, data, **kwargs):
            eid = data["entity_id"]
            if service == "turn_on":
                state_map[eid] = FakeState("on")
            elif service == "turn_off":
                state_map[eid] = FakeState("off")

        hass.services.async_call = AsyncMock(side_effect=_track_call)
        hass.states.get = MagicMock(side_effect=lambda eid: state_map.get(eid))

        zones = _make_zones(2, duration=1)
        seq = _make_sequencer(hass=hass, zones=zones, pause_seconds=30)

        # Let the sequencer progress into the pause by making sleep block
        # forever on the 30s pause call (the earlier 1s zone sleep returns).
        pause_entered = asyncio.Event()
        block_forever: asyncio.Future[None] = asyncio.get_event_loop().create_future()

        async def _fake_sleep(seconds):
            if seconds == 30:
                pause_entered.set()
                await block_forever  # block until cancelled by stop()
            return None

        with patch(
            "custom_components.irrigation_proxy.sequencer.asyncio.sleep",
            new=_fake_sleep,
        ):
            await seq.start()
            await pause_entered.wait()
            # Mid-pause: flags must be set
            assert seq._pause_started_at is not None
            assert seq._pause_duration_seconds == 30

            await seq.stop()

        assert seq._pause_started_at is None
        assert seq._pause_duration_seconds == 0
        assert seq.pause_remaining_seconds is None
        assert seq.state == SequencerState.IDLE
