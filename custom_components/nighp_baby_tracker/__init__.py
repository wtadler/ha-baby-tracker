"""Baby Tracker (Nighp)."""

from __future__ import annotations

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.storage import Store
from homeassistant.helpers.typing import ConfigType

from .const import (
    CONF_DEVICE_UUID,
    CONF_EMAIL,
    CONF_PASSWORD,
    DEVICE_NAME,
    DOMAIN,
    STORAGE_VERSION,
)
from .coordinator import (
    BabyTrackerConfigEntry,
    BabyTrackerCoordinator,
    nursing_storage_key,
    storage_key,
)
from .pybabytracker import BabyTrackerClient
from .services import async_register_services
from .writes import issue_id

PLATFORMS: list[Platform] = [Platform.BINARY_SENSOR, Platform.BUTTON, Platform.SENSOR]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the actions, which belong to the integration rather than to one entry."""
    async_register_services(hass)
    return True


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
    """Apply a new sync interval right away, without reloading the entry. While a timer runs,
    syncing stays at one minute and the new interval takes over when it stops."""
    coordinator = entry.runtime_data
    interval = coordinator.wanted_interval()
    if interval != coordinator.update_interval:
        coordinator.update_interval = interval
        # The setter doesn't reschedule, so refresh now: that also schedules the next one.
        await coordinator.async_request_refresh()


async def async_unload_entry(hass: HomeAssistant, entry: BabyTrackerConfigEntry) -> bool:
    """Stop syncing and free the client."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.async_close()
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: BabyTrackerConfigEntry) -> None:
    """Delete the saved sync state, the saved nursing timers, and any repair about this entry."""
    ir.async_delete_issue(hass, DOMAIN, issue_id(entry.entry_id))
    await Store(hass, STORAGE_VERSION, storage_key(entry.entry_id)).async_remove()
    await Store(hass, STORAGE_VERSION, nursing_storage_key(entry.entry_id)).async_remove()
