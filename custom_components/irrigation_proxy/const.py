"""Constants for the Irrigation Proxy integration."""

from typing import Final

DOMAIN: Final = "irrigation_proxy"

# Config keys
CONF_NAME: Final = "name"
CONF_ZONES: Final = "zones"
CONF_DURATION_MINUTES: Final = "duration_minutes"
CONF_MAX_RUNTIME_MINUTES: Final = "max_runtime_minutes"
CONF_RAIN_THRESHOLD_MM: Final = "rain_threshold_mm"

# Defaults
DEFAULT_MAX_RUNTIME_MINUTES: Final = 60
DEFAULT_DURATION_MINUTES: Final = 15
DEFAULT_UPDATE_INTERVAL_SECONDS: Final = 30
DEFAULT_STATE_VERIFY_DELAY_SECONDS: Final = 5
DEFAULT_CLOSE_RETRY_MAX: Final = 3
DEFAULT_SAFETY_MARGIN_SECONDS: Final = 30
DEFAULT_PAUSE_BETWEEN_ZONES_SECONDS: Final = 30

# Weather defaults
DEFAULT_RAIN_THRESHOLD_MM: Final = 5.0
DEFAULT_REFERENCE_ET0_MM: Final = 5.0
WEATHER_UPDATE_INTERVAL_MINUTES: Final = 30
OPEN_METEO_BASE_URL: Final = "https://api.open-meteo.com/v1/forecast"

# Service names
SERVICE_START_PROGRAM: Final = "start_program"
SERVICE_STOP_PROGRAM: Final = "stop_program"

# Platforms
PLATFORMS: Final[list[str]] = ["switch", "sensor", "binary_sensor"]
