"""Support for number settings of covers connected with WMS WebControl pro."""

from collections.abc import Callable
from typing import override

from wmspro.const import WMS_WebControl_pro_API_actionDescription as ACTION_DESC

from homeassistant.components.number import NumberDeviceClass, NumberEntity, NumberMode
from homeassistant.const import EntityCategory, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import WebControlProConfigEntry
from .const import (
    SETTLE_DELAY_TILT,
    TRAVEL_DIRECTION_CLOSE,
    TRAVEL_DIRECTION_OPEN,
    TRAVEL_TIME_MAX,
    TRAVEL_TIME_STEP,
)
from .entity import WebControlProGenericEntity
from .refresh import PollOutcome
from .travel_times import async_set_travel_time, get_travel_time

PARALLEL_UPDATES = 1

# Destinations with one of these actions get a cover entity with travel times.
_TRAVEL_TIME_ACTIONS = (
    ACTION_DESC.AwningDrive,
    ACTION_DESC.RollerShutterBlindDrive,
    ACTION_DESC.SlatDrive,
)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: WebControlProConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the WMS based number entities from a config entry."""
    hub = config_entry.runtime_data.hub

    entities: list[WebControlProGenericEntity] = []
    for dest in hub.dests.values():
        if any(dest.hasAction(desc) for desc in _TRAVEL_TIME_ACTIONS):
            entities.append(WebControlProTravelTimeOpen(hass, config_entry, dest))
            entities.append(WebControlProTravelTimeClose(hass, config_entry, dest))
        if dest.hasAction(ACTION_DESC.SlatRotate):
            entities.append(WebControlProSlatRangeMin(hass, config_entry, dest))
            entities.append(WebControlProSlatRangeMax(hass, config_entry, dest))
            entities.append(WebControlProSlatRotationRaw(hass, config_entry, dest))
            if not dest.hasAction(ACTION_DESC.SlatDrive):
                # Only add the numeric slat rotation entity if no cover entity exists
                entities.append(WebControlProSlatRotation(hass, config_entry, dest))

    async_add_entities(entities)


class WebControlProSlatRange(WebControlProGenericEntity, NumberEntity):
    """Representation of a WMS based range-option for a slat-based cover."""

    _attr_entity_category = EntityCategory.CONFIG

    _value_name: str
    _value_func: Callable[[float, float], float]

    @override
    def _on_poll(self, outcome: PollOutcome) -> None:
        """Learn the current rotation from a successful status poll."""
        if not outcome.ok:
            return

        # Learn min/max rotation if different from action limits
        action = self._dest.action(ACTION_DESC.SlatRotate)
        rotation = action["rotation"]
        if rotation and action.wms__minValue < rotation < action.wms__maxValue:
            value = self._value_func(self.native_value, rotation)
            if value != self.native_value:
                action[self._value_name] = value
                self._entry.async_create_task(
                    self.hass, action.sync(), name="wmspro persist rotation range"
                )

    @property
    @override
    def native_min_value(self) -> float:
        """Return the minimum value."""
        action = self._dest.action(ACTION_DESC.SlatRotate)
        # Use wms__ prefix to get the raw value from the hub without overwrite
        return self._value_func(action.wms__minValue, 0)

    @property
    @override
    def native_max_value(self) -> float:
        """Return the maximum value."""
        action = self._dest.action(ACTION_DESC.SlatRotate)
        # Use wms__ prefix to get the raw value from the hub without overwrite
        return self._value_func(0, action.wms__maxValue)

    @property
    @override
    def native_value(self) -> float:
        """Return the current min/max value."""
        action = self._dest.action(ACTION_DESC.SlatRotate)
        # Pull the current min/max rotation from the custom overwrite if set
        value = action[self._value_name]
        # -75 and 75 are community-provided sane defaults for various devices
        if value is None:
            value = self._value_func(-75, 75)
        return value

    @override
    async def async_set_native_value(self, value: float) -> None:
        """Update the current min/max value."""
        action = self._dest.action(ACTION_DESC.SlatRotate)
        # Push the new min/max rotation to the hub as custom overwrite via the action items
        action[self._value_name] = value
        # Persist the change now and let the entities of the destination
        # write their state, no status request is needed for this.
        await action.sync()
        self._poller.async_notify(self._dest.id)


class WebControlProSlatRangeMin(WebControlProSlatRange):
    """Representation of the minimum rotation range-option for a slat-based cover."""

    _attr_translation_key = "rotation-min"

    _value_name = "minValue"
    _value_func = staticmethod(min)


class WebControlProSlatRangeMax(WebControlProSlatRange):
    """Representation of the maximum rotation range-option for a slat-based cover."""

    _attr_translation_key = "rotation-max"

    _value_name = "maxValue"
    _value_func = staticmethod(max)


class WebControlProSlatRotation(WebControlProGenericEntity, NumberEntity):
    """Representation of the WMS based slat-rotation for a slat-based cover."""

    _attr_translation_key = "rotation"

    @property
    @override
    def native_min_value(self) -> float:
        """Return the minimum value."""
        action = self._dest.action(ACTION_DESC.SlatRotate)
        return action.minValue

    @property
    @override
    def native_max_value(self) -> float:
        """Return the maximum value."""
        action = self._dest.action(ACTION_DESC.SlatRotate)
        return action.maxValue

    @property
    @override
    def native_value(self) -> float | None:
        """Return the current value."""
        action = self._dest.action(ACTION_DESC.SlatRotate)
        rotation = action["rotation"]
        if rotation is None:
            return None
        return rotation

    @override
    async def async_set_native_value(self, value: float) -> None:
        """Update the current value."""
        action = self._dest.action(ACTION_DESC.SlatRotate)
        await action(rotation=value)
        self._poller.async_schedule_poll(self._dest.id, SETTLE_DELAY_TILT)


class WebControlProSlatRotationRaw(WebControlProSlatRotation):
    """Representation of the WMS based raw slat-rotation for a slat-based cover."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "rotation-raw"

    @property
    @override
    def native_min_value(self) -> float:
        """Return the minimum value."""
        action = self._dest.action(ACTION_DESC.SlatRotate)
        return action.wms__minValue

    @property
    @override
    def native_max_value(self) -> float:
        """Return the maximum value."""
        action = self._dest.action(ACTION_DESC.SlatRotate)
        return action.wms__maxValue


