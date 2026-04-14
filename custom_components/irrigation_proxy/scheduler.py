"""Program scheduler – triggers sequencer.start() at configured times.

Listens for scheduled (weekday × time) triggers. When one fires:

1. Checks the configured weekday mask.
2. Consults the WeatherProvider for the selected rain-adjust mode:
   - "off":   always start at 100 %.
   - "hard":  if combined rain ≥ rain_threshold → skip entirely.
   - "scale": compute planned_mm (Σ duration × ASSUMED_FLOW_MM_PER_MIN);
              if rain ≥ planned_mm → skip; otherwise scale each zone by
              (planned_mm − rain) / planned_mm.
3. Calls sequencer.start(duration_multiplier=...).

The scheduler is intentionally decoupled from Home Assistant's config
machinery – it receives the already-parsed schedule via register(). The
coordinator handles lifecycle (register at setup, unregister on unload /
options update).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from typing import TYPE_CHECKING

from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_track_time_change

from .const import (
    ASSUMED_FLOW_MM_PER_MIN,
    RAIN_ADJUST_HARD,
    RAIN_ADJUST_OFF,
    RAIN_ADJUST_SCALE,
    WEEKDAYS,
)

if TYPE_CHECKING:
    from .sequencer import Sequencer
    from .weather import WeatherProvider

_LOGGER = logging.getLogger(__name__)


@dataclass
class ScheduleConfig:
    """User-facing schedule description."""

    enabled: bool = False
    start_times: list[time] = field(default_factory=list)
    weekdays: set[str] = field(default_factory=set)
    rain_adjust_mode: str = RAIN_ADJUST_OFF

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
    """Render a schedule_start_times value back to a comma-separated string."""
    if not values:
        return ""
    parts: list[str] = []
    for v in values:
        if isinstance(v, time):
            parts.append(v.strftime("%H:%M"))
        else:
            parts.append(str(v))
    return ", ".join(parts)


def compute_duration_multiplier(
    mode: str,
    zones_total_minutes: float,
    rain_mm: float,
    rain_threshold_mm: float,
    *,
    flow_mm_per_min: float = ASSUMED_FLOW_MM_PER_MIN,
) -> float:
    """Return a 0..1 multiplier for the planned run based on rain + mode.

    0.0 means "skip entirely". 1.0 means "run at full duration".
    """
    if mode == RAIN_ADJUST_OFF:
        return 1.0

    if mode == RAIN_ADJUST_HARD:
        if rain_mm >= rain_threshold_mm:
            return 0.0
        return 1.0

    if mode == RAIN_ADJUST_SCALE:
        planned_mm = max(0.0, zones_total_minutes * flow_mm_per_min)
        if planned_mm <= 0:
            return 1.0
        if rain_mm >= planned_mm:
            return 0.0
        remaining = max(0.0, planned_mm - rain_mm)
        return min(1.0, remaining / planned_mm)

    _LOGGER.warning("Scheduler: unknown rain_adjust_mode %r – running at 100%%", mode)
    return 1.0


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
            candidate = datetime.combine(candidate_date, t, tzinfo=now.tzinfo)
            if candidate > now:
                return candidate
    return None


class ProgramScheduler:
    """Registers time-triggered callbacks and invokes the sequencer."""

    def __init__(
        self,
        hass: HomeAssistant,
        sequencer: "Sequencer",
        weather: "WeatherProvider | None",
        get_config: Callable[[], ScheduleConfig],
        on_fire: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self._hass = hass
        self._sequencer = sequencer
        self._weather = weather
        self._get_config = get_config
        self._on_fire = on_fire
        self._unsubs: list[Callable[[], None]] = []
        self._last_fire: datetime | None = None
        self._last_multiplier: float = 1.0
        self._last_skip_reason: str | None = None

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

    @property
    def last_skip_reason(self) -> str | None:
        return self._last_skip_reason

    @property
    def last_multiplier(self) -> float:
        return self._last_multiplier

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

        multiplier = self._evaluate_multiplier(config)
        self._last_multiplier = multiplier

        if multiplier <= 0:
            _LOGGER.info(
                "Scheduler: skipping scheduled run (reason=%s)",
                self._last_skip_reason or "unknown",
            )
            return

        self._last_skip_reason = None
        _LOGGER.info(
            "Scheduler: starting program via schedule (multiplier=%.2f)",
            multiplier,
        )
        await self._sequencer.start(duration_multiplier=multiplier)

        if self._on_fire is not None:
            try:
                await self._on_fire()
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Scheduler: on_fire callback raised")

    def _evaluate_multiplier(self, config: ScheduleConfig) -> float:
        """Return duration multiplier (0..1) based on weather + mode."""
        mode = config.rain_adjust_mode or RAIN_ADJUST_OFF

        if mode == RAIN_ADJUST_OFF:
            self._last_skip_reason = None
            return 1.0

        if self._weather is None:
            # Nothing to decide against – behave as if rain adjust were off.
            _LOGGER.debug(
                "Scheduler: rain_adjust_mode=%s but no weather provider – running at 100%%",
                mode,
            )
            return 1.0

        rain_mm = self._weather.total_rain_mm
        threshold = self._weather.rain_threshold_mm
        zones_total_minutes = float(
            sum(z.duration_minutes for z in self._sequencer.zones)
        )

        multiplier = compute_duration_multiplier(
            mode=mode,
            zones_total_minutes=zones_total_minutes,
            rain_mm=rain_mm,
            rain_threshold_mm=threshold,
        )

        if multiplier <= 0:
            if mode == RAIN_ADJUST_HARD:
                self._last_skip_reason = (
                    f"rain {rain_mm:.1f}mm ≥ threshold {threshold:.1f}mm"
                )
            else:
                planned_mm = zones_total_minutes * ASSUMED_FLOW_MM_PER_MIN
                self._last_skip_reason = (
                    f"rain {rain_mm:.1f}mm ≥ planned {planned_mm:.1f}mm"
                )
        return multiplier
