"""Generic entity for the WMS WebControl pro API integration."""

from typing import TYPE_CHECKING, override

from wmspro.destination import Destination

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity

from .const import ATTRIBUTION, DOMAIN, MANUFACTURER
from .refresh import PollOutcome

if TYPE_CHECKING:
    from . import WebControlProConfigEntry


class WebControlProGenericEntity(Entity):
    """Foundation of all WMS based entities."""

    _attr_attribution = ATTRIBUTION
    _attr_has_entity_name = True
    _attr_should_poll = False

    # True adds the destination of the entity to the regular poll cycle.
    _base_poll = False

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: WebControlProConfigEntry,
        dest: Destination,
    ) -> None:
        """Initialize the entity with destination channel."""
        config_entry_id = config_entry.entry_id
        dest_id_str = str(dest.id)
        self._entry = config_entry
        self._poller = config_entry.runtime_data.poller
        self._dest = dest
        self._attr_unique_id = dest_id_str
        if self.translation_key:
            self._attr_unique_id += f"-{self.translation_key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, dest_id_str)},
            manufacturer=MANUFACTURER,
            model=dest.animationType.name,
            name=dest.name,
            serial_number=dest_id_str,
            suggested_area=dest.room.name,
            via_device_id=dr.async_get_device_id_by_identifier(
                hass, (DOMAIN, config_entry_id), config_entry_id=config_entry_id
            ),
            configuration_url=f"http://{dest.host}/control",
        )

    @override
    async def async_added_to_hass(self) -> None:
        """Listen to the status polls of the destination."""
        await super().async_added_to_hass()
        self.async_on_remove(
            self._poller.async_add_listener(
                self._dest.id,
                self._handle_poll,
                base_poll=self._base_poll,
                is_moving=self._is_moving if self._base_poll else None,
            )
        )

    @callback
    def _handle_poll(self, outcome: PollOutcome | None) -> None:
        """Handle a status poll, outcome is None for a plain state update."""
        if outcome is not None:
            self._on_poll(outcome)
        self.async_write_ha_state()

    def _on_poll(self, outcome: PollOutcome) -> None:
        """React to the result of a status poll."""

    def _is_moving(self) -> bool:
        """Return True while the regular poll cycle has to skip the entity."""
        return False

    @property
    @override
    def available(self) -> bool:
        """Return if entity is available."""
        return self._dest.available
