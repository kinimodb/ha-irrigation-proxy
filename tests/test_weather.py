"""Tests for the WeatherProvider module.

Tests the parsing and calculation logic without HA (per CLAUDE.md).
The API fetch is mocked.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.irrigation_proxy.weather import WeatherData, WeatherProvider

from .conftest import make_mock_hass


def _make_provider(
    hass: MagicMock | None = None,
    latitude: float = 49.30,
    longitude: float = 8.56,
    rain_threshold_mm: float = 5.0,
    reference_et0_mm: float = 5.0,
) -> WeatherProvider:
    """Create a WeatherProvider with test defaults (Reilingen coordinates)."""
    if hass is None:
        hass = make_mock_hass()
    return WeatherProvider(
        hass=hass,
        latitude=latitude,
        longitude=longitude,
        rain_threshold_mm=rain_threshold_mm,
        reference_et0_mm=reference_et0_mm,
    )


def _sample_api_response(
    et0_yesterday: float = 3.0,
    et0_today: float = 5.0,
    et0_tomorrow: float | None = 4.5,
    precip_yesterday: float = 0.0,
    precip_today: float = 0.0,
    precip_tomorrow: float = 0.0,
    temp_yesterday: float = 18.0,
    temp_today: float = 25.0,
    temp_tomorrow: float = 22.0,
) -> dict:
    """Build a mock Open-Meteo API response."""
    return {
        "daily": {
            "time": ["2024-07-14", "2024-07-15", "2024-07-16"],
            "et0_fao_evapotranspiration": [et0_yesterday, et0_today, et0_tomorrow],
            "precipitation_sum": [precip_yesterday, precip_today, precip_tomorrow],
            "temperature_2m_max": [temp_yesterday, temp_today, temp_tomorrow],
        }
    }


class TestParseResponse:
    """Tests for WeatherProvider._parse_response()."""

    def test_parses_normal_response(self) -> None:
        provider = _make_provider()
        data = _sample_api_response(
            et0_today=4.2,
            precip_yesterday=1.5,
            precip_today=2.0,
            precip_tomorrow=3.0,
            temp_today=28.0,
        )

        provider._parse_response(data)

        assert provider._data.et0_today == 4.2
        assert provider._data.precipitation_last_24h == 1.5
        assert provider._data.precipitation_forecast_24h == 5.0  # 2.0 + 3.0
        assert provider._data.temperature_max == 28.0

    def test_handles_none_values(self) -> None:
        """API sometimes returns None for missing data points."""
        provider = _make_provider()
        data = {
            "daily": {
                "time": ["2024-07-14", "2024-07-15"],
                "et0_fao_evapotranspiration": [None, None],
                "precipitation_sum": [None, None],
                "temperature_2m_max": [None, None],
            }
        }

        provider._parse_response(data)

        assert provider._data.et0_today == 0.0
        assert provider._data.precipitation_last_24h == 0.0
        assert provider._data.precipitation_forecast_24h == 0.0
        assert provider._data.temperature_max == 0.0

    def test_handles_empty_daily(self) -> None:
        provider = _make_provider()
        provider._parse_response({"daily": {}})

        # Sollte Default-Werte behalten
        assert provider._data.et0_today == 0.0

    def test_handles_missing_daily(self) -> None:
        provider = _make_provider()
        provider._parse_response({})

        assert provider._data.et0_today == 0.0

    def test_handles_short_arrays(self) -> None:
        """If API returns fewer days than expected."""
        provider = _make_provider()
        data = {
            "daily": {
                "et0_fao_evapotranspiration": [3.0],  # Nur gestern
                "precipitation_sum": [1.0],
                "temperature_2m_max": [20.0],
            }
        }

        provider._parse_response(data)

        # et0_today braucht Index 1, nicht vorhanden → bleibt 0.0
        assert provider._data.et0_today == 0.0
        # precipitation_last_24h ist Index 0 → 1.0
        assert provider._data.precipitation_last_24h == 1.0


class TestCalculateAdjustments:
    """Tests for WeatherProvider._calculate_adjustments()."""

    def test_normal_day_factor_1(self) -> None:
        """ET₀ = reference → factor = 1.0."""
        provider = _make_provider(reference_et0_mm=5.0)
        provider._data.et0_today = 5.0
        provider._data.precipitation_last_24h = 0.0
        provider._data.precipitation_forecast_24h = 0.0

        provider._calculate_adjustments()

        assert provider._data.water_need_factor == 1.0
        assert provider._data.rain_skip is False

    def test_hot_day_factor_above_1(self) -> None:
        """ET₀ > reference → factor > 1.0."""
        provider = _make_provider(reference_et0_mm=5.0)
        provider._data.et0_today = 7.5
        provider._data.precipitation_last_24h = 0.0
        provider._data.precipitation_forecast_24h = 0.0

        provider._calculate_adjustments()

        assert provider._data.water_need_factor == 1.5
        assert provider._data.rain_skip is False

    def test_cool_day_factor_below_1(self) -> None:
        """ET₀ < reference → factor < 1.0."""
        provider = _make_provider(reference_et0_mm=5.0)
        provider._data.et0_today = 2.5
        provider._data.precipitation_last_24h = 0.0
        provider._data.precipitation_forecast_24h = 0.0

        provider._calculate_adjustments()

        assert provider._data.water_need_factor == 0.5

    def test_factor_clamped_at_2(self) -> None:
        """Factor should never exceed 2.0."""
        provider = _make_provider(reference_et0_mm=5.0)
        provider._data.et0_today = 15.0  # Extreme Hitze
        provider._data.precipitation_last_24h = 0.0
        provider._data.precipitation_forecast_24h = 0.0

        provider._calculate_adjustments()

        assert provider._data.water_need_factor == 2.0

    def test_rain_skip_triggered(self) -> None:
        """Total rain >= threshold → skip, factor = 0."""
        provider = _make_provider(rain_threshold_mm=5.0)
        provider._data.et0_today = 5.0
        provider._data.precipitation_last_24h = 3.0
        provider._data.precipitation_forecast_24h = 3.0  # Total = 6 >= 5

        provider._calculate_adjustments()

        assert provider._data.rain_skip is True
        assert provider._data.water_need_factor == 0.0

    def test_rain_skip_exact_threshold(self) -> None:
        """Total rain == threshold → skip (>= not >)."""
        provider = _make_provider(rain_threshold_mm=5.0)
        provider._data.et0_today = 5.0
        provider._data.precipitation_last_24h = 2.0
        provider._data.precipitation_forecast_24h = 3.0  # Total = 5.0

        provider._calculate_adjustments()

        assert provider._data.rain_skip is True

    def test_rain_skip_just_below_threshold(self) -> None:
        """Total rain < threshold → no skip."""
        provider = _make_provider(rain_threshold_mm=5.0)
        provider._data.et0_today = 5.0
        provider._data.precipitation_last_24h = 2.0
        provider._data.precipitation_forecast_24h = 2.5  # Total = 4.5

        provider._calculate_adjustments()

        assert provider._data.rain_skip is False

    def test_recent_rain_reduces_factor(self) -> None:
        """Recent rain should offset the ET₀-based factor."""
        provider = _make_provider(reference_et0_mm=5.0)
        provider._data.et0_today = 5.0
        provider._data.precipitation_last_24h = 2.5  # Halbiert den Bedarf
        provider._data.precipitation_forecast_24h = 0.0

        provider._calculate_adjustments()

        # factor = 5/5 - 2.5/5 = 1.0 - 0.5 = 0.5
        assert provider._data.water_need_factor == 0.5

    def test_heavy_recent_rain_floors_factor_at_zero(self) -> None:
        """If rain > ET₀, factor floors at 0 (not negative)."""
        provider = _make_provider(reference_et0_mm=5.0, rain_threshold_mm=20.0)
        provider._data.et0_today = 3.0
        provider._data.precipitation_last_24h = 4.0
        provider._data.precipitation_forecast_24h = 0.0

        provider._calculate_adjustments()

        # factor = 3/5 - 4/3 = 0.6 - 1.33 → clamped to 0.0
        assert provider._data.water_need_factor == 0.0

    def test_zero_reference_et0_defaults_to_1(self) -> None:
        """Avoid division by zero if reference is 0."""
        provider = _make_provider(reference_et0_mm=0.0)
        provider._data.et0_today = 5.0
        provider._data.precipitation_last_24h = 0.0
        provider._data.precipitation_forecast_24h = 0.0

        provider._calculate_adjustments()

        assert provider._data.water_need_factor == 1.0

    def test_zero_et0_today_no_rain_offset(self) -> None:
        """With ET₀=0 and some rain, factor stays at 0."""
        provider = _make_provider(reference_et0_mm=5.0, rain_threshold_mm=20.0)
        provider._data.et0_today = 0.0
        provider._data.precipitation_last_24h = 2.0
        provider._data.precipitation_forecast_24h = 0.0

        provider._calculate_adjustments()

        # factor = 0/5 = 0.0, no rain offset because et0=0
        assert provider._data.water_need_factor == 0.0


class TestRateLimiting:
    """Tests for the 30-minute rate limiting."""

    @pytest.mark.asyncio
    async def test_first_call_always_fetches(self) -> None:
        provider = _make_provider()

        mock_response = _sample_api_response()

        with patch.object(provider, "_fetch_api", new_callable=AsyncMock, return_value=mock_response):
            result = await provider.async_update()

        assert result.last_update is not None
        assert result.et0_today == 5.0

    @pytest.mark.asyncio
    async def test_second_call_within_interval_skips_fetch(self) -> None:
        provider = _make_provider()

        mock_response = _sample_api_response(et0_today=5.0)

        with patch.object(provider, "_fetch_api", new_callable=AsyncMock, return_value=mock_response) as mock_fetch:
            await provider.async_update()  # Erste Abfrage
            await provider.async_update()  # Zweite innerhalb 30min

        # Nur ein API-Aufruf
        assert mock_fetch.call_count == 1

    @pytest.mark.asyncio
    async def test_call_after_interval_fetches_again(self) -> None:
        provider = _make_provider()

        mock_response = _sample_api_response()

        with patch.object(provider, "_fetch_api", new_callable=AsyncMock, return_value=mock_response) as mock_fetch:
            await provider.async_update()

            # Simuliere: 31 Minuten vergangen
            provider._last_fetch = datetime.now(timezone.utc) - timedelta(minutes=31)

            await provider.async_update()

        assert mock_fetch.call_count == 2

    @pytest.mark.asyncio
    async def test_api_error_preserves_last_data(self) -> None:
        """On API error, old data should remain, error logged."""
        provider = _make_provider()

        # Erste erfolgreiche Abfrage
        good_response = _sample_api_response(et0_today=4.0)
        with patch.object(provider, "_fetch_api", new_callable=AsyncMock, return_value=good_response):
            await provider.async_update()

        assert provider.data.et0_today == 4.0

        # Zweite Abfrage schlägt fehl
        provider._last_fetch = datetime.now(timezone.utc) - timedelta(minutes=31)

        with patch.object(provider, "_fetch_api", new_callable=AsyncMock, side_effect=RuntimeError("timeout")):
            result = await provider.async_update()

        # Alte Daten bleiben erhalten
        assert result.et0_today == 4.0
        assert result.last_error == "timeout"


class TestWeatherDataDefaults:
    """Tests for WeatherData default values."""

    def test_defaults(self) -> None:
        data = WeatherData()

        assert data.et0_today == 0.0
        assert data.precipitation_last_24h == 0.0
        assert data.precipitation_forecast_24h == 0.0
        assert data.temperature_max == 0.0
        assert data.water_need_factor == 1.0
        assert data.rain_skip is False
        assert data.last_update is None
        assert data.last_error is None
