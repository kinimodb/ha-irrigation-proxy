"""Weather data from Open-Meteo API for smart irrigation adjustment.

Fetches ET₀ (reference evapotranspiration) and precipitation data,
then calculates a water_need_factor that the coordinator exposes
to sensors and automations.

Rate-limited to max 1 API call per 30 minutes (CLAUDE.md rule #4).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    DEFAULT_RAIN_THRESHOLD_MM,
    DEFAULT_REFERENCE_ET0_MM,
    OPEN_METEO_BASE_URL,
    WEATHER_UPDATE_INTERVAL_MINUTES,
)

_LOGGER = logging.getLogger(__name__)


@dataclass
class WeatherData:
    """Immutable snapshot of weather data for irrigation decisions."""

    et0_today: float = 0.0  # mm/day – reference evapotranspiration
    precipitation_last_24h: float = 0.0  # mm
    precipitation_forecast_24h: float = 0.0  # mm
    temperature_max: float = 0.0  # °C
    water_need_factor: float = 1.0  # 0.0 – 2.0 multiplier
    rain_skip: bool = False  # True → skip irrigation
    last_update: datetime | None = None
    last_error: str | None = None


class WeatherProvider:
    """Fetches weather data from Open-Meteo and calculates irrigation adjustments.

    Design:
    - Rate-limited: skips fetch if last successful call < 30 min ago
    - Fail-safe: on API error, keeps last known data and logs warning
    - Pure calculation methods are separated for easy unit testing
    """

    def __init__(
        self,
        hass: HomeAssistant,
        latitude: float,
        longitude: float,
        rain_threshold_mm: float = DEFAULT_RAIN_THRESHOLD_MM,
        reference_et0_mm: float = DEFAULT_REFERENCE_ET0_MM,
    ) -> None:
        self._hass = hass
        self._latitude = latitude
        self._longitude = longitude
        self._rain_threshold_mm = rain_threshold_mm
        self._reference_et0_mm = reference_et0_mm
        self._data = WeatherData()
        self._last_fetch: datetime | None = None

    @property
    def data(self) -> WeatherData:
        """Current weather data snapshot."""
        return self._data

    async def async_update(self) -> WeatherData:
        """Fetch weather data if rate limit allows, return current data."""
        now = datetime.now(timezone.utc)

        if self._last_fetch is not None:
            elapsed = (now - self._last_fetch).total_seconds()
            if elapsed < WEATHER_UPDATE_INTERVAL_MINUTES * 60:
                _LOGGER.debug(
                    "Weather: skipping fetch (%.0fs since last, need %ds)",
                    elapsed,
                    WEATHER_UPDATE_INTERVAL_MINUTES * 60,
                )
                return self._data

        try:
            raw = await self._fetch_api()
            self._parse_response(raw)
            self._calculate_adjustments()
            self._data.last_update = now
            self._data.last_error = None
            self._last_fetch = now
            _LOGGER.info(
                "Weather: updated – ET₀=%.1fmm, rain_24h=%.1fmm, "
                "forecast=%.1fmm, factor=%.2f, skip=%s",
                self._data.et0_today,
                self._data.precipitation_last_24h,
                self._data.precipitation_forecast_24h,
                self._data.water_need_factor,
                self._data.rain_skip,
            )
        except Exception as err:
            self._data.last_error = str(err)
            _LOGGER.warning("Weather: fetch failed – %s", err)

        return self._data

    async def _fetch_api(self) -> dict[str, Any]:
        """Call Open-Meteo API with 10s timeout."""
        session = async_get_clientsession(self._hass)

        params = {
            "latitude": self._latitude,
            "longitude": self._longitude,
            "daily": ",".join([
                "et0_fao_evapotranspiration",
                "precipitation_sum",
                "temperature_2m_max",
            ]),
            "past_days": 1,
            "forecast_days": 2,
            "timezone": "auto",
        }

        async with asyncio.timeout(10):
            resp = await session.get(OPEN_METEO_BASE_URL, params=params)

        if resp.status != 200:
            text = await resp.text()
            raise RuntimeError(
                f"Open-Meteo returned HTTP {resp.status}: {text[:200]}"
            )

        return await resp.json()

    def _parse_response(self, data: dict[str, Any]) -> None:
        """Extract relevant values from API response.

        With past_days=1 and forecast_days=2 the daily arrays are:
          [0] = yesterday, [1] = today, [2] = tomorrow
        """
        daily = data.get("daily", {})

        et0_values = daily.get("et0_fao_evapotranspiration", [])
        precip_values = daily.get("precipitation_sum", [])
        temp_values = daily.get("temperature_2m_max", [])

        # ET₀ today
        if len(et0_values) >= 2:
            self._data.et0_today = et0_values[1] or 0.0

        # Niederschlag gestern (Proxy für letzte 24h)
        if len(precip_values) >= 1:
            self._data.precipitation_last_24h = precip_values[0] or 0.0

        # Niederschlag-Prognose (heute + morgen)
        forecast_rain = 0.0
        if len(precip_values) >= 2:
            forecast_rain += precip_values[1] or 0.0
        if len(precip_values) >= 3:
            forecast_rain += precip_values[2] or 0.0
        self._data.precipitation_forecast_24h = forecast_rain

        # Temperatur-Maximum heute
        if len(temp_values) >= 2:
            self._data.temperature_max = temp_values[1] or 0.0

    def _calculate_adjustments(self) -> None:
        """Calculate water_need_factor and rain_skip from parsed data.

        Logic:
        1. If recent + forecast rain >= threshold → rain_skip, factor = 0
        2. Base factor = ET₀_today / reference_ET₀
        3. Reduce by recent precipitation (each mm rain offsets ET₀)
        4. Clamp to [0.0, 2.0]
        """
        total_rain = (
            self._data.precipitation_last_24h
            + self._data.precipitation_forecast_24h
        )
        self._data.rain_skip = total_rain >= self._rain_threshold_mm

        if self._data.rain_skip:
            self._data.water_need_factor = 0.0
            return

        # Base factor from ET₀
        if self._reference_et0_mm > 0:
            factor = self._data.et0_today / self._reference_et0_mm
        else:
            factor = 1.0

        # Reduce by recent precipitation
        if self._data.precipitation_last_24h > 0 and self._data.et0_today > 0:
            rain_offset = self._data.precipitation_last_24h / self._data.et0_today
            factor = max(0.0, factor - rain_offset)

        # Clamp
        self._data.water_need_factor = max(0.0, min(2.0, factor))
