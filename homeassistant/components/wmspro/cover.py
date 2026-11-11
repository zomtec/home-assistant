"""Support for covers connected with WMS WebControl pro."""

from dataclasses import dataclass
from datetime import datetime
import logging
from time import monotonic
from typing import Any, override

import aiohttp
from wmspro.action import ActionList
from wmspro.const import (
    WMS_WebControl_pro_API_actionDescription as ACTION_DESC,
    WMS_WebControl_pro_API_actionType,
    WMS_WebControl_pro_API_responseType,
)
from wmspro.destination import Destination

from homeassistant.components.cover import (
    ATTR_POSITION,
    ATTR_TILT_POSITION,
    CoverDeviceClass,
    CoverEntity,
    CoverEntityFeature,
)
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.restore_state import ExtraStoredData, RestoreEntity
from homeassistant.util.percentage import (
    percentage_to_ranged_value,
    ranged_value_to_percentage,
)

from . import WebControlProConfigEntry
from .const import (
    ATTR_POSITION_SOURCE,
    FOLLOW_UP_POLL_DELAY,
    MAX_FOLLOW_UP_POLLS,
    POSITION_TOLERANCE,
    SETTLE_DELAY_AFTER_STOP,
    SETTLE_DELAY_AFTER_TRAVEL,
    SETTLE_DELAY_TILT,
    SETTLE_DELAY_UNKNOWN_TRAVEL,
    TRAVEL_DIRECTION_CLOSE,
    TRAVEL_DIRECTION_OPEN,
    TRAVEL_UPDATE_INTERVAL,
)
from .entity import WebControlProGenericEntity
from .refresh import POLL_ATTRIBUTES, PollOutcome, get_poll_stats
from .travel import CoverModel, TravelSettings, wms_percentage_to_position
from .travel_times import get_travel_time

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 1

TRAVEL_SETTINGS = TravelSettings(
    settle_after_travel=SETTLE_DELAY_AFTER_TRAVEL,
    settle_after_stop=SETTLE_DELAY_AFTER_STOP,
    settle_tilt=SETTLE_DELAY_TILT,
    settle_unknown_travel=SETTLE_DELAY_UNKNOWN_TRAVEL,
    position_tolerance=POSITION_TOLERANCE,
)

# The features are set explicitly: the default of Home Assistant drops
# SET_POSITION and the tilt features while position or tilt are unknown.
BASE_FEATURES = (
    CoverEntityFeature.OPEN
    | CoverEntityFeature.CLOSE
    | CoverEntityFeature.STOP
    | CoverEntityFeature.SET_POSITION
)
TILT_FEATURES = (
    BASE_FEATURES
    | CoverEntityFeature.OPEN_TILT
    | CoverEntityFeature.CLOSE_TILT
    | CoverEntityFeature.SET_TILT_POSITION
)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: WebControlProConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the WMS based covers from a config entry."""
    hub = config_entry.runtime_data.hub

    entities: list[WebControlProGenericEntity] = []
    for dest in hub.dests.values():
        if dest.hasAction(ACTION_DESC.AwningDrive):
            entities.append(WebControlProAwning(hass, config_entry, dest))
            if dest.hasAction(ACTION_DESC.ValanceDrive):
                entities.append(WebControlProValance(hass, config_entry, dest))
        elif dest.hasAction(ACTION_DESC.RollerShutterBlindDrive):
            entities.append(WebControlProRollerShutter(hass, config_entry, dest))
        elif dest.hasAction(ACTION_DESC.SlatDrive):
            if dest.hasAction(ACTION_DESC.SlatRotate):
                entities.append(WebControlProSlatRotate(hass, config_entry, dest))
            else:
                entities.append(WebControlProSlat(hass, config_entry, dest))

    async_add_entities(entities)


def _as_int(value: Any) -> int | None:
    """Return a stored value as integer, None if it is not a number."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


@dataclass
class CoverStoredData(ExtraStoredData):
    """State of a cover that is kept over a restart."""

    data: dict[str, int | None]

    def as_dict(self) -> dict[str, Any]:
        """Return the stored state as dictionary."""
        return dict(self.data)


