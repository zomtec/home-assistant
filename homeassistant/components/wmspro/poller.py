"""Central status polling for the WMS WebControl pro API integration.

All status requests of the integration go through this poller. Entities do
not poll on their own (should_poll is False), they register a listener and
ask the poller for a single delayed poll after a command.
"""

import asyncio
from collections.abc import Callable
import logging

from wmspro.webcontrol import WebControlPro

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.event import async_call_later, async_track_time_interval

from .const import COVER_POLL_INTERVAL
from .refresh import PollOutcome, async_refresh_destination

_LOGGER = logging.getLogger(__name__)

type PollListener = Callable[[PollOutcome | None], None]


class WmsStatusPoller:
    """Serialize and schedule the status requests of one hub."""

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, hub: WebControlPro
    ) -> None:
        """Initialize the poller."""
        self._hass = hass
        self._entry = entry
        self._hub = hub
        self._lock = asyncio.Lock()
        self._listeners: dict[int, list[PollListener]] = {}
        self._moving_checks: dict[int, list[Callable[[], bool]]] = {}
        self._base_poll_ids: set[int] = set()
        self._pending: dict[int, CALLBACK_TYPE] = {}
        self._unsub_interval: CALLBACK_TYPE | None = None
        self.last_outcome: dict[int, PollOutcome] = {}

    # --- Listeners ---

    @callback
    def async_add_listener(
        self,
        dest_id: int,
        listener: PollListener,
        *,
        base_poll: bool = False,
        is_moving: Callable[[], bool] | None = None,
    ) -> CALLBACK_TYPE:
        """Register a listener for the poll results of a destination.

        base_poll adds the destination to the regular poll cycle. is_moving
        lets the regular cycle skip the destination while it returns True.
        """
        self._listeners.setdefault(dest_id, []).append(listener)
        if base_poll:
            self._base_poll_ids.add(dest_id)
        if is_moving is not None:
            self._moving_checks.setdefault(dest_id, []).append(is_moving)

        @callback
        def _remove() -> None:
            self._listeners[dest_id].remove(listener)
            if is_moving is not None:
                self._moving_checks[dest_id].remove(is_moving)

        return _remove

    @callback
    def async_notify(self, dest_id: int, outcome: PollOutcome | None = None) -> None:
        """Call the listeners of a destination.

        outcome is None if no status was requested and the entities only
        have to write their state again.
        """
        for listener in list(self._listeners.get(dest_id, ())):
            listener(outcome)

    # --- Polling ---

    async def async_poll(
        self, dest_id: int, *, raise_on_error: bool = False
    ) -> PollOutcome:
        """Request the status of one destination once and notify listeners."""
        dest = self._hub.dests[dest_id]
        async with self._lock:
            outcome = await async_refresh_destination(
                dest, raise_on_error=raise_on_error
            )
        self.last_outcome[dest_id] = outcome
        self.async_notify(dest_id, outcome)
        return outcome

    @callback
    def async_schedule_poll(self, dest_id: int, delay: float) -> None:
        """Schedule a single poll, replacing a pending one of the destination."""
        self.async_cancel_poll(dest_id)

        @callback
        def _fire(_now) -> None:
            self._pending.pop(dest_id, None)
            self._entry.async_create_background_task(
                self._hass,
                self.async_poll(dest_id),
                name=f"wmspro scheduled poll {dest_id}",
            )

        self._pending[dest_id] = async_call_later(self._hass, delay, _fire)

    @callback
    def async_schedule_poll_all(self, delay: float) -> None:
        """Schedule a single poll for every destination of the regular cycle."""
        for dest_id in self._hub.dests:
            if dest_id in self._base_poll_ids:
                self.async_schedule_poll(dest_id, delay)

    @callback
    def async_cancel_poll(self, dest_id: int) -> None:
        """Cancel the pending poll of a destination."""
        if unsub := self._pending.pop(dest_id, None):
            unsub()

    def _skip_in_cycle(self, dest_id: int) -> bool:
        if dest_id in self._pending:
            return True
        return any(check() for check in self._moving_checks.get(dest_id, ()))

    async def _async_poll_cycle(self) -> None:
        """Poll all destinations of the regular cycle one after another."""
        for dest_id in list(self._hub.dests):
            if dest_id not in self._base_poll_ids or self._skip_in_cycle(dest_id):
                continue
            await self.async_poll(dest_id)

    # --- Lifecycle ---

    @callback
    def async_start(self) -> None:
        """Start the regular poll cycle."""

        @callback
        def _cycle(_now) -> None:
            self._entry.async_create_background_task(
                self._hass, self._async_poll_cycle(), name="wmspro poll cycle"
            )

        self._unsub_interval = async_track_time_interval(
            self._hass, _cycle, COVER_POLL_INTERVAL
        )

    @callback
    def async_stop(self) -> None:
        """Stop the regular cycle and cancel all pending polls."""
        if self._unsub_interval:
            self._unsub_interval()
            self._unsub_interval = None
        for dest_id in list(self._pending):
            self.async_cancel_poll(dest_id)
