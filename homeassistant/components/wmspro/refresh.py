"""Radio-friendly status refresh for the WMS WebControl pro API integration.

The refresh of pywmspro retries a status request three times within a few
seconds whenever the hub reports "radio busy" and logs a warning each time
this fails. In a busy WMS mesh this multiplies the radio traffic and floods
the log. This module asks only once per poll, keeps the previous values on
failure and counts the outcome per destination instead of logging it.

It uses private members of pywmspro, which is safe as long as the manifest
pins the exact library version.
"""

from dataclasses import dataclass, field
from datetime import datetime
import logging
import time
from typing import Any

import aiohttp
from wmspro.const import WMS_WebControl_pro_API_drivingCause
from wmspro.destination import Destination

from homeassistant.util import dt as dt_util

from .const import STALE_WARNING_AFTER

_LOGGER = logging.getLogger(__name__)

# Error code of the hub: the radio channel is occupied, nothing was sent.
ERROR_RADIO_BUSY = 0x50005

_STATS_ATTR = "_ha_poll_stats"


@dataclass(frozen=True)
class PollOutcome:
    """Result of one status request of a destination.

    result is "ok", "busy", "empty" or "error". values maps the id of each
    action contained in this response to its value dictionary.
    """

    result: str
    values: dict[int, dict[str, Any]] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        """Return True if the hub delivered data for the destination."""
        return self.result == "ok"


@dataclass
class PollStats:
    """Outcome counters of the status polls of one destination."""

    ok: int = 0
    busy: int = 0
    empty: int = 0
    error: int = 0
    no_position: int = 0
    command_error: int = 0
    last_ok: datetime | None = None
    last_result: str | None = None
    failing_since: float | None = None
    warned: bool = False

    def as_attributes(self) -> dict[str, Any]:
        """Return the counters as extra state attributes."""
        return {
            "wms_poll_ok": self.ok,
            "wms_poll_busy": self.busy,
            "wms_poll_empty": self.empty,
            "wms_poll_error": self.error,
            "wms_poll_no_position": self.no_position,
            "wms_command_error": self.command_error,
            "wms_poll_last_ok": self.last_ok.isoformat() if self.last_ok else None,
            "wms_poll_last_result": self.last_result,
        }


POLL_ATTRIBUTES = frozenset(PollStats().as_attributes())


def get_poll_stats(dest: Destination) -> PollStats:
    """Return the poll counters stored on a destination."""
    stats = getattr(dest, _STATS_ATTR, None)
    if stats is None:
        stats = PollStats()
        setattr(dest, _STATS_ATTR, stats)
    return stats


def _note_failure(dest: Destination, stats: PollStats, result: str) -> None:
    """Count a failed poll and warn once if a destination stays silent."""
    stats.last_result = result
    now = time.monotonic()
    if stats.failing_since is None:
        stats.failing_since = now
    elif (
        not stats.warned
        and now - stats.failing_since >= STALE_WARNING_AFTER.total_seconds()
    ):
        stats.warned = True
        _LOGGER.warning(
            "No status for %s (%s) since %d minutes, last result: %s",
            dest,
            dest.id,
            (now - stats.failing_since) // 60,
            result,
        )


def _note_success(dest: Destination, stats: PollStats) -> None:
    """Count a successful poll and report the end of a silent period."""
    if stats.warned:
        _LOGGER.warning("Status for %s (%s) is available again", dest, dest.id)
    stats.ok += 1
    stats.last_ok = dt_util.utcnow()
    stats.last_result = "ok"
    stats.failing_since = None
    stats.warned = False


async def async_refresh_destination(
    dest: Destination, *, raise_on_error: bool = False
) -> PollOutcome:
    """Request the status of a destination once, without retries.

    On any failure the previously known values are kept.
    """
    stats = get_poll_stats(dest)

    # Persist pending changes of the action overwrites like the library does.
    for action in dest.actions.values():
        await action.sync()

    try:
        status = await dest._control._getStatus(dest.id)  # noqa: SLF001
    except (aiohttp.ClientError, TimeoutError) as err:
        if raise_on_error:
            raise
        stats.error += 1
        _note_failure(dest, stats, f"error: {err!r}")
        return PollOutcome("error")

    if not status:
        stats.empty += 1
        _note_failure(dest, stats, "empty")
        return PollOutcome("empty")

    if ERROR_RADIO_BUSY in (status.get("errors") or ()):
        stats.busy += 1
        _note_failure(dest, stats, "busy")
        return PollOutcome("busy")

    dest._status = status  # noqa: SLF001

    refreshed = False
    values: dict[int, dict[str, Any]] = {}
    for detail in status.get("details") or ():
        if detail.get("destinationId") != dest.id or "data" not in detail:
            continue

        refreshed = True
        data = detail["data"]
        if "drivingCause" in data:
            dest._drivingCause = WMS_WebControl_pro_API_drivingCause(  # noqa: SLF001
                data["drivingCause"]
            )
        if "heartbeatError" in data:
            dest._heartbeatError = data["heartbeatError"]  # noqa: SLF001
        if "blocking" in data:
            dest._blocking = data["blocking"]  # noqa: SLF001

        for product in data.get("productData", []):
            action_id = product["actionId"]
            if action_id in dest.actions and "value" in product:
                dest.actions[action_id]._update_params(product["value"])  # noqa: SLF001
                values[action_id] = dict(product["value"])
            else:
                dest._unknownProducts[action_id] = product  # noqa: SLF001

    if not refreshed:
        stats.empty += 1
        _note_failure(dest, stats, "empty")
        return PollOutcome("empty")

    _note_success(dest, stats)
    return PollOutcome("ok", values)
