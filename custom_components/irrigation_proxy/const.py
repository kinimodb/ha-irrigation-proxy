"""Constants for the Irrigation Proxy integration."""

from typing import Final

DOMAIN: Final = "irrigation_proxy"

# Config keys
CONF_NAME: Final = "name"
CONF_ZONES: Final = "zones"
CONF_DURATION_MINUTES: Final = "duration_minutes"
CONF_MAX_RUNTIME_MINUTES: Final = "max_runtime_minutes"

# Defaults
DEFAULT_MAX_RUNTIME_MINUTES: Final = 60
DEFAULT_DURATION_MINUTES: Final = 15
DEFAULT_UPDATE_INTERVAL_SECONDS: Final = 30
DEFAULT_STATE_VERIFY_DELAY_SECONDS: Final = 5
DEFAULT_CLOSE_RETRY_MAX: Final = 3
DEFAULT_SAFETY_MARGIN_SECONDS: Final = 30

# Platforms (Sprint 1: switch only)
PLATFORMS: Final[list[str]] = ["switch"]
