"""The integration's actions: the log_* actions, the timer actions, and undo."""

import uuid
from typing import Any

import probatio
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers import service

from .bottle import BOTTLE_KINDS, MILLILITERS, OUNCES
from .const import (
    DOMAIN,
    SERVICE_DISCARD_NURSING,
    SERVICE_LOG_BOTTLE,
    SERVICE_LOG_DIAPER,
    SERVICE_LOG_NURSING,
    SERVICE_LOG_PUMP,
    SERVICE_LOG_SLEEP,
    SERVICE_START_NURSING,
    SERVICE_START_PUMP,
    SERVICE_START_SLEEP,
    SERVICE_STOP_NURSING,
    SERVICE_STOP_PUMP,
    SERVICE_STOP_SLEEP,
    SERVICE_SWITCH_NURSING_SIDE,
    SERVICE_UNDO,
    is_account_identifier,
)
from .coordinator import BabyTrackerCoordinator
from .writes import (
    DIAPER_STATUS,
    FINISH_SIDES,
    async_discard_nursing,
    async_log_bottle,
    async_log_diaper,
    async_log_nursing,
    async_log_pump,
    async_log_sleep,
    async_start_nursing,
    async_start_pump,
    async_start_sleep,
    async_stop_nursing,
    async_stop_pump,
    async_stop_sleep,
    async_switch_nursing_side,
    async_undo,
    validation_error,
)

ATTR_DEVICE_ID = "device_id"
# Deprecated aliases, kept for one release so existing automations keep working.
ATTR_BABY = "baby"
ATTR_DEVICE = "device"
ATTR_TYPE = "type"
ATTR_TIME = "time"
ATTR_NOTE = "note"
ATTR_CONFIG_ENTRY_ID = "config_entry_id"
ATTR_OBJECT_ID = "object_id"
ATTR_KIND = "kind"
ATTR_AMOUNT = "amount"
ATTR_UNIT = "unit"
ATTR_MINUTES = "minutes"
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
    """Return a UUID in the uppercase form the app uses for record IDs."""
    try:
        return str(uuid.UUID(str(value))).upper()
    except ValueError as err:
        raise probatio.Invalid("must be a UUID") from err


# Every baby action names its baby with ``device_id``. ``baby`` is the deprecated alias.
_BABY_TARGET = {
    probatio.Optional(ATTR_DEVICE_ID): cv.string,
    probatio.Optional(ATTR_BABY): cv.string,
}


def _baby_schema(fields: dict[Any, Any]) -> probatio.All[Any]:
    """Return the schema of an action that targets one baby."""
    return probatio.All(
        probatio.Schema({**_BABY_TARGET, **fields}),
        cv.has_at_least_one_key(ATTR_DEVICE_ID, ATTR_BABY),
        cv.has_at_most_one_key(ATTR_DEVICE_ID, ATTR_BABY),
    )


LOG_DIAPER_SCHEMA = _baby_schema(
    {
        probatio.Required(ATTR_TYPE): probatio.In(list(DIAPER_STATUS)),
        probatio.Optional(ATTR_TIME): cv.datetime,
        probatio.Optional(ATTR_NOTE, default=""): cv.string,
        probatio.Optional(ATTR_OBJECT_ID): _uuid_upper,
    }
)

# Amounts are checked in writes.py, so a bad one gets a translated message.
_UNIT = probatio.In([OUNCES, MILLILITERS])


def _whole_minutes(value: object) -> int:
    """Whole minutes: 12 or 12.0 pass, 2.5 and text don't."""
    if isinstance(value, bool) or not isinstance(value, int | float) or int(value) != value:
        raise probatio.Invalid("must be a whole number of minutes")
    if value < 0:
        raise probatio.Invalid("can't be negative")
    return int(value)


_COMMON = {
    probatio.Optional(ATTR_NOTE, default=""): cv.string,
    probatio.Optional(ATTR_OBJECT_ID): _uuid_upper,
}

LOG_BOTTLE_SCHEMA = _baby_schema(
    {
        **_COMMON,
        probatio.Required(ATTR_KIND): probatio.In(list(BOTTLE_KINDS)),
        probatio.Required(ATTR_AMOUNT): probatio.Coerce(float),
        probatio.Optional(ATTR_UNIT): _UNIT,
        probatio.Optional(ATTR_TIME): cv.datetime,
    }
)

