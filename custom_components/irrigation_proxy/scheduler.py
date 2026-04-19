"""Program scheduler – triggers sequencer.start() at configured times.

The scheduler is intentionally narrow in v0.5.0: no rain handling, no
duration scaling. It just watches weekday × HH:MM triggers and kicks
the sequencer when one of them fires and the weekday mask matches.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from typing import TYPE_CHECKING

from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_track_time_change

from .const import WEEKDAYS

if TYPE_CHECKING:
    from .sequencer import Sequencer

_LOGGER = logging.getLogger(__name__)


@dataclass
class ScheduleConfig:
    """User-facing schedule description."""

    enabled: bool = False
    start_times: list[time] = field(default_factory=list)
    weekdays: set[str] = field(default_factory=set)

    def matches_today(self, now: datetime) -> bool:
        """Return True if `now`'s weekday is in the schedule's mask."""
        if not self.enabled or not self.weekdays:
            return False
        idx = now.weekday()  # Mon=0 … Sun=6
        if idx < 0 or idx >= len(WEEKDAYS):
            return False
        return WEEKDAYS[idx] in self.weekdays


def parse_start_times(raw: str | list[str] | None) -> list[time]:
    """Parse HH:MM values from a comma-separated string or list.

    Invalid entries are skipped with a warning. Returned list is sorted
    and de-duplicated.
    """
    if raw is None:
        return []
    if isinstance(raw, str):
        parts = [p.strip() for p in raw.split(",") if p.strip()]
    else:
        parts = [str(p).strip() for p in raw if str(p).strip()]

    out: set[time] = set()
    for p in parts:
        try:
            hh, mm = p.split(":", 1)
            t = time(int(hh), int(mm))
            out.add(t)
        except (ValueError, TypeError):
            _LOGGER.warning("Scheduler: ignoring invalid start time %r", p)
    return sorted(out)


def format_start_times(values: list[time] | list[str] | None) -> str:
    """Render schedule_start_times back to a comma-separated string."""
    if not values:
        return ""
    parts: list[str] = []
    for v in values:
        if isinstance(v, time):
            parts.append(v.strftime("%H:%M"))
        else:
            parts.append(str(v))
    return ", ".join(parts)


def next_fire_time(
    now: datetime,
    config: ScheduleConfig,
) -> datetime | None:
    """Return the next scheduled fire time after `now`, or None if none."""
    if not config.enabled or not config.start_times or not config.weekdays:
        return None

    # Look ahead up to 14 days to handle sparse weekday masks.
    for day_offset in range(0, 15):
        candidate_date = (now + timedelta(days=day_offset)).date()
        weekday_key = WEEKDAYS[candidate_date.weekday()]
        if weekday_key not in config.weekdays:
            continue
        for t in config.start_times:
            # Combine as naive then convert via .astimezone() so the local
            # DST offset for that specific date is applied correctly.
            # datetime.combine(..., tzinfo=now.tzinfo) would copy the *current*
            # UTC offset and produce the wrong wall-clock time on the far side
            # of a DST boundary.
            candidate = datetime.combine(candidate_date, t).astimezone()
            if candidate > now:
                return candidate
    return None


class ProgramScheduler:
    """Registers time-triggered callbacks and invokes the sequencer."""

    def __init__(
        self,
        hass: HomeAssistant,
        sequencer: "Sequencer",
        get_config: Callable[[], ScheduleConfig],
        on_fire: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self._hass = hass
        self._sequencer = sequencer
        self._get_config = get_config
        self._on_fire = on_fire
        self._unsubs: list[Callable[[], None]] = []
        self._last_fire: datetime | None = None

    # -- Lifecycle -------------------------------------------------------

    def reload(self) -> None:
        """Re-register time triggers based on the current config."""
        self.unregister()
        config = self._get_config()
        if not config.enabled or not config.start_times:
            _LOGGER.debug("Scheduler: disabled or no start times configured")
            return

        for t in config.start_times:
            unsub = async_track_time_change(
                self._hass,
                self._handle_fire,
                hour=t.hour,
                minute=t.minute,
                second=0,
            )
            self._unsubs.append(unsub)

        _LOGGER.info(
            "Scheduler: registered %d start time(s) on %s",
            len(config.start_times),
            ",".join(sorted(config.weekdays)) or "<no weekdays>",
        )

    def unregister(self) -> None:
        """Cancel all registered time triggers."""
        for unsub in self._unsubs:
            try:
                unsub()
            except Exception:  # noqa: BLE001 – defensive
                _LOGGER.exception("Scheduler: failed to cancel time trigger")
        self._unsubs.clear()

    # -- Status ----------------------------------------------------------

    @property
    def next_fire_time(self) -> datetime | None:
        """Timestamp of the next scheduled run (local tz), or None."""
        config = self._get_config()
        now = datetime.now().astimezone()
        return next_fire_time(now, config)

    @property
    def last_fire(self) -> datetime | None:
        return self._last_fire

    # -- Trigger handling ------------------------------------------------

    async def _handle_fire(self, now: datetime) -> None:
        """Called by HA when a configured start time ticks over."""
        config = self._get_config()

        if not config.enabled:
            return

        local_now = now.astimezone()
        if not config.matches_today(local_now):
            _LOGGER.debug(
                "Scheduler: trigger at %s ignored – weekday not in schedule",
                local_now,
            )
            return

        self._last_fire = local_now

        _LOGGER.info("Scheduler: starting program via schedule")
        await self._sequencer.start()

        if self._on_fire is not None:
            try:
                await self._on_fire()
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Scheduler: on_fire callback raised")
