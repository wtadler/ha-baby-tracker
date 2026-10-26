"""Baby Tracker (Nighp)."""

from __future__ import annotations

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.storage import Store

from .const import CONF_DEVICE_UUID, CONF_EMAIL, CONF_PASSWORD, DEVICE_NAME, STORAGE_VERSION
from .coordinator import (
    BabyTrackerConfigEntry,
    BabyTrackerCoordinator,
    scan_interval,
    storage_key,
)
from .pybabytracker import BabyTrackerClient

PLATFORMS: list[Platform] = [Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: BabyTrackerConfigEntry) -> bool:
    """Sign in, load or backfill the records, and set up the sensors."""
    # The service authenticates by cookie, so this client gets a session of its own.
    session = async_create_clientsession(hass)
    client = BabyTrackerClient(
        entry.data[CONF_EMAIL],
        entry.data[CONF_PASSWORD],
        entry.data[CONF_DEVICE_UUID],
        DEVICE_NAME,
        session=session,
    )
    coordinator = BabyTrackerCoordinator(hass, entry, client, session)
    try:
        await coordinator.async_config_entry_first_refresh()
    except BaseException:
        await coordinator.async_close()
        raise

    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    return True


async def _async_options_updated(hass: HomeAssistant, entry: BabyTrackerConfigEntry) -> None:
    """Apply a new sync interval without reloading the entry."""
    entry.runtime_data.update_interval = scan_interval(entry)


async def async_unload_entry(hass: HomeAssistant, entry: BabyTrackerConfigEntry) -> bool:
    """Stop syncing and free the client."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.async_close()
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: BabyTrackerConfigEntry) -> None:
    """Delete the saved sync state."""
    await Store(hass, STORAGE_VERSION, storage_key(entry.entry_id)).async_remove()
