"""Travel times of the covers, stored in the options of the config entry.

Layout of the options:
{"travel_times": {"<destination id>": {"open": <seconds>, "close": <seconds>}}}
A missing entry means 0 seconds, the target of a drive is then taken over
at once.
"""

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback

from .const import CONF_TRAVEL_TIMES


def get_travel_time(entry: ConfigEntry, dest_id: int, direction: str) -> float:
    """Return the travel time of a destination in seconds, 0 if not set."""
    times = entry.options.get(CONF_TRAVEL_TIMES, {}).get(str(dest_id), {})
    try:
        return max(0.0, float(times.get(direction, 0)))
    except TypeError, ValueError:
        return 0.0


@callback
def async_set_travel_time(
    hass: HomeAssistant, entry: ConfigEntry, dest_id: int, direction: str, value: float
) -> None:
    """Store the travel time of a destination in seconds."""
    all_times = {
        key: dict(times)
        for key, times in entry.options.get(CONF_TRAVEL_TIMES, {}).items()
    }
    all_times.setdefault(str(dest_id), {})[direction] = float(value)
    hass.config_entries.async_update_entry(
        entry, options={**entry.options, CONF_TRAVEL_TIMES: all_times}
    )
