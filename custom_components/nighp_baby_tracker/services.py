"""The integration's actions: the log_* actions, the timer actions, and undo."""

from __future__ import annotations

import uuid

import voluptuous as vol
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr

from .const import (
    DOMAIN,
    SERVICE_LOG_BOTTLE,
    SERVICE_LOG_DIAPER,
    SERVICE_LOG_NURSING,
    SERVICE_LOG_PUMP,
    SERVICE_LOG_SLEEP,
    SERVICE_START_NURSING,
    SERVICE_START_SLEEP,
    SERVICE_STOP_NURSING,
    SERVICE_STOP_SLEEP,
    SERVICE_SWITCH_NURSING_SIDE,
    SERVICE_UNDO,
)
from .coordinator import BabyTrackerConfigEntry, BabyTrackerCoordinator
from .writes import (
    BOTTLE_KINDS,
    DIAPER_STATUS,
    FINISH_SIDES,
    MILLILITERS,
    OUNCES,
    async_log_bottle,
    async_log_diaper,
    async_log_nursing,
    async_log_pump,
    async_log_sleep,
    async_start_nursing,
    async_start_sleep,
    async_stop_nursing,
    async_stop_sleep,
    async_switch_nursing_side,
    async_undo,
    validation_error,
)

ATTR_BABY = "baby"
ATTR_TYPE = "type"
ATTR_TIME = "time"
ATTR_NOTE = "note"
ATTR_CONFIG_ENTRY_ID = "config_entry_id"
ATTR_OBJECT_ID = "object_id"
ATTR_KIND = "kind"
ATTR_AMOUNT = "amount"
ATTR_UNIT = "unit"
ATTR_LEFT_AMOUNT = "left_amount"
ATTR_RIGHT_AMOUNT = "right_amount"
ATTR_LEFT_MINUTES = "left_minutes"
ATTR_RIGHT_MINUTES = "right_minutes"
ATTR_FINISH_SIDE = "finish_side"
ATTR_END_TIME = "end_time"
ATTR_START = "start"
ATTR_END = "end"
ATTR_SIDE = "side"


def _uuid_upper(value: object) -> str:
    """A UUID, in the uppercase form the app uses for record IDs."""
    try:
        return str(uuid.UUID(str(value))).upper()
    except ValueError as err:
        raise vol.Invalid("must be a UUID") from err


LOG_DIAPER_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_BABY): cv.string,
        vol.Required(ATTR_TYPE): vol.In(list(DIAPER_STATUS)),
        vol.Optional(ATTR_TIME): cv.datetime,
        vol.Optional(ATTR_NOTE, default=""): cv.string,
        vol.Optional(ATTR_OBJECT_ID): _uuid_upper,
    }
)

# Amounts are checked in writes.py, so a bad one gets a translated message.
_UNIT = vol.In([OUNCES, MILLILITERS])


def _whole_minutes(value: object) -> int:
    """Whole minutes: 12 or 12.0 pass, 2.5 and text don't."""
    if isinstance(value, bool) or not isinstance(value, int | float) or int(value) != value:
        raise vol.Invalid("must be a whole number of minutes")
    if value < 0:
        raise vol.Invalid("can't be negative")
    return int(value)


_COMMON = {
    vol.Required(ATTR_BABY): cv.string,
    vol.Optional(ATTR_NOTE, default=""): cv.string,
    vol.Optional(ATTR_OBJECT_ID): _uuid_upper,
}

LOG_BOTTLE_SCHEMA = vol.Schema(
    {
        **_COMMON,
        vol.Required(ATTR_KIND): vol.In(list(BOTTLE_KINDS)),
        vol.Required(ATTR_AMOUNT): vol.Coerce(float),
        vol.Optional(ATTR_UNIT): _UNIT,
        vol.Optional(ATTR_TIME): cv.datetime,
    }
)

LOG_NURSING_SCHEMA = vol.Schema(
    {
        **_COMMON,
        vol.Optional(ATTR_LEFT_MINUTES, default=0): _whole_minutes,
        vol.Optional(ATTR_RIGHT_MINUTES, default=0): _whole_minutes,
        vol.Optional(ATTR_FINISH_SIDE): vol.In(list(FINISH_SIDES)),
        vol.Optional(ATTR_END_TIME): cv.datetime,
    }
)

