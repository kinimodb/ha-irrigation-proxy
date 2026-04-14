"""Constants for the Irrigation Proxy integration."""

from typing import Final

DOMAIN: Final = "irrigation_proxy"

# Config keys
CONF_NAME: Final = "name"
CONF_ZONES: Final = "zones"
CONF_DURATION_MINUTES: Final = "duration_minutes"
CONF_MAX_RUNTIME_MINUTES: Final = "max_runtime_minutes"
CONF_RAIN_THRESHOLD_MM: Final = "rain_threshold_mm"

# Schedule / v0.4.0 config keys
CONF_SCHEDULE_ENABLED: Final = "schedule_enabled"
CONF_SCHEDULE_START_TIMES: Final = "schedule_start_times"
CONF_SCHEDULE_WEEKDAYS: Final = "schedule_weekdays"
CONF_INTER_ZONE_DELAY_SECONDS: Final = "inter_zone_delay_seconds"
CONF_ZONE_DURATIONS: Final = "zone_durations"  # dict[entity_id, minutes]
CONF_RAIN_ADJUST_MODE: Final = "rain_adjust_mode"

# Rain adjust modes
RAIN_ADJUST_OFF: Final = "off"
RAIN_ADJUST_HARD: Final = "hard"
RAIN_ADJUST_SCALE: Final = "scale"
RAIN_ADJUST_MODES: Final[tuple[str, ...]] = (
    RAIN_ADJUST_OFF,
    RAIN_ADJUST_HARD,
    RAIN_ADJUST_SCALE,
)

# Weekday identifiers (Mon..Sun, matches datetime.weekday() order)
WEEKDAYS: Final[tuple[str, ...]] = (
    "mon",
    "tue",
    "wed",
    "thu",
    "fri",
    "sat",
    "sun",
)

# Defaults
DEFAULT_MAX_RUNTIME_MINUTES: Final = 60
DEFAULT_DURATION_MINUTES: Final = 15
DEFAULT_UPDATE_INTERVAL_SECONDS: Final = 30
DEFAULT_STATE_VERIFY_DELAY_SECONDS: Final = 5
DEFAULT_CLOSE_RETRY_MAX: Final = 3
DEFAULT_SAFETY_MARGIN_SECONDS: Final = 30
DEFAULT_PAUSE_BETWEEN_ZONES_SECONDS: Final = 30
DEFAULT_SCHEDULE_ENABLED: Final = False
DEFAULT_RAIN_ADJUST_MODE: Final = RAIN_ADJUST_OFF

# Live-UI tick interval while the sequencer is running (seconds)
TIMER_TICK_INTERVAL_SECONDS: Final = 1

# Weather defaults
DEFAULT_RAIN_THRESHOLD_MM: Final = 5.0
DEFAULT_REFERENCE_ET0_MM: Final = 5.0
WEATHER_UPDATE_INTERVAL_MINUTES: Final = 30
OPEN_METEO_BASE_URL: Final = "https://api.open-meteo.com/v1/forecast"

# Coarse conversion from watering time to mm irrigation depth.
# Tunable later once real flow/area data is available; used only by the
# optional "scale" rain-adjust mode to decide skip vs. shorten.
ASSUMED_FLOW_MM_PER_MIN: Final = 0.25

# Service names
SERVICE_START_PROGRAM: Final = "start_program"
SERVICE_STOP_PROGRAM: Final = "stop_program"

# Platforms
PLATFORMS: Final[list[str]] = ["switch", "sensor", "binary_sensor"]
