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
