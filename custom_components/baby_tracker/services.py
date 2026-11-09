"""The integration's actions: the log_* actions, the timer actions, and undo."""

import uuid
from enum import StrEnum
from typing import Any

import probatio
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import issue_registry as ir  # HACS-only
from homeassistant.helpers import service

from .bottle import BOTTLE_KINDS, MILLILITERS, OUNCES  # Core PR 10
from .const import (
    DOMAIN,
    BabyTrackerService,
    is_account_identifier,
)
from .coordinator import BabyTrackerCoordinator
from .writes import (
    DIAPER_STATUS,
    FINISH_SIDES,  # Core PR 10
    async_discard_nursing,  # Core PR 18
    async_log_bottle,  # Core PR 10
    async_log_diaper,
    async_log_medicine,  # Core PR 26
    async_log_nursing,  # Core PR 10
    async_log_pump,  # Core PR 12
    async_log_sleep,  # Core PR 10
    async_start_nursing,  # Core PR 18
    async_start_pump,  # Core PR 21
    async_start_sleep,  # Core PR 16
    async_stop_nursing,  # Core PR 18
    async_stop_pump,  # Core PR 21
    async_stop_sleep,  # Core PR 16
    async_switch_nursing_side,  # Core PR 18
    async_undo,  # Core PR 13
    validation_error,
)


class BabyTrackerServiceArgument(StrEnum):
    """The names of the fields the actions take."""

    DEVICE_ID = "device_id"
    BABY = "baby"  # HACS-only: the deprecated alias for ``device_id``
    DEVICE = "device"  # HACS-only: the deprecated way to name an account's pump
    TYPE = "type"
    TIME = "time"
    NOTE = "note"
    CONFIG_ENTRY_ID = "config_entry_id"  # Core PR 12
    OBJECT_ID = "object_id"
    KIND = "kind"  # Core PR 10
    AMOUNT = "amount"  # Core PR 10
    UNIT = "unit"  # Core PR 10
    MINUTES = "minutes"  # Core PR 12
    LEFT_AMOUNT = "left_amount"  # Core PR 12
    RIGHT_AMOUNT = "right_amount"  # Core PR 12
    LEFT_MINUTES = "left_minutes"  # Core PR 10
    RIGHT_MINUTES = "right_minutes"  # Core PR 10
    FINISH_SIDE = "finish_side"  # Core PR 10
    END_TIME = "end_time"  # Core PR 10
    START = "start"  # Core PR 10
    END = "end"  # Core PR 10
    SIDE = "side"  # Core PR 18
    MEDICINE = "medicine"  # Core PR 26


def _uuid_upper(value: object) -> str:
    """Return a UUID in the uppercase form the app uses for record IDs."""
    try:
        return str(uuid.UUID(str(value))).upper()
    except ValueError as err:
        raise probatio.Invalid("must be a UUID") from err


# Every baby action names its baby with ``device_id``.
_BABY_TARGET = {
    probatio.Optional(BabyTrackerServiceArgument.DEVICE_ID): cv.string,
    probatio.Optional(BabyTrackerServiceArgument.BABY): cv.string,  # HACS-only: deprecated alias
}
_BABY_KEYS = (
    BabyTrackerServiceArgument.DEVICE_ID,
    BabyTrackerServiceArgument.BABY,  # HACS-only
)


def _baby_schema(fields: dict[Any, Any]) -> probatio.All[Any]:
    """Return the schema of an action that targets one baby."""
    return probatio.All(
        probatio.Schema({**_BABY_TARGET, **fields}),
        cv.has_at_least_one_key(*_BABY_KEYS),
        cv.has_at_most_one_key(*_BABY_KEYS),  # HACS-only
    )