class WebControlProTravelTime(WebControlProGenericEntity, NumberEntity):
    """Representation of the travel time of a cover for one direction.

    0 seconds means that the travel time is not configured, the target of a
    drive is then taken over at once.
    """

    _attr_device_class = NumberDeviceClass.DURATION
    _attr_entity_category = EntityCategory.CONFIG
    _attr_mode = NumberMode.BOX
    _attr_native_max_value = TRAVEL_TIME_MAX
    _attr_native_min_value = 0.0
    _attr_native_step = TRAVEL_TIME_STEP
    _attr_native_unit_of_measurement = UnitOfTime.SECONDS

    _direction: str

    @property
    @override
    def available(self) -> bool:
        """Return True, the setting does not depend on the radio link."""
        return True

    @property
    @override
    def native_value(self) -> float:
        """Return the travel time in seconds."""
        return get_travel_time(self._entry, self._dest.id, self._direction)

    @override
    async def async_set_native_value(self, value: float) -> None:
        """Store the travel time in seconds."""
        async_set_travel_time(
            self.hass, self._entry, self._dest.id, self._direction, value
        )
        self.async_write_ha_state()


class WebControlProTravelTimeOpen(WebControlProTravelTime):
    """Representation of the travel time for opening a cover."""

    _attr_translation_key = "travel-time-open"
    _direction = TRAVEL_DIRECTION_OPEN


class WebControlProTravelTimeClose(WebControlProTravelTime):
    """Representation of the travel time for closing a cover."""

    _attr_translation_key = "travel-time-close"
    _direction = TRAVEL_DIRECTION_CLOSE