LOG_NURSING_SCHEMA = _baby_schema(
    {
        **_COMMON,
        probatio.Optional(ATTR_LEFT_MINUTES, default=0): _whole_minutes,
        probatio.Optional(ATTR_RIGHT_MINUTES, default=0): _whole_minutes,
        probatio.Optional(ATTR_FINISH_SIDE): probatio.In(list(FINISH_SIDES)),
        probatio.Optional(ATTR_END_TIME): cv.datetime,
    }
)

LOG_SLEEP_SCHEMA = _baby_schema(
    {
        **_COMMON,
        probatio.Required(ATTR_START): cv.datetime,
        probatio.Required(ATTR_END): cv.datetime,
    }
)

# A pump belongs to the account, not a baby, so it names an account. ``device`` and ``baby``
# (an account's or a baby's device) are the deprecated ways to name one.
_PUMP_COMMON = {
    probatio.Optional(ATTR_NOTE, default=""): cv.string,
    probatio.Optional(ATTR_OBJECT_ID): _uuid_upper,
}

LOG_PUMP_SCHEMA = probatio.All(
    probatio.Schema(
        {
            **_PUMP_COMMON,
            probatio.Optional(ATTR_CONFIG_ENTRY_ID): cv.string,
            probatio.Optional(ATTR_DEVICE): cv.string,
            probatio.Optional(ATTR_BABY): cv.string,
            # The total, or the sides (checked in writes.py): so none has a default, and an
            # explicit 0 minutes alongside ``minutes`` is a conflict.
            probatio.Optional(ATTR_AMOUNT): probatio.Coerce(float),
            probatio.Optional(ATTR_MINUTES): _whole_minutes,
            probatio.Optional(ATTR_LEFT_AMOUNT): probatio.Coerce(float),
            probatio.Optional(ATTR_RIGHT_AMOUNT): probatio.Coerce(float),
            probatio.Optional(ATTR_UNIT): _UNIT,
            probatio.Optional(ATTR_LEFT_MINUTES): _whole_minutes,
            probatio.Optional(ATTR_RIGHT_MINUTES): _whole_minutes,
            probatio.Optional(ATTR_FINISH_SIDE): probatio.In(list(FINISH_SIDES)),
            probatio.Optional(ATTR_TIME): cv.datetime,
        }
    ),
    cv.has_at_least_one_key(ATTR_CONFIG_ENTRY_ID, ATTR_DEVICE, ATTR_BABY),
    cv.has_at_most_one_key(ATTR_CONFIG_ENTRY_ID, ATTR_DEVICE, ATTR_BABY),
)

# The pump timer belongs to the account, so these name it by config entry.
START_PUMP_SCHEMA = probatio.Schema({probatio.Required(ATTR_CONFIG_ENTRY_ID): cv.string})

STOP_PUMP_SCHEMA = probatio.Schema(
    {
        probatio.Required(ATTR_CONFIG_ENTRY_ID): cv.string,
        probatio.Optional(ATTR_AMOUNT): probatio.Coerce(float),
        probatio.Optional(ATTR_UNIT): _UNIT,
        probatio.Optional(ATTR_NOTE, default=""): cv.string,
    }
)

START_SLEEP_SCHEMA = _baby_schema(
    {
        **_COMMON,
        probatio.Optional(ATTR_START): cv.datetime,
    }
)

STOP_SLEEP_SCHEMA = _baby_schema({probatio.Optional(ATTR_END): cv.datetime})

START_NURSING_SCHEMA = _baby_schema({probatio.Required(ATTR_SIDE): probatio.In(list(FINISH_SIDES))})

BABY_ONLY_SCHEMA = _baby_schema({})

STOP_NURSING_SCHEMA = _baby_schema({probatio.Optional(ATTR_NOTE, default=""): cv.string})

