"""The integration's actions: log_diaper and undo."""

from __future__ import annotations

import voluptuous as vol
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr

from .const import DOMAIN, SERVICE_LOG_DIAPER, SERVICE_UNDO
from .coordinator import BabyTrackerConfigEntry, BabyTrackerCoordinator
from .writes import DIAPER_STATUS, async_log_diaper, async_undo

ATTR_BABY = "baby"
ATTR_TYPE = "type"
ATTR_TIME = "time"
ATTR_NOTE = "note"
ATTR_CONFIG_ENTRY_ID = "config_entry_id"
ATTR_OBJECT_ID = "object_id"

LOG_DIAPER_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_BABY): cv.string,
        vol.Required(ATTR_TYPE): vol.In(list(DIAPER_STATUS)),
        vol.Optional(ATTR_TIME): cv.datetime,
        vol.Optional(ATTR_NOTE, default=""): cv.string,
    }
)

UNDO_SCHEMA = vol.All(
    vol.Schema(
        {
            vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string,
            vol.Optional(ATTR_BABY): cv.string,
            vol.Optional(ATTR_OBJECT_ID): cv.string,
        }
    ),
    cv.has_at_least_one_key(ATTR_CONFIG_ENTRY_ID, ATTR_BABY),
)


def _validation_error(key: str) -> ServiceValidationError:
    return ServiceValidationError(translation_domain=DOMAIN, translation_key=key)


def _loaded(entry: BabyTrackerConfigEntry | None) -> BabyTrackerCoordinator:
    if entry is None or entry.domain != DOMAIN:
        raise _validation_error("entry_not_found")
    if entry.state is not ConfigEntryState.LOADED:
        raise _validation_error("entry_not_loaded")
    return entry.runtime_data


def _from_baby_device(hass: HomeAssistant, device_id: str) -> tuple[BabyTrackerCoordinator, str]:
    """The coordinator that owns a baby's device, and the baby's ID."""
    device = dr.async_get(hass).async_get(device_id)
    if device is None:
        raise _validation_error("baby_not_found")
    baby_id = next((ident for domain, ident in device.identifiers if domain == DOMAIN), None)
    if baby_id is None:
        raise _validation_error("baby_not_found")
    for entry_id in device.config_entries:
        entry = hass.config_entries.async_get_entry(entry_id)
        if entry is not None and entry.domain == DOMAIN:
            return _loaded(entry), baby_id
    raise _validation_error("baby_not_found")


async def _log_diaper(call: ServiceCall) -> None:
    coordinator, baby_id = _from_baby_device(call.hass, call.data[ATTR_BABY])
    await async_log_diaper(
        coordinator,
        baby_id,
        call.data[ATTR_TYPE],
        time=call.data.get(ATTR_TIME),
        note=call.data[ATTR_NOTE],
    )


async def _undo(call: ServiceCall) -> None:
    baby_id: str | None = None
    if ATTR_BABY in call.data:
        coordinator, baby_id = _from_baby_device(call.hass, call.data[ATTR_BABY])
    else:
        entry = call.hass.config_entries.async_get_entry(call.data[ATTR_CONFIG_ENTRY_ID])
        coordinator = _loaded(entry)
    await async_undo(coordinator, object_id=call.data.get(ATTR_OBJECT_ID), baby_id=baby_id)


def async_register_services(hass: HomeAssistant) -> None:
    """Register the actions once for the integration, whatever entries are set up."""
    hass.services.async_register(DOMAIN, SERVICE_LOG_DIAPER, _log_diaper, schema=LOG_DIAPER_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_UNDO, _undo, schema=UNDO_SCHEMA)