LOG_SLEEP_SCHEMA = vol.Schema(
    {
        **_COMMON,
        vol.Required(ATTR_START): cv.datetime,
        vol.Required(ATTR_END): cv.datetime,
    }
)

LOG_PUMP_SCHEMA = vol.Schema(
    {
        **_COMMON,
        vol.Optional(ATTR_LEFT_AMOUNT): vol.Coerce(float),
        vol.Optional(ATTR_RIGHT_AMOUNT): vol.Coerce(float),
        vol.Optional(ATTR_UNIT): _UNIT,
        vol.Optional(ATTR_LEFT_MINUTES, default=0): _whole_minutes,
        vol.Optional(ATTR_RIGHT_MINUTES, default=0): _whole_minutes,
        vol.Optional(ATTR_FINISH_SIDE): vol.In(list(FINISH_SIDES)),
        vol.Optional(ATTR_TIME): cv.datetime,
    }
)

START_SLEEP_SCHEMA = vol.Schema(
    {
        **_COMMON,
        vol.Optional(ATTR_START): cv.datetime,
    }
)

STOP_SLEEP_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_BABY): cv.string,
        vol.Optional(ATTR_END): cv.datetime,
    }
)

START_NURSING_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_BABY): cv.string,
        vol.Required(ATTR_SIDE): vol.In(list(FINISH_SIDES)),
    }
)

BABY_ONLY_SCHEMA = vol.Schema({vol.Required(ATTR_BABY): cv.string})

STOP_NURSING_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_BABY): cv.string,
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


def _loaded(entry: BabyTrackerConfigEntry | None) -> BabyTrackerCoordinator:
    if entry is None or entry.domain != DOMAIN:
        raise validation_error("entry_not_found")
    if entry.state is not ConfigEntryState.LOADED:
        raise validation_error("entry_not_loaded")
    return entry.runtime_data


def _from_baby_device(hass: HomeAssistant, device_id: str) -> tuple[BabyTrackerCoordinator, str]:
    """The coordinator that owns a baby's device, and the baby's ID."""
    device = dr.async_get(hass).async_get(device_id)
    if device is None:
        raise validation_error("baby_not_found")
    baby_id = next((ident for domain, ident in device.identifiers if domain == DOMAIN), None)
    if baby_id is None:
        raise validation_error("baby_not_found")
    loaded: list[BabyTrackerConfigEntry] = []
    found_any = False
    for entry_id in device.config_entries:
        entry = hass.config_entries.async_get_entry(entry_id)
        if entry is None or entry.domain != DOMAIN:
            continue
        found_any = True
        if entry.state is ConfigEntryState.LOADED:
            loaded.append(entry)
    if loaded:
        return loaded[0].runtime_data, baby_id
    raise validation_error("entry_not_loaded" if found_any else "baby_not_found")


async def _log_diaper(call: ServiceCall) -> None:
    coordinator, baby_id = _from_baby_device(call.hass, call.data[ATTR_BABY])
    await async_log_diaper(
        coordinator,
        baby_id,
        call.data[ATTR_TYPE],
        time=call.data.get(ATTR_TIME),
        note=call.data[ATTR_NOTE],
        object_id=call.data.get(ATTR_OBJECT_ID),
    )


async def _log_bottle(call: ServiceCall) -> None:
    coordinator, baby_id = _from_baby_device(call.hass, call.data[ATTR_BABY])
    await async_log_bottle(
        coordinator,
        baby_id,
        call.data[ATTR_KIND],
        call.data[ATTR_AMOUNT],
        unit=call.data.get(ATTR_UNIT),
        time=call.data.get(ATTR_TIME),
        note=call.data[ATTR_NOTE],
        object_id=call.data.get(ATTR_OBJECT_ID),
    )


