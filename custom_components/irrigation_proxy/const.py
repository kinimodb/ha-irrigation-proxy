"""Constants for the Irrigation Proxy integration."""

from typing import Final

DOMAIN: Final = "irrigation_proxy"

# -- Config keys -----------------------------------------------------------

CONF_NAME: Final = "name"

# Zones: list of dicts with keys id, name, valve_entity_id, duration_minutes.
# Order of this list = sequence order.
CONF_ZONES: Final = "zones"
CONF_ZONE_ID: Final = "id"
CONF_ZONE_NAME: Final = "name"
CONF_ZONE_VALVE: Final = "valve_entity_id"
CONF_ZONE_DURATION_MINUTES: Final = "duration_minutes"

# Optional master / pump valve that sits on the main supply line.
# When configured: opens after the zone valve, closes before the zone valve,
# letting pressure drain between zones.
CONF_MASTER_VALVE: Final = "master_valve_entity_id"
CONF_DEPRESSURIZE_SECONDS: Final = "depressurize_seconds"

# Schedule
CONF_SCHEDULE_ENABLED: Final = "schedule_enabled"
CONF_SCHEDULE_START_TIMES: Final = "schedule_start_times"
CONF_SCHEDULE_WEEKDAYS: Final = "schedule_weekdays"

# Timing
CONF_INTER_ZONE_DELAY_SECONDS: Final = "inter_zone_delay_seconds"

# Safety
CONF_MAX_RUNTIME_MINUTES: Final = "max_runtime_minutes"

# -- Weekdays --------------------------------------------------------------

WEEKDAYS: Final[tuple[str, ...]] = (
    "mon",
    "tue",
    "wed",
    "thu",
    "fri",
    "sat",
    "sun",
)

# -- Defaults --------------------------------------------------------------

DEFAULT_DURATION_MINUTES: Final = 15
DEFAULT_MAX_RUNTIME_MINUTES: Final = 60
DEFAULT_INTER_ZONE_DELAY_SECONDS: Final = 30
DEFAULT_DEPRESSURIZE_SECONDS: Final = 5
DEFAULT_SCHEDULE_ENABLED: Final = False

DEFAULT_UPDATE_INTERVAL_SECONDS: Final = 30
DEFAULT_STATE_VERIFY_DELAY_SECONDS: Final = 5
DEFAULT_CLOSE_RETRY_MAX: Final = 3
DEFAULT_SAFETY_MARGIN_SECONDS: Final = 30

# Live-UI tick interval while the sequencer is running (seconds).
TIMER_TICK_INTERVAL_SECONDS: Final = 1

# -- Service names ---------------------------------------------------------

SERVICE_START_PROGRAM: Final = "start_program"
SERVICE_STOP_PROGRAM: Final = "stop_program"

# -- Platforms -------------------------------------------------------------

PLATFORMS: Final[list[str]] = ["switch", "sensor"]