LOG_DIAPER_SCHEMA = _baby_schema(
    {
        probatio.Required(BabyTrackerServiceArgument.TYPE): probatio.In(list(DIAPER_STATUS)),
        probatio.Optional(BabyTrackerServiceArgument.TIME): cv.datetime,
        probatio.Optional(BabyTrackerServiceArgument.NOTE, default=""): cv.string,
        probatio.Optional(BabyTrackerServiceArgument.OBJECT_ID): _uuid_upper,
    }
)


# Core PR 26
def _not_a_bool(value: object) -> object:
    """Refuse a bool, which would otherwise read as the amount 1 or 0."""
    if isinstance(value, bool):
        raise probatio.Invalid("an amount must be a number")
    return value


# Core PR 26
# The medicine is looked up, and the amount checked, in writes.py after the write's own sync, so a
# medicine first given on a phone minutes ago is found and every refusal is translated.
LOG_MEDICINE_SCHEMA = _baby_schema(
    {
        probatio.Required(BabyTrackerServiceArgument.MEDICINE): cv.string,
        probatio.Optional(BabyTrackerServiceArgument.AMOUNT): probatio.All(
            _not_a_bool, probatio.Coerce(float)
        ),
        probatio.Optional(BabyTrackerServiceArgument.TIME): cv.datetime,
        probatio.Optional(BabyTrackerServiceArgument.NOTE, default=""): cv.string,
        probatio.Optional(BabyTrackerServiceArgument.OBJECT_ID): _uuid_upper,
    }
)

# Core PR 10
# Amounts are checked in writes.py, so a bad one gets a translated message.
_UNIT = probatio.In([OUNCES, MILLILITERS])


# Core PR 10
def _whole_minutes(value: object) -> int:
    """Whole minutes: 12 or 12.0 pass, 2.5 and text don't."""
    if isinstance(value, bool) or not isinstance(value, int | float) or int(value) != value:
        raise probatio.Invalid("must be a whole number of minutes")
    if value < 0:
        raise probatio.Invalid("can't be negative")
    return int(value)


# Core PR 10
_COMMON = {
    probatio.Optional(BabyTrackerServiceArgument.NOTE, default=""): cv.string,
    probatio.Optional(BabyTrackerServiceArgument.OBJECT_ID): _uuid_upper,
}

# Core PR 10
LOG_BOTTLE_SCHEMA = _baby_schema(
    {
        **_COMMON,
        probatio.Required(BabyTrackerServiceArgument.KIND): probatio.In(list(BOTTLE_KINDS)),
        probatio.Required(BabyTrackerServiceArgument.AMOUNT): probatio.Coerce(float),
        probatio.Optional(BabyTrackerServiceArgument.UNIT): _UNIT,
        probatio.Optional(BabyTrackerServiceArgument.TIME): cv.datetime,
    }
)

# Core PR 10
LOG_NURSING_SCHEMA = _baby_schema(
    {
        **_COMMON,
        probatio.Optional(BabyTrackerServiceArgument.LEFT_MINUTES, default=0): _whole_minutes,
        probatio.Optional(BabyTrackerServiceArgument.RIGHT_MINUTES, default=0): _whole_minutes,
        probatio.Optional(BabyTrackerServiceArgument.FINISH_SIDE): probatio.In(list(FINISH_SIDES)),
        probatio.Optional(BabyTrackerServiceArgument.END_TIME): cv.datetime,
    }
)

# Core PR 10
LOG_SLEEP_SCHEMA = _baby_schema(
    {
        **_COMMON,
        probatio.Required(BabyTrackerServiceArgument.START): cv.datetime,
        probatio.Required(BabyTrackerServiceArgument.END): cv.datetime,
    }
)

# Core PR 12
# A pump belongs to the account, not a baby, so it names an account.
_PUMP_COMMON = {
    probatio.Optional(BabyTrackerServiceArgument.NOTE, default=""): cv.string,
    probatio.Optional(BabyTrackerServiceArgument.OBJECT_ID): _uuid_upper,
}

# Core PR 12
_PUMP_KEYS = (
    BabyTrackerServiceArgument.CONFIG_ENTRY_ID,
    BabyTrackerServiceArgument.DEVICE,  # HACS-only
    BabyTrackerServiceArgument.BABY,  # HACS-only
)