async def _log_nursing(call: ServiceCall) -> None:
    coordinator, baby_id = _from_baby_device(call.hass, call.data[ATTR_BABY])
    await async_log_nursing(
        coordinator,
        baby_id,
        left_minutes=call.data[ATTR_LEFT_MINUTES],
        right_minutes=call.data[ATTR_RIGHT_MINUTES],
        finish_side=call.data.get(ATTR_FINISH_SIDE),
        end_time=call.data.get(ATTR_END_TIME),
        note=call.data[ATTR_NOTE],
        object_id=call.data.get(ATTR_OBJECT_ID),
    )


async def _log_sleep(call: ServiceCall) -> None:
    coordinator, baby_id = _from_baby_device(call.hass, call.data[ATTR_BABY])
    await async_log_sleep(
        coordinator,
        baby_id,
        start=call.data[ATTR_START],
        end=call.data[ATTR_END],
        note=call.data[ATTR_NOTE],
        object_id=call.data.get(ATTR_OBJECT_ID),
    )


async def _log_pump(call: ServiceCall) -> None:
    coordinator, baby_id = _from_baby_device(call.hass, call.data[ATTR_BABY])
    await async_log_pump(
        coordinator,
        baby_id,
        left_amount=call.data.get(ATTR_LEFT_AMOUNT),
        right_amount=call.data.get(ATTR_RIGHT_AMOUNT),
        unit=call.data.get(ATTR_UNIT),
        left_minutes=call.data[ATTR_LEFT_MINUTES],
        right_minutes=call.data[ATTR_RIGHT_MINUTES],
        finish_side=call.data.get(ATTR_FINISH_SIDE),
        time=call.data.get(ATTR_TIME),
        note=call.data[ATTR_NOTE],
        object_id=call.data.get(ATTR_OBJECT_ID),
    )


async def _start_sleep(call: ServiceCall) -> None:
    coordinator, baby_id = _from_baby_device(call.hass, call.data[ATTR_BABY])
    await async_start_sleep(
        coordinator,
        baby_id,
        start=call.data.get(ATTR_START),
        note=call.data[ATTR_NOTE],
        object_id=call.data.get(ATTR_OBJECT_ID),
    )


async def _stop_sleep(call: ServiceCall) -> None:
    coordinator, baby_id = _from_baby_device(call.hass, call.data[ATTR_BABY])
    await async_stop_sleep(coordinator, baby_id, end=call.data.get(ATTR_END))


async def _start_nursing(call: ServiceCall) -> None:
    coordinator, baby_id = _from_baby_device(call.hass, call.data[ATTR_BABY])
    await async_start_nursing(coordinator, baby_id, call.data[ATTR_SIDE])


async def _switch_nursing_side(call: ServiceCall) -> None:
    coordinator, baby_id = _from_baby_device(call.hass, call.data[ATTR_BABY])
    await async_switch_nursing_side(coordinator, baby_id)


async def _stop_nursing(call: ServiceCall) -> None:
    coordinator, baby_id = _from_baby_device(call.hass, call.data[ATTR_BABY])
    await async_stop_nursing(coordinator, baby_id, note=call.data[ATTR_NOTE])


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
    hass.services.async_register(DOMAIN, SERVICE_LOG_BOTTLE, _log_bottle, schema=LOG_BOTTLE_SCHEMA)
    hass.services.async_register(
        DOMAIN, SERVICE_LOG_NURSING, _log_nursing, schema=LOG_NURSING_SCHEMA
    )
    hass.services.async_register(DOMAIN, SERVICE_LOG_SLEEP, _log_sleep, schema=LOG_SLEEP_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_LOG_PUMP, _log_pump, schema=LOG_PUMP_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_UNDO, _undo, schema=UNDO_SCHEMA)
    hass.services.async_register(
        DOMAIN, SERVICE_START_SLEEP, _start_sleep, schema=START_SLEEP_SCHEMA
    )
    hass.services.async_register(DOMAIN, SERVICE_STOP_SLEEP, _stop_sleep, schema=STOP_SLEEP_SCHEMA)
    hass.services.async_register(
        DOMAIN, SERVICE_START_NURSING, _start_nursing, schema=START_NURSING_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_SWITCH_NURSING_SIDE, _switch_nursing_side, schema=BABY_ONLY_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_STOP_NURSING, _stop_nursing, schema=STOP_NURSING_SCHEMA
    )