# A baby's undo takes its device. An account's undo takes its config entry.
UNDO_SCHEMA = probatio.All(
    probatio.Schema(
        {
            probatio.Optional(ATTR_CONFIG_ENTRY_ID): cv.string,
            probatio.Optional(ATTR_DEVICE_ID): cv.string,
            probatio.Optional(ATTR_BABY): cv.string,
            probatio.Optional(ATTR_OBJECT_ID): cv.string,
        }
    ),
    cv.has_at_least_one_key(ATTR_CONFIG_ENTRY_ID, ATTR_DEVICE_ID, ATTR_BABY),
)


def _warn_deprecated(call: ServiceCall, field: str, replacement: str) -> None:
    """Raise a repair issue that says an old automation should use ``replacement`` instead."""
    ir.async_create_issue(
        call.hass,
        DOMAIN,
        f"deprecated_{call.service}_{field}",
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key="deprecated_action_field",
        translation_placeholders={
            "action": call.service,
            "field": field,
            "replacement": replacement,
        },
    )


def _entry_coordinator(hass: HomeAssistant, entry_id: str) -> BabyTrackerCoordinator:
    """Return the coordinator of a loaded Baby Tracker config entry."""
    coordinator: BabyTrackerCoordinator = service.async_get_config_entry(
        hass, DOMAIN, entry_id
    ).runtime_data
    return coordinator


def _from_device(hass: HomeAssistant, device_id: str) -> tuple[BabyTrackerCoordinator, str, bool]:
    """Return the coordinator that owns one of the integration's devices.

    Also returns the device's identifier and whether it is an account's device (rather than a
    baby's).
    """
    device = dr.async_get(hass).async_get(device_id)
    if device is None:
        raise validation_error("baby_not_found")
    identifier = next((ident for domain, ident in device.identifiers if domain == DOMAIN), None)
    if identifier is None:
        raise validation_error("baby_not_found")
    entry = hass.config_entries.async_get_entry(device.config_entry_id)
    if entry is None or entry.domain != DOMAIN:
        raise validation_error("baby_not_found")
    return _entry_coordinator(hass, entry.entry_id), identifier, is_account_identifier(identifier)


def _from_baby_device(hass: HomeAssistant, device_id: str) -> tuple[BabyTrackerCoordinator, str]:
    """Return the coordinator that owns a baby's device, and the baby's ID.

    An account's device isn't a baby.
    """
    coordinator, baby_id, is_account = _from_device(hass, device_id)
    if is_account:
        raise validation_error("baby_not_found")
    return coordinator, baby_id


def _baby(call: ServiceCall) -> tuple[BabyTrackerCoordinator, str]:
    """Return the coordinator and ID of the baby an action names, by ``device_id`` or ``baby``."""
    if ATTR_DEVICE_ID in call.data:
        return _from_baby_device(call.hass, call.data[ATTR_DEVICE_ID])
    _warn_deprecated(call, ATTR_BABY, ATTR_DEVICE_ID)
    return _from_baby_device(call.hass, call.data[ATTR_BABY])


def _account_coordinator(call: ServiceCall) -> BabyTrackerCoordinator:
    """Return the account a pump goes to.

    That is the named config entry. The deprecated ``device`` and ``baby`` name the entry that
    owns a device (a baby's or the account's).
    """
    if ATTR_CONFIG_ENTRY_ID in call.data:
        return _entry_coordinator(call.hass, call.data[ATTR_CONFIG_ENTRY_ID])
    field = ATTR_DEVICE if ATTR_DEVICE in call.data else ATTR_BABY
    _warn_deprecated(call, field, ATTR_CONFIG_ENTRY_ID)
    return _from_device(call.hass, call.data[field])[0]


async def _log_diaper(call: ServiceCall) -> None:
    coordinator, baby_id = _baby(call)
    await async_log_diaper(
        coordinator,
        baby_id,
        call.data[ATTR_TYPE],
        time=call.data.get(ATTR_TIME),
        note=call.data[ATTR_NOTE],
        object_id=call.data.get(ATTR_OBJECT_ID),
    )


async def _log_bottle(call: ServiceCall) -> None:
    coordinator, baby_id = _baby(call)
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
    coordinator, baby_id = _baby(call)
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
    coordinator, baby_id = _baby(call)
    await async_log_sleep(
        coordinator,
        baby_id,
        start=call.data[ATTR_START],
        end=call.data[ATTR_END],
        note=call.data[ATTR_NOTE],
        object_id=call.data.get(ATTR_OBJECT_ID),
    )