# Core PR 12
LOG_PUMP_SCHEMA = probatio.All(
    probatio.Schema(
        {
            **_PUMP_COMMON,
            probatio.Optional(BabyTrackerServiceArgument.CONFIG_ENTRY_ID): cv.string,
            probatio.Optional(BabyTrackerServiceArgument.DEVICE): cv.string,  # HACS-only
            probatio.Optional(BabyTrackerServiceArgument.BABY): cv.string,  # HACS-only
            # The total, or the sides (checked in writes.py): so none has a default, and an
            # explicit 0 minutes alongside ``minutes`` is a conflict.
            probatio.Optional(BabyTrackerServiceArgument.AMOUNT): probatio.Coerce(float),
            probatio.Optional(BabyTrackerServiceArgument.MINUTES): _whole_minutes,
            probatio.Optional(BabyTrackerServiceArgument.LEFT_AMOUNT): probatio.Coerce(float),
            probatio.Optional(BabyTrackerServiceArgument.RIGHT_AMOUNT): probatio.Coerce(float),
            probatio.Optional(BabyTrackerServiceArgument.UNIT): _UNIT,
            probatio.Optional(BabyTrackerServiceArgument.LEFT_MINUTES): _whole_minutes,
            probatio.Optional(BabyTrackerServiceArgument.RIGHT_MINUTES): _whole_minutes,
            probatio.Optional(BabyTrackerServiceArgument.FINISH_SIDE): probatio.In(
                list(FINISH_SIDES)
            ),
            probatio.Optional(BabyTrackerServiceArgument.TIME): cv.datetime,
        }
    ),
    cv.has_at_least_one_key(*_PUMP_KEYS),
    cv.has_at_most_one_key(*_PUMP_KEYS),  # HACS-only
)

# Core PR 21
# The pump timer belongs to the account, so these name it by config entry.
START_PUMP_SCHEMA = probatio.Schema(
    {probatio.Required(BabyTrackerServiceArgument.CONFIG_ENTRY_ID): cv.string}
)

# Core PR 21
STOP_PUMP_SCHEMA = probatio.Schema(
    {
        probatio.Required(BabyTrackerServiceArgument.CONFIG_ENTRY_ID): cv.string,
        probatio.Optional(BabyTrackerServiceArgument.AMOUNT): probatio.Coerce(float),
        probatio.Optional(BabyTrackerServiceArgument.UNIT): _UNIT,
        probatio.Optional(BabyTrackerServiceArgument.NOTE, default=""): cv.string,
    }
)

# Core PR 16
START_SLEEP_SCHEMA = _baby_schema(
    {
        **_COMMON,
        probatio.Optional(BabyTrackerServiceArgument.START): cv.datetime,
    }
)

# Core PR 16
STOP_SLEEP_SCHEMA = _baby_schema({probatio.Optional(BabyTrackerServiceArgument.END): cv.datetime})

# Core PR 18
START_NURSING_SCHEMA = _baby_schema(
    {probatio.Required(BabyTrackerServiceArgument.SIDE): probatio.In(list(FINISH_SIDES))}
)

# Core PR 18
BABY_ONLY_SCHEMA = _baby_schema({})

# Core PR 18
STOP_NURSING_SCHEMA = _baby_schema(
    {probatio.Optional(BabyTrackerServiceArgument.NOTE, default=""): cv.string}
)

# Core PR 13
# A baby's undo takes its device. An account's undo takes its config entry.
UNDO_SCHEMA = probatio.All(
    probatio.Schema(
        {
            probatio.Optional(BabyTrackerServiceArgument.CONFIG_ENTRY_ID): cv.string,
            probatio.Optional(BabyTrackerServiceArgument.DEVICE_ID): cv.string,
            probatio.Optional(BabyTrackerServiceArgument.BABY): cv.string,  # HACS-only
            probatio.Optional(BabyTrackerServiceArgument.OBJECT_ID): cv.string,
        }
    ),
    cv.has_at_least_one_key(
        BabyTrackerServiceArgument.CONFIG_ENTRY_ID,
        BabyTrackerServiceArgument.DEVICE_ID,
        BabyTrackerServiceArgument.BABY,  # HACS-only
    ),
)


