"""The WMS WebControl pro API integration."""

from dataclasses import dataclass
from pathlib import Path
import shutil

import aiohttp
from wmspro.webcontrol import WebControlPro

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv, device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import STORAGE_DIR
from homeassistant.helpers.typing import UNDEFINED, ConfigType

from .const import DOMAIN, MANUFACTURER, STATUS_REQUEST_SPACING
from .poller import WmsStatusPoller
from .services import async_setup_services

PLATFORMS: list[Platform] = [
    Platform.BUTTON,
    Platform.COVER,
    Platform.LIGHT,
    Platform.NUMBER,
    Platform.SCENE,
    Platform.SWITCH,
]


@dataclass
class WebControlProData:
    """Runtime data of a config entry."""

    hub: WebControlPro
    poller: WmsStatusPoller


type WebControlProConfigEntry = ConfigEntry[WebControlProData]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


def _build_storage_config_dir(
    hass: HomeAssistant, entry: WebControlProConfigEntry
) -> Path:
    """Build the path to the config directory for a given config entry."""
    return Path(hass.config.path(STORAGE_DIR, f"{DOMAIN}-{entry.entry_id}"))


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the component."""
    async_setup_services(hass)
    return True


async def async_setup_entry(
    hass: HomeAssistant, entry: WebControlProConfigEntry
) -> None:
    """Set up wmspro from a config entry."""
    host = entry.data[CONF_HOST]
    session = async_get_clientsession(hass)
    config_dir = _build_storage_config_dir(hass, entry)
    config_dir.mkdir(parents=True, exist_ok=True)
    hub = WebControlPro(host, session, str(config_dir))
    hub._status_wait = STATUS_REQUEST_SPACING  # noqa: SLF001

    try:
        await hub.ping()
    except (aiohttp.ClientError, TimeoutError) as err:
        raise ConfigEntryNotReady(f"Error while connecting to {host}") from err

    device_registry = dr.async_get(hass)
    device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        connections={(dr.CONNECTION_NETWORK_MAC, entry.unique_id)}
        if entry.unique_id
        else UNDEFINED,
        identifiers={(DOMAIN, entry.entry_id)},
        manufacturer=MANUFACTURER,
        model="WMS WebControl pro",
        configuration_url=f"http://{hub.host}/system",
    )

    poller = WmsStatusPoller(hass, entry, hub)
    try:
        await hub.refresh()
        # One status request per destination, the entities take over the
        # results from poller.last_outcome when they are added.
        for dest_id in hub.dests:
            await poller.async_poll(dest_id, raise_on_error=True)
    except (aiohttp.ClientError, TimeoutError) as err:
        raise ConfigEntryNotReady(f"Error while refreshing from {host}") from err

    entry.runtime_data = WebControlProData(hub, poller)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    poller.async_start()
    entry.async_on_unload(poller.async_stop)


async def async_unload_entry(
    hass: HomeAssistant, entry: WebControlProConfigEntry
) -> None:
    """Unload a config entry."""
    await hass.config_entries.async_unload_platforms_or_raise(entry, PLATFORMS)


async def async_remove_entry(
    hass: HomeAssistant, entry: WebControlProConfigEntry
) -> None:
    """Remove a config entry."""
    config_dir = _build_storage_config_dir(hass, entry)
    if config_dir.is_dir():
        await hass.async_add_executor_job(shutil.rmtree, config_dir)
