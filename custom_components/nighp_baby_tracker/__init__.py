"""Baby Tracker (Nighp)."""

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.storage import Store
from homeassistant.helpers.typing import ConfigType

from .const import (
    CONF_DEVICE_UUID,
    CONFIG_ENTRY_MINOR_VERSION,
    CONFIG_ENTRY_VERSION,
    DEVICE_NAME,
    DOMAIN,
    STORAGE_VERSION,
    is_account_identifier,
)
from .coordinator import (
    BabyTrackerConfigEntry,
    BabyTrackerCoordinator,
    storage_key,
)
from .pybabytracker import BabyTrackerClient
from .services import async_setup_services
from .timer_state import nursing_storage_key, pump_storage_key
from .writes import forget_gap

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.SENSOR,
    Platform.SWITCH,
]

# HACS-only: the legacy button cleanup. These are the ends of the unique IDs (``<baby_id>_<key>``)
# of the Sleep and Nurse left/right buttons, which the timer switches cover. Matching is by suffix,
# so no button may have a key that ends in one of these (a ``log_sleep`` button would be deleted
# on setup).
REMOVED_BUTTONS = ("_sleep", "_nurse_left", "_nurse_right")

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the actions, which belong to the integration rather than to one entry."""
    async_setup_services(hass)
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
    # Runs on unload and also when setup fails, so the client is never left open.
    entry.async_on_unload(client.close)
    coordinator = BabyTrackerCoordinator(hass, entry, client, session)
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = coordinator
    _remove_old_timer_buttons(hass, entry)  # HACS-only: the legacy button cleanup
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


def _remove_old_timer_buttons(hass: HomeAssistant, entry: BabyTrackerConfigEntry) -> None:
    """Drop the Sleep and Nurse left/right buttons from the entity registry.

    HACS-only: the legacy button cleanup. A registry entry with no entity behind it would show
    as an unavailable button. Every other button is left alone, and with none left this does
    nothing, so it is safe on every setup.
    """
    registry = er.async_get(hass)
    for entity in er.async_entries_for_config_entry(registry, entry.entry_id):
        if (
            entity.domain == Platform.BUTTON
            and entity.platform == DOMAIN
            and entity.unique_id.endswith(REMOVED_BUTTONS)
        ):
            registry.async_remove(entity.entity_id)


async def async_migrate_entry(hass: HomeAssistant, entry: BabyTrackerConfigEntry) -> bool:
    """Bring an older entry up to date, once.

    HACS-only: the config entry migration. Minor version 2 drops the per-baby last-pump
    entities. Doing it twice is harmless.
    """
    if entry.version > CONFIG_ENTRY_VERSION:
        # The entry's layout comes from a newer release than this one: don't guess at it.
        return False
    if entry.minor_version < 2:
        _remove_per_baby_last_pumps(hass, entry)
        hass.config_entries.async_update_entry(
            entry, version=CONFIG_ENTRY_VERSION, minor_version=CONFIG_ENTRY_MINOR_VERSION
        )
    return True


def _remove_per_baby_last_pumps(hass: HomeAssistant, entry: BabyTrackerConfigEntry) -> None:
    """Drop the per-baby last-pump entities (``<baby_id>_last_pump``).

    HACS-only: the legacy pump cleanup, run by the config entry migration. A pump belongs to the
    account, so the sensor lives on the account's device (``<entry_id>_last_pump``). Nothing else
    is touched, and it does nothing once they're gone.
    """
    registry = er.async_get(hass)
    account_unique_id = f"{entry.entry_id}_last_pump"
    for entity in er.async_entries_for_config_entry(registry, entry.entry_id):
        if (
            entity.domain == Platform.SENSOR
            and entity.platform == DOMAIN
            and entity.unique_id.endswith("_last_pump")
            and entity.unique_id != account_unique_id
        ):
            registry.async_remove(entity.entity_id)


async def async_unload_entry(hass: HomeAssistant, entry: BabyTrackerConfigEntry) -> bool:
    """Unload the platforms. The coordinator stops and the client closes through the entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: BabyTrackerConfigEntry) -> None:
    """Delete the saved sync state, the saved timers, and any repair about this entry."""
    forget_gap(hass, entry.entry_id)
    await Store(hass, STORAGE_VERSION, storage_key(entry.entry_id)).async_remove()
    await Store(hass, STORAGE_VERSION, nursing_storage_key(entry.entry_id)).async_remove()
    await Store(hass, STORAGE_VERSION, pump_storage_key(entry.entry_id)).async_remove()


async def async_remove_config_entry_device(
    hass: HomeAssistant, entry: BabyTrackerConfigEntry, device_entry: dr.DeviceEntry
) -> bool:
    """Let a baby that has left the account be deleted from the device page.

    A baby still in the account's data can't be removed, and neither can the account's own
    device: the next sync would bring the baby back, and the account's device holds its pump
    sensor. With the entry not loaded there's no data to go by, so nothing can be removed.
    """
    if entry.state is not ConfigEntryState.LOADED:
        return False
    identifiers = {value for domain, value in device_entry.identifiers if domain == DOMAIN}
    if any(is_account_identifier(value) for value in identifiers):
        return False
    return not any(value in entry.runtime_data.data.babies for value in identifiers)