# HACS-only: the deprecated_action_field repair
def _warn_deprecated(call: ServiceCall, field: str, replacement: str) -> None:
    """Raise a repair issue that says an automation should use ``replacement`` instead.

    HACS-only: the ``deprecated_action_field`` repair, for the deprecated ``baby`` and ``device``
    fields.
    """
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
    """Return the coordinator and ID of the baby an action names by ``device_id``."""
    # HACS-only: the deprecated ``baby`` alias
    if BabyTrackerServiceArgument.DEVICE_ID not in call.data:
        _warn_deprecated(
            call, BabyTrackerServiceArgument.BABY, BabyTrackerServiceArgument.DEVICE_ID
        )
        return _from_baby_device(call.hass, call.data[BabyTrackerServiceArgument.BABY])
    return _from_baby_device(call.hass, call.data[BabyTrackerServiceArgument.DEVICE_ID])


# Core PR 12
def _account_coordinator(call: ServiceCall) -> BabyTrackerCoordinator:
    """Return the account a pump goes to: the named config entry."""
    # HACS-only: the deprecated ``device`` and ``baby`` name the entry that owns a device (a
    # baby's or the account's).
    if BabyTrackerServiceArgument.CONFIG_ENTRY_ID not in call.data:
        field = (
            BabyTrackerServiceArgument.DEVICE
            if BabyTrackerServiceArgument.DEVICE in call.data
            else BabyTrackerServiceArgument.BABY
        )
        _warn_deprecated(call, field, BabyTrackerServiceArgument.CONFIG_ENTRY_ID)
        return _from_device(call.hass, call.data[field])[0]
    return _entry_coordinator(call.hass, call.data[BabyTrackerServiceArgument.CONFIG_ENTRY_ID])


async def _log_diaper(call: ServiceCall) -> None:
    coordinator, baby_id = _baby(call)
    await async_log_diaper(
        coordinator,
        baby_id,
        call.data[BabyTrackerServiceArgument.TYPE],
        time=call.data.get(BabyTrackerServiceArgument.TIME),
        note=call.data[BabyTrackerServiceArgument.NOTE],
        object_id=call.data.get(BabyTrackerServiceArgument.OBJECT_ID),
    )


# Core PR 26
async def _log_medicine(call: ServiceCall) -> None:
    coordinator, baby_id = _baby(call)
    await async_log_medicine(
        coordinator,
        baby_id,
        call.data[BabyTrackerServiceArgument.MEDICINE],
        amount=call.data.get(BabyTrackerServiceArgument.AMOUNT),
        time=call.data.get(BabyTrackerServiceArgument.TIME),
        note=call.data[BabyTrackerServiceArgument.NOTE],
        object_id=call.data.get(BabyTrackerServiceArgument.OBJECT_ID),
    )


# Core PR 10
async def _log_bottle(call: ServiceCall) -> None:
    coordinator, baby_id = _baby(call)
    await async_log_bottle(
        coordinator,
        baby_id,
        call.data[BabyTrackerServiceArgument.KIND],
        call.data[BabyTrackerServiceArgument.AMOUNT],
        unit=call.data.get(BabyTrackerServiceArgument.UNIT),
        time=call.data.get(BabyTrackerServiceArgument.TIME),
        note=call.data[BabyTrackerServiceArgument.NOTE],
        object_id=call.data.get(BabyTrackerServiceArgument.OBJECT_ID),
    )