class WebControlProCover(WebControlProGenericEntity, CoverEntity, RestoreEntity):
    """Base representation of a WMS based cover."""

    _drive_action_desc: ACTION_DESC
    _uses_travel_times = True
    _base_poll = True
    _attr_name = None
    _attr_supported_features = BASE_FEATURES
    _unrecorded_attributes = POLL_ATTRIBUTES | {ATTR_POSITION_SOURCE}

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: WebControlProConfigEntry,
        dest: Destination,
    ) -> None:
        """Initialize the cover with an unknown state."""
        super().__init__(hass, config_entry, dest)
        self._model = CoverModel(TRAVEL_SETTINGS)
        self._unsub_tick: CALLBACK_TYPE | None = None
        self._follow_ups = 0

    # --- Lifecycle ---

    @override
    async def async_added_to_hass(self) -> None:
        """Take over the status of the setup poll or the stored state."""
        await super().async_added_to_hass()

        outcome = self._poller.last_outcome.get(self._dest.id)
        if outcome is not None:
            self._on_poll(outcome)

        if self._model.position is None:
            extra = await self.async_get_last_extra_data()
            if extra is not None:
                data = extra.as_dict()
                target = _as_int(data.get("target_position"))
                self._model.restore(
                    _as_int(data.get("position")),
                    _as_int(data.get("tilt")),
                    target,
                    _as_int(data.get("target_tilt")),
                )
                if target is not None:
                    # The restart interrupted a drive, check where it ended.
                    self._poller.async_schedule_poll(
                        self._dest.id, SETTLE_DELAY_UNKNOWN_TRAVEL
                    )

    @override
    async def async_will_remove_from_hass(self) -> None:
        """Stop the position updates."""
        await super().async_will_remove_from_hass()
        if self._unsub_tick is not None:
            self._unsub_tick()
            self._unsub_tick = None

    @property
    @override
    def extra_restore_state_data(self) -> CoverStoredData:
        """Return the state to keep over a restart."""
        return CoverStoredData(self._model.snapshot())

    # --- State ---

    @property
    @override
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the poll counters and the origin of the position."""
        source = self._model.source
        return {
            **get_poll_stats(self._dest).as_attributes(),
            ATTR_POSITION_SOURCE: source.value if source else None,
        }

    @property
    @override
    def current_cover_position(self) -> int | None:
        """Return current position of cover."""
        return self._model.position_int

    @property
    @override
    def is_closed(self) -> bool | None:
        """Return if the cover is closed, None while the position is unknown."""
        position = self._model.position_int
        if position is None:
            return None
        return position == 0

    @property
    @override
    def is_opening(self) -> bool:
        """Return if the cover is opening."""
        return self._model.is_opening

    @property
    @override
    def is_closing(self) -> bool:
        """Return if the cover is closing."""
        return self._model.is_closing

    @override
    def _is_moving(self) -> bool:
        """Return True while a drive is interpolated."""
        return self._model.is_travelling

    # --- Polling ---

    def _measured_tilt(self, outcome: PollOutcome) -> int | None:
        """Return the tilt contained in a poll result."""
        return None

    @override
    def _on_poll(self, outcome: PollOutcome) -> None:
        """Take over a measured position while the cover stands still."""
        if not outcome.ok:
            self._follow_ups = 0
            return

        action = self._dest.action(self._drive_action_desc)
        value = outcome.values.get(action.id)
        position = (
            wms_percentage_to_position(value.get("percentage")) if value else None
        )
        if position is None:
            get_poll_stats(self._dest).no_position += 1
            self._follow_ups = 0
            return

        result = self._model.apply_measurement(
            monotonic(), position, self._measured_tilt(outcome)
        )
        self._sync_ticker()
        if not result.applied:
            return
        if result.deviation and self._follow_ups < MAX_FOLLOW_UP_POLLS:
            # The cover moved without a command of this integration or is
            # still moving. Ask again until two results match.
            self._follow_ups += 1
            self._poller.async_schedule_poll(self._dest.id, FOLLOW_UP_POLL_DELAY)
        else:
            self._follow_ups = 0

    # --- Position updates during a drive ---

    @callback
    def _sync_ticker(self) -> None:
        """Run the position updates only while a drive is interpolated."""
        if self._model.is_travelling and self._unsub_tick is None:
            self._unsub_tick = async_track_time_interval(
                self.hass, self._tick, TRAVEL_UPDATE_INTERVAL
            )
        elif not self._model.is_travelling and self._unsub_tick is not None:
            self._unsub_tick()
            self._unsub_tick = None

    @callback
    def _tick(self, _now: datetime) -> None:
        """Write the interpolated position if it changed."""
        before = self._model.position_int
        finished = self._model.advance(monotonic())
        self._sync_ticker()
        if finished or self._model.position_int != before:
            self.async_write_ha_state()

    # --- Commands ---

    def _travel_times(self) -> tuple[float, float]:
        """Return the travel times for opening and closing in seconds."""
        if not self._uses_travel_times:
            return (0.0, 0.0)
        return (
            get_travel_time(self._entry, self._dest.id, TRAVEL_DIRECTION_OPEN),
            get_travel_time(self._entry, self._dest.id, TRAVEL_DIRECTION_CLOSE),
        )

    async def _async_send(
        self,
        action_list: ActionList,
        response_type: WMS_WebControl_pro_API_responseType = (
            WMS_WebControl_pro_API_responseType.Instant
        ),
    ) -> bool:
        """Send a command to the hub, return True if the hub accepted it."""
        try:
            response = await action_list(responseType=response_type)
        except (aiohttp.ClientError, TimeoutError) as err:
            raise HomeAssistantError(
                f"Error sending command to {self._dest}: {err!r}"
            ) from err

        errors = response.get("errors") if isinstance(response, dict) else None
        if errors:
            get_poll_stats(self._dest).command_error += 1
            _LOGGER.warning(
                "Hub rejected command for %s (%s): %s",
                self._dest,
                self._dest.id,
                errors,
            )
            return False
        return True

    @callback
    def _after_command(self, delay: float) -> None:
        """Update timers and state after an accepted command."""
        self._follow_ups = 0
        self._sync_ticker()
        self._poller.async_schedule_poll(self._dest.id, delay)
        self.async_write_ha_state()

    @callback
    def _after_rejected(self, delay: float) -> None:
        """Check the real state after a rejected command."""
        if not self._model.is_travelling:
            self._poller.async_schedule_poll(self._dest.id, delay)
        self.async_write_ha_state()

    async def _async_move(
        self, action_list: ActionList, position: int, tilt: int | None = None
    ) -> None:
        """Send a drive command and start the calculated drive."""
        if not await self._async_send(action_list):
            self._after_rejected(SETTLE_DELAY_UNKNOWN_TRAVEL)
            return
        time_open, time_close = self._travel_times()
        delay = self._model.start_move(
            monotonic(), position, time_open, time_close, tilt=tilt
        )
        self._after_command(delay)

    @override
    async def async_set_cover_position(self, **kwargs: Any) -> None:
        """Move the cover to a specific position."""
        position = kwargs[ATTR_POSITION]
        action = self._dest.action(self._drive_action_desc)
        await self._async_move(action.prep(percentage=100 - position), position)

    @override
    async def async_open_cover(self, **kwargs: Any) -> None:
        """Open the cover."""
        action = self._dest.action(self._drive_action_desc)
        await self._async_move(action.prep(percentage=0), 100)

    @override
    async def async_close_cover(self, **kwargs: Any) -> None:
        """Close the cover."""
        action = self._dest.action(self._drive_action_desc)
        await self._async_move(action.prep(percentage=100), 0)

    @override
    async def async_stop_cover(self, **kwargs: Any) -> None:
        """Stop the device if in motion."""
        action = self._dest.action(
            ACTION_DESC.ManualCommand,
            WMS_WebControl_pro_API_actionType.Stop,
        )
        if not await self._async_send(
            action.prep(), WMS_WebControl_pro_API_responseType.Detailed
        ):
            self._after_rejected(SETTLE_DELAY_AFTER_STOP)
            return
        self._after_command(self._model.stop(monotonic()))


class WebControlProAwning(WebControlProCover):
    """Representation of a WMS based awning."""

    _attr_device_class = CoverDeviceClass.AWNING
    _drive_action_desc = ACTION_DESC.AwningDrive


class WebControlProValance(WebControlProCover):
    """Representation of a WMS based valance."""

    _attr_device_class = CoverDeviceClass.SHADE
    _attr_translation_key = "valance"
    _drive_action_desc = ACTION_DESC.ValanceDrive
    # The travel times belong to the main drive of the destination.
    _uses_travel_times = False


class WebControlProRollerShutter(WebControlProCover):
    """Representation of a WMS based roller shutter or blind."""

    _attr_device_class = CoverDeviceClass.SHUTTER
    _drive_action_desc = ACTION_DESC.RollerShutterBlindDrive


class WebControlProSlat(WebControlProCover):
    """Representation of a WMS based blind using a slat drive."""

    _attr_device_class = CoverDeviceClass.BLIND
    _drive_action_desc = ACTION_DESC.SlatDrive


class WebControlProSlatRotate(WebControlProSlat):
    """Representation of a WMS based blind which supports tilting."""

    _tilt_action_desc = ACTION_DESC.SlatRotate
    _attr_supported_features = TILT_FEATURES

    def _rotation_to_tilt(self, rotation: float) -> int:
        """Convert a WMS rotation to a tilt position (0 closed, 100 open)."""
        action = self._dest.action(self._tilt_action_desc)
        tilt = 100 - ranged_value_to_percentage(
            (action.minValue, action.maxValue), rotation
        )
        return max(0, min(100, tilt))

    def _tilt_to_rotation(self, tilt: int) -> float:
        """Convert a tilt position (0 closed, 100 open) to a WMS rotation."""
        action = self._dest.action(self._tilt_action_desc)
        return percentage_to_ranged_value(
            (action.minValue, action.maxValue), 100 - tilt
        )

    @override
    def _measured_tilt(self, outcome: PollOutcome) -> int | None:
        """Return the tilt contained in a poll result."""
        action = self._dest.action(self._tilt_action_desc)
        value = outcome.values.get(action.id)
        rotation = value.get("rotation") if value else None
        if isinstance(rotation, bool) or not isinstance(rotation, (int, float)):
            return None
        return self._rotation_to_tilt(rotation)

    @property
    @override
    def current_cover_tilt_position(self) -> int | None:
        """Return current position of cover tilt."""
        return self._model.tilt

    @override
    async def async_open_cover(self, **kwargs: Any) -> None:
        """Open the cover and tilt to minimum like the WMS WebControl pro."""
        action_drive = self._dest.action(self._drive_action_desc)
        action_list = action_drive.prep(percentage=0)
        action_tilt = self._dest.action(self._tilt_action_desc)
        action_list += action_tilt.prep(rotation=action_tilt.minValue)
        await self._async_move(
            action_list, 100, self._rotation_to_tilt(action_tilt.minValue)
        )

    @override
    async def async_close_cover(self, **kwargs: Any) -> None:
        """Close the cover and tilt to maximum like the WMS WebControl pro."""
        action_drive = self._dest.action(self._drive_action_desc)
        action_list = action_drive.prep(percentage=100)
        action_tilt = self._dest.action(self._tilt_action_desc)
        action_list += action_tilt.prep(rotation=action_tilt.maxValue)
        await self._async_move(
            action_list, 0, self._rotation_to_tilt(action_tilt.maxValue)
        )

    @override
    async def async_set_cover_position(self, **kwargs: Any) -> None:
        """Move the cover to a specific position and tilt for open/close."""
        target_position = kwargs[ATTR_POSITION]
        if target_position == 0:
            await self.async_close_cover()
        elif target_position == 100:
            await self.async_open_cover()
        else:
            await super().async_set_cover_position(**kwargs)

    async def _async_tilt(self, rotation: float, tilt: int) -> None:
        """Send a tilt-only command, it ends a running drive."""
        action = self._dest.action(self._tilt_action_desc)
        if not await self._async_send(action.prep(rotation=rotation)):
            self._after_rejected(SETTLE_DELAY_TILT)
            return
        self._after_command(self._model.set_tilt(monotonic(), tilt))

    @override
    async def async_set_cover_tilt_position(self, **kwargs: Any) -> None:
        """Set the cover tilt position."""
        tilt = kwargs[ATTR_TILT_POSITION]
        await self._async_tilt(self._tilt_to_rotation(tilt), tilt)

    @override
    async def async_open_cover_tilt(self, **kwargs: Any) -> None:
        """Open the cover tilt."""
        # 0 is the open position for the tilt in WMS WebControl pro,
        # with the open position the slat is parallel to the ground.
        # This position will let the most light through and
        # is required to fully store the cover in the box.
        await self._async_tilt(0, self._rotation_to_tilt(0))

    @override
    async def async_close_cover_tilt(self, **kwargs: Any) -> None:
        """Close the cover tilt."""
        action = self._dest.action(self._tilt_action_desc)
        # maxValue is the close position for the tilt in WMS WebControl pro,
        # with the close position the slat is perpendicular to the ground.
        # This position will block the light best.
        await self._async_tilt(action.maxValue, self._rotation_to_tilt(action.maxValue))

    async def async_set_cover_position_and_tilt(self, **kwargs: Any) -> None:
        """Handle the service action call to set cover position and tilt."""
        position = kwargs[ATTR_POSITION]
        tilt = kwargs[ATTR_TILT_POSITION]
        action_drive = self._dest.action(self._drive_action_desc)
        action_list = action_drive.prep(percentage=100 - position)
        action_tilt = self._dest.action(self._tilt_action_desc)
        action_list += action_tilt.prep(rotation=self._tilt_to_rotation(tilt))
        await self._async_move(action_list, position, tilt)
