"""Constants for the WMS WebControl pro API integration."""

from datetime import timedelta

DOMAIN = "wmspro"
SUGGESTED_HOST = "webcontrol"

ATTRIBUTION = "Data provided by WMS WebControl pro API"
MANUFACTURER = "WAREMA Renkhoff SE"

BRIGHTNESS_SCALE = (1, 100)
SERVICE_SET_COVER_POSITION_AND_TILT = "set_cover_position_and_tilt"

# --- Status polling ---

# Interval of the regular status poll of each cover.
COVER_POLL_INTERVAL = timedelta(minutes=30)
# Minimum pause in seconds between two status requests sent to the hub.
STATUS_REQUEST_SPACING = 0.5
# Warn once if a destination did not deliver a status for this long.
STALE_WARNING_AFTER = timedelta(hours=24)
# Seconds between a command to a light, switch or number and its status poll.
COMMAND_POLL_DELAY = 2.0

# --- Calculated cover position ---

# Interval of the position updates while a drive is interpolated.
TRAVEL_UPDATE_INTERVAL = timedelta(seconds=1)
# Seconds between the calculated end of a drive and the status poll.
SETTLE_DELAY_AFTER_TRAVEL = 2.0
# Seconds between a stop command and the status poll.
SETTLE_DELAY_AFTER_STOP = 2.0
# Seconds between a tilt-only command and the status poll.
SETTLE_DELAY_TILT = 5.0
# Seconds between a drive command and the status poll if the travel time
# is not configured or the start position is unknown.
SETTLE_DELAY_UNKNOWN_TRAVEL = 120.0
# Seconds until the next poll if a measured position differs from the
# displayed one, and the maximum number of such polls in a row.
FOLLOW_UP_POLL_DELAY = 30.0
MAX_FOLLOW_UP_POLLS = 3
# Difference in percent points that counts as a deviation.
POSITION_TOLERANCE = 3

# --- Travel times, stored in the options of the config entry ---

CONF_TRAVEL_TIMES = "travel_times"
TRAVEL_DIRECTION_OPEN = "open"
TRAVEL_DIRECTION_CLOSE = "close"
TRAVEL_TIME_MAX = 300.0
TRAVEL_TIME_STEP = 0.1

ATTR_POSITION_SOURCE = "wms_position_source"