# Core PR 10
async def _log_nursing(call: ServiceCall) -> None:
    coordinator, baby_id = _baby(call)
    await async_log_nursing(
        coordinator,
        baby_id,
        left_minutes=call.data[BabyTrackerServiceArgument.LEFT_MINUTES],
        right_minutes=call.data[BabyTrackerServiceArgument.RIGHT_MINUTES],
        finish_side=call.data.get(BabyTrackerServiceArgument.FINISH_SIDE),
        end_time=call.data.get(BabyTrackerServiceArgument.END_TIME),
        note=call.data[BabyTrackerServiceArgument.NOTE],
        object_id=call.data.get(BabyTrackerServiceArgument.OBJECT_ID),
    )


# Core PR 10
async def _log_sleep(call: ServiceCall) -> None:
    coordinator, baby_id = _baby(call)
    await async_log_sleep(
        coordinator,
        baby_id,
        start=call.data[BabyTrackerServiceArgument.START],
        end=call.data[BabyTrackerServiceArgument.END],
        note=call.data[BabyTrackerServiceArgument.NOTE],
        object_id=call.data.get(BabyTrackerServiceArgument.OBJECT_ID),
    )


# Core PR 12
async def _log_pump(call: ServiceCall) -> None:
    await async_log_pump(
        _account_coordinator(call),
        amount=call.data.get(BabyTrackerServiceArgument.AMOUNT),
        minutes=call.data.get(BabyTrackerServiceArgument.MINUTES),
        left_amount=call.data.get(BabyTrackerServiceArgument.LEFT_AMOUNT),
        right_amount=call.data.get(BabyTrackerServiceArgument.RIGHT_AMOUNT),
        unit=call.data.get(BabyTrackerServiceArgument.UNIT),
        left_minutes=call.data.get(BabyTrackerServiceArgument.LEFT_MINUTES),
        right_minutes=call.data.get(BabyTrackerServiceArgument.RIGHT_MINUTES),
        finish_side=call.data.get(BabyTrackerServiceArgument.FINISH_SIDE),
        time=call.data.get(BabyTrackerServiceArgument.TIME),
        note=call.data[BabyTrackerServiceArgument.NOTE],
        object_id=call.data.get(BabyTrackerServiceArgument.OBJECT_ID),
    )


# Core PR 21
async def _start_pump(call: ServiceCall) -> None:
    await async_start_pump(
        _entry_coordinator(call.hass, call.data[BabyTrackerServiceArgument.CONFIG_ENTRY_ID])
    )


# Core PR 21
async def _stop_pump(call: ServiceCall) -> None:
    await async_stop_pump(
        _entry_coordinator(call.hass, call.data[BabyTrackerServiceArgument.CONFIG_ENTRY_ID]),
        amount=call.data.get(BabyTrackerServiceArgument.AMOUNT),
        unit=call.data.get(BabyTrackerServiceArgument.UNIT),
        note=call.data[BabyTrackerServiceArgument.NOTE],
    )


# Core PR 16
async def _start_sleep(call: ServiceCall) -> None:
    coordinator, baby_id = _baby(call)
    await async_start_sleep(
        coordinator,
        baby_id,
        start=call.data.get(BabyTrackerServiceArgument.START),
        note=call.data[BabyTrackerServiceArgument.NOTE],
        object_id=call.data.get(BabyTrackerServiceArgument.OBJECT_ID),
    )


# Core PR 16
async def _stop_sleep(call: ServiceCall) -> None:
    coordinator, baby_id = _baby(call)
    await async_stop_sleep(coordinator, baby_id, end=call.data.get(BabyTrackerServiceArgument.END))


# Core PR 18
async def _start_nursing(call: ServiceCall) -> None:
    coordinator, baby_id = _baby(call)
    await async_start_nursing(coordinator, baby_id, call.data[BabyTrackerServiceArgument.SIDE])


# Core PR 18
async def _switch_nursing_side(call: ServiceCall) -> None:
    coordinator, baby_id = _baby(call)
    await async_switch_nursing_side(coordinator, baby_id)