async def _log_pump(call: ServiceCall) -> None:
    await async_log_pump(
        _account_coordinator(call),
        amount=call.data.get(ATTR_AMOUNT),
        minutes=call.data.get(ATTR_MINUTES),
        left_amount=call.data.get(ATTR_LEFT_AMOUNT),
        right_amount=call.data.get(ATTR_RIGHT_AMOUNT),
        unit=call.data.get(ATTR_UNIT),
        left_minutes=call.data.get(ATTR_LEFT_MINUTES),
        right_minutes=call.data.get(ATTR_RIGHT_MINUTES),
        finish_side=call.data.get(ATTR_FINISH_SIDE),
        time=call.data.get(ATTR_TIME),
        note=call.data[ATTR_NOTE],
        object_id=call.data.get(ATTR_OBJECT_ID),
    )


async def _start_pump(call: ServiceCall) -> None:
    await async_start_pump(_entry_coordinator(call.hass, call.data[ATTR_CONFIG_ENTRY_ID]))


async def _stop_pump(call: ServiceCall) -> None:
    await async_stop_pump(
        _entry_coordinator(call.hass, call.data[ATTR_CONFIG_ENTRY_ID]),
        amount=call.data.get(ATTR_AMOUNT),
        unit=call.data.get(ATTR_UNIT),
        note=call.data[ATTR_NOTE],
    )


async def _start_sleep(call: ServiceCall) -> None:
    coordinator, baby_id = _baby(call)
    await async_start_sleep(
        coordinator,
        baby_id,
        start=call.data.get(ATTR_START),
        note=call.data[ATTR_NOTE],
        object_id=call.data.get(ATTR_OBJECT_ID),
    )


async def _stop_sleep(call: ServiceCall) -> None:
    coordinator, baby_id = _baby(call)
    await async_stop_sleep(coordinator, baby_id, end=call.data.get(ATTR_END))


async def _start_nursing(call: ServiceCall) -> None:
    coordinator, baby_id = _baby(call)
    await async_start_nursing(coordinator, baby_id, call.data[ATTR_SIDE])


async def _switch_nursing_side(call: ServiceCall) -> None:
    coordinator, baby_id = _baby(call)
    await async_switch_nursing_side(coordinator, baby_id)


async def _stop_nursing(call: ServiceCall) -> None:
    coordinator, baby_id = _baby(call)
    await async_stop_nursing(coordinator, baby_id, note=call.data[ATTR_NOTE])


async def _discard_nursing(call: ServiceCall) -> None:
    coordinator, baby_id = _baby(call)
    await async_discard_nursing(coordinator, baby_id)


async def _undo(call: ServiceCall) -> None:
    baby_id: str | None = None
    if ATTR_DEVICE_ID in call.data or ATTR_BABY in call.data:
        coordinator, baby_id = _baby(call)
    else:
        coordinator = _entry_coordinator(call.hass, call.data[ATTR_CONFIG_ENTRY_ID])
    await async_undo(coordinator, object_id=call.data.get(ATTR_OBJECT_ID), baby_id=baby_id)


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Register the actions once for the integration, whatever entries are set up."""
    hass.services.async_register(DOMAIN, SERVICE_LOG_DIAPER, _log_diaper, schema=LOG_DIAPER_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_LOG_BOTTLE, _log_bottle, schema=LOG_BOTTLE_SCHEMA)
    hass.services.async_register(
        DOMAIN, SERVICE_LOG_NURSING, _log_nursing, schema=LOG_NURSING_SCHEMA
    )
    hass.services.async_register(DOMAIN, SERVICE_LOG_SLEEP, _log_sleep, schema=LOG_SLEEP_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_LOG_PUMP, _log_pump, schema=LOG_PUMP_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_START_PUMP, _start_pump, schema=START_PUMP_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_STOP_PUMP, _stop_pump, schema=STOP_PUMP_SCHEMA)
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
    hass.services.async_register(
        DOMAIN, SERVICE_DISCARD_NURSING, _discard_nursing, schema=BABY_ONLY_SCHEMA
    )