# Core PR 18
async def _stop_nursing(call: ServiceCall) -> None:
    coordinator, baby_id = _baby(call)
    await async_stop_nursing(coordinator, baby_id, note=call.data[BabyTrackerServiceArgument.NOTE])


# Core PR 18
async def _discard_nursing(call: ServiceCall) -> None:
    coordinator, baby_id = _baby(call)
    await async_discard_nursing(coordinator, baby_id)


# Core PR 13
async def _undo(call: ServiceCall) -> None:
    baby_id: str | None = None
    if (
        BabyTrackerServiceArgument.DEVICE_ID in call.data
        or BabyTrackerServiceArgument.BABY in call.data  # HACS-only
    ):
        coordinator, baby_id = _baby(call)
    else:
        coordinator = _entry_coordinator(
            call.hass, call.data[BabyTrackerServiceArgument.CONFIG_ENTRY_ID]
        )
    await async_undo(
        coordinator, object_id=call.data.get(BabyTrackerServiceArgument.OBJECT_ID), baby_id=baby_id
    )


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Register the actions once for the integration, whatever entries are set up."""
    hass.services.async_register(
        DOMAIN, BabyTrackerService.LOG_DIAPER, _log_diaper, schema=LOG_DIAPER_SCHEMA
    )
    hass.services.async_register(  # Core PR 10
        DOMAIN, BabyTrackerService.LOG_BOTTLE, _log_bottle, schema=LOG_BOTTLE_SCHEMA
    )
    hass.services.async_register(  # Core PR 10
        DOMAIN, BabyTrackerService.LOG_NURSING, _log_nursing, schema=LOG_NURSING_SCHEMA
    )
    hass.services.async_register(  # Core PR 10
        DOMAIN, BabyTrackerService.LOG_SLEEP, _log_sleep, schema=LOG_SLEEP_SCHEMA
    )
    hass.services.async_register(  # Core PR 12
        DOMAIN, BabyTrackerService.LOG_PUMP, _log_pump, schema=LOG_PUMP_SCHEMA
    )
    hass.services.async_register(  # Core PR 21
        DOMAIN, BabyTrackerService.START_PUMP, _start_pump, schema=START_PUMP_SCHEMA
    )
    hass.services.async_register(  # Core PR 21
        DOMAIN, BabyTrackerService.STOP_PUMP, _stop_pump, schema=STOP_PUMP_SCHEMA
    )
    hass.services.async_register(  # Core PR 13
        DOMAIN, BabyTrackerService.UNDO, _undo, schema=UNDO_SCHEMA
    )
    hass.services.async_register(  # Core PR 16
        DOMAIN, BabyTrackerService.START_SLEEP, _start_sleep, schema=START_SLEEP_SCHEMA
    )
    hass.services.async_register(  # Core PR 16
        DOMAIN, BabyTrackerService.STOP_SLEEP, _stop_sleep, schema=STOP_SLEEP_SCHEMA
    )
    hass.services.async_register(  # Core PR 18
        DOMAIN, BabyTrackerService.START_NURSING, _start_nursing, schema=START_NURSING_SCHEMA
    )
    hass.services.async_register(  # Core PR 18
        DOMAIN,
        BabyTrackerService.SWITCH_NURSING_SIDE,
        _switch_nursing_side,
        schema=BABY_ONLY_SCHEMA,
    )
    hass.services.async_register(  # Core PR 18
        DOMAIN, BabyTrackerService.STOP_NURSING, _stop_nursing, schema=STOP_NURSING_SCHEMA
    )
    hass.services.async_register(  # Core PR 18
        DOMAIN, BabyTrackerService.DISCARD_NURSING, _discard_nursing, schema=BABY_ONLY_SCHEMA
    )
    hass.services.async_register(  # Core PR 26
        DOMAIN, BabyTrackerService.LOG_MEDICINE, _log_medicine, schema=LOG_MEDICINE_SCHEMA
    )
