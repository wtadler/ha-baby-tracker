"""Writing diapers, bottles, nursing, sleeps, and pumps, and undoing them.

Every write goes through the coordinator's ``async_write``, which holds the lock, syncs first (so
the baby is known and the numbering is current), saves the engine state even when the write
failed, and publishes the new state. This module adds the Home Assistant error translation and
the checks that refuse bad input before anything is sent.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime
from typing import Any

from homeassistant.const import UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util

from .const import (
    CONF_BOTTLE_SIZE,
    CONF_BOTTLE_TYPE,
    CONF_BOTTLE_UNIT,
    CONF_EMAIL,
    DOMAIN,
)
from .coordinator import BabyTrackerCoordinator, SyncBeforeWriteError
from .pybabytracker import (
    DIAPER_DIRTY,
    DIAPER_MIXED,
    DIAPER_WET,
    SIDE_LEFT,
    SIDE_RIGHT,
    UNIT_MILLILITERS,
    UNIT_OUNCES,
    AuthenticationError,
    BabyTrackerClient,
    BabyTrackerError,
    NotOwnRecordError,
    ProtocolError,
    RecordNotFoundError,
    SyncEngine,
    UnknownBabyError,
    Volume,
    WriteOutcomeUnknownError,
)

_LOGGER = logging.getLogger(__name__)

DIAPER_STATUS = {"wet": DIAPER_WET, "dirty": DIAPER_DIRTY, "mixed": DIAPER_MIXED}

# Home Assistant's names for the two bottle units, and the library's.
OUNCES = UnitOfVolume.FLUID_OUNCES.value
MILLILITERS = UnitOfVolume.MILLILITERS.value
VOLUME_UNITS = {OUNCES: UNIT_OUNCES, MILLILITERS: UNIT_MILLILITERS}
BOTTLE_KINDS = ("formula", "pumped")
FINISH_SIDES = {"left": SIDE_LEFT, "right": SIDE_RIGHT}

DEFAULT_BOTTLE_KIND = "formula"
DEFAULT_BOTTLE_OUNCES = 4
DEFAULT_BOTTLE_MILLILITERS = 120


def _error(key: str, **placeholders: str) -> HomeAssistantError:
    return HomeAssistantError(
        translation_domain=DOMAIN, translation_key=key, translation_placeholders=placeholders
    )


def validation_error(key: str) -> ServiceValidationError:
    return ServiceValidationError(translation_domain=DOMAIN, translation_key=key)


def issue_id(entry_id: str) -> str:
    return f"own_log_gap_{entry_id}"


def _reauth(coordinator: BabyTrackerCoordinator) -> HomeAssistantError:
    coordinator.config_entry.async_start_reauth(coordinator.hass)
    return _error("auth_failed")


def _own_log_gap(coordinator: BabyTrackerCoordinator) -> HomeAssistantError:
    entry = coordinator.config_entry
    ir.async_create_issue(
        coordinator.hass,
        DOMAIN,
        issue_id(coordinator.config_entry.entry_id),
        is_fixable=False,
        severity=ir.IssueSeverity.ERROR,
        translation_key="own_log_gap",
        translation_placeholders={"account": entry.data[CONF_EMAIL]},
    )
    return _error("own_log_gap")


async def _write[T](
    coordinator: BabyTrackerCoordinator,
    operation: Callable[[SyncEngine, BabyTrackerClient], Awaitable[T]],
) -> T:
    """Run a write through the coordinator and turn library errors into Home Assistant's."""
    try:
        result = await coordinator.async_write(operation)
    except AuthenticationError as err:
        raise _reauth(coordinator) from err
    except SyncBeforeWriteError as err:
        raise _error("sync_failed", error=str(err)) from err
    except UnknownBabyError as err:
        raise validation_error("baby_not_found") from err
    except WriteOutcomeUnknownError as err:
        raise _error("write_outcome_unknown") from err
    except NotOwnRecordError as err:
        raise validation_error("not_own_record") from err
    except RecordNotFoundError as err:
        raise validation_error("record_not_found") from err
    except ProtocolError as err:
        _LOGGER.error("This device's own log can't be caught up: %s", err)
        raise _own_log_gap(coordinator) from err
    except BabyTrackerError as err:
        raise _error("write_failed", error=str(err)) from err
    ir.async_delete_issue(coordinator.hass, DOMAIN, issue_id(coordinator.config_entry.entry_id))
    return result


def _in_home_assistant_time(time: datetime | None) -> tuple[datetime, int]:
    """The moment (default now) and its UTC offset in Home Assistant's time zone, in seconds."""
    zone = dt_util.get_default_time_zone()
    if time is None:
        time = dt_util.now()
    elif time.tzinfo is None:
        time = time.replace(tzinfo=zone)
    offset = time.astimezone(zone).utcoffset()
    assert offset is not None
    return time, int(offset.total_seconds())


def default_volume_unit(hass: HomeAssistant) -> str:
    """Fluid ounces for a US customary setup, milliliters otherwise."""
    # Home Assistant's US customary system measures volume in gallons: too big for a bottle.
    return OUNCES if hass.config.units.volume_unit == UnitOfVolume.GALLONS else MILLILITERS


def default_bottle(hass: HomeAssistant) -> tuple[float, str, str]:
    """The bottle size, unit, and type used until the options say otherwise."""
    unit = default_volume_unit(hass)
    size = DEFAULT_BOTTLE_OUNCES if unit == OUNCES else DEFAULT_BOTTLE_MILLILITERS
    return size, unit, DEFAULT_BOTTLE_KIND


def configured_bottle(hass: HomeAssistant, options: Mapping[str, Any]) -> tuple[float, str, str]:
    """The bottle the options describe. Saved options that don't make sense (any one of the
    three) give the defaults."""
    size = options.get(CONF_BOTTLE_SIZE)
    unit = options.get(CONF_BOTTLE_UNIT)
    kind = options.get(CONF_BOTTLE_TYPE)
    if (
        isinstance(size, int | float)
        and not isinstance(size, bool)
        and math.isfinite(size)
        and size > 0
        and unit in VOLUME_UNITS
        and kind in BOTTLE_KINDS
    ):
        return size, unit, kind
    return default_bottle(hass)


def _volume(amount: float, unit: str | None, hass: HomeAssistant) -> Volume:
    """An amount in a Home Assistant unit (default: the unit system's) as a library volume."""
    chosen = unit if unit is not None else default_volume_unit(hass)
    if chosen not in VOLUME_UNITS:
        raise validation_error("invalid_unit")
    return Volume(amount, VOLUME_UNITS[chosen])


def _checked_amount(amount: float) -> float:
    if not math.isfinite(amount) or amount <= 0:
        raise validation_error("invalid_amount")
    return amount


async def _add(
    coordinator: BabyTrackerCoordinator,
    object_id: str | None,
    add: Callable[[SyncEngine, BabyTrackerClient, str | None], Awaitable[object]],
) -> None:
    """Run one add through ``_write``, naming a clash with another record's ID."""

    async def operation(engine: SyncEngine, client: BabyTrackerClient) -> object:
        try:
            return await add(engine, client, object_id)
        except ValueError as err:
            if object_id is None:
                raise
            # The engine refuses an object_id that another record already holds.
            raise validation_error("object_id_in_use") from err

    await _write(coordinator, operation)


async def async_log_diaper(
    coordinator: BabyTrackerCoordinator,
    baby_id: str,
    kind: str,
    *,
    time: datetime | None = None,
    note: str = "",
    object_id: str | None = None,
) -> None:
    """Add a diaper for a baby. ``kind`` is wet, dirty, or mixed; ``time`` defaults to now.

    Pass the same ``object_id`` to retry a write safely: if the earlier attempt landed, nothing
    is posted again. Without one, each call is a new logical write (a button press). The other
    ``async_log_*`` functions work the same way.
    """
    moment, offset = _in_home_assistant_time(time)

    async def add(engine: SyncEngine, client: BabyTrackerClient, object_id: str | None) -> object:
        return await engine.add_diaper(
            client,
            baby_id,
            DIAPER_STATUS[kind],
            time=moment,
            timezone_offset=offset,
            note=note,
            object_id=object_id,
        )

    await _add(coordinator, object_id, add)


async def async_log_bottle(
    coordinator: BabyTrackerCoordinator,
    baby_id: str,
    kind: str,
    amount: float,
    *,
    unit: str | None = None,
    time: datetime | None = None,
    note: str = "",
    object_id: str | None = None,
) -> None:
    """Add a formula or pumped-milk bottle. ``unit`` is fl. oz. or mL (default: the unit
    system's); ``time`` defaults to now."""
    if kind not in BOTTLE_KINDS:
        raise validation_error("invalid_bottle_kind")
    volume = _volume(_checked_amount(amount), unit, coordinator.hass)
    moment, offset = _in_home_assistant_time(time)

    async def add(engine: SyncEngine, client: BabyTrackerClient, object_id: str | None) -> object:
        method = engine.add_formula if kind == "formula" else engine.add_pumped
        return await method(
            client,
            baby_id,
            volume=volume,
            time=moment,
            timezone_offset=offset,
            note=note,
            object_id=object_id,
        )

    await _add(coordinator, object_id, add)


async def async_log_configured_bottle(coordinator: BabyTrackerCoordinator, baby_id: str) -> None:
    """Log the bottle the options describe, right now (the Bottle button)."""
    size, unit, kind = configured_bottle(coordinator.hass, coordinator.config_entry.options)
    await async_log_bottle(coordinator, baby_id, kind, size, unit=unit)


async def async_log_nursing(
    coordinator: BabyTrackerCoordinator,
    baby_id: str,
    *,
    left_minutes: int = 0,
    right_minutes: int = 0,
    finish_side: str | None = None,
    end_time: datetime | None = None,
    note: str = "",
    object_id: str | None = None,
) -> None:
    """Add a finished nursing session. The record starts ``left + right`` minutes before
    ``end_time`` (default now); ``finish_side`` (left or right) defaults to the last side used."""
    if left_minutes + right_minutes <= 0:
        raise validation_error("no_duration")
    if finish_side is not None and finish_side not in FINISH_SIDES:
        raise validation_error("invalid_finish_side")
    moment, offset = _in_home_assistant_time(end_time)

    async def add(engine: SyncEngine, client: BabyTrackerClient, object_id: str | None) -> object:
        return await engine.add_nursing(
            client,
            baby_id,
            left_minutes=left_minutes,
            right_minutes=right_minutes,
            finish_side=FINISH_SIDES[finish_side] if finish_side is not None else None,
            end_time=moment,
            timezone_offset=offset,
            note=note,
            object_id=object_id,
        )

    await _add(coordinator, object_id, add)


async def async_log_sleep(
    coordinator: BabyTrackerCoordinator,
    baby_id: str,
    *,
    start: datetime,
    end: datetime,
    note: str = "",
    object_id: str | None = None,
) -> None:
    """Add a finished sleep from ``start`` to ``end``. A time without an offset is read in Home
    Assistant's time zone."""
    begin, offset = _in_home_assistant_time(start)
    finish, _ = _in_home_assistant_time(end)
    if finish <= begin:
        raise validation_error("end_before_start")

    async def add(engine: SyncEngine, client: BabyTrackerClient, object_id: str | None) -> object:
        return await engine.add_sleep(
            client,
            baby_id,
            start=begin,
            end=finish,
            timezone_offset=offset,
            note=note,
            object_id=object_id,
        )

    await _add(coordinator, object_id, add)


async def async_log_pump(
    coordinator: BabyTrackerCoordinator,
    baby_id: str,
    *,
    left_amount: float | None = None,
    right_amount: float | None = None,
    unit: str | None = None,
    left_minutes: int = 0,
    right_minutes: int = 0,
    finish_side: str | None = None,
    time: datetime | None = None,
    note: str = "",
    object_id: str | None = None,
) -> None:
    """Add a pumping session of one or both sides, starting at ``time`` (default now)."""
    if left_amount is None and right_amount is None:
        raise validation_error("no_amount")
    for amount in (left_amount, right_amount):
        if amount is not None and (not math.isfinite(amount) or amount < 0):
            raise validation_error("invalid_amount")
    _checked_amount((left_amount or 0) + (right_amount or 0))
    if finish_side is not None and finish_side not in FINISH_SIDES:
        raise validation_error("invalid_finish_side")
    left = _volume(left_amount, unit, coordinator.hass) if left_amount is not None else None
    right = _volume(right_amount, unit, coordinator.hass) if right_amount is not None else None
    moment, offset = _in_home_assistant_time(time)

    async def add(engine: SyncEngine, client: BabyTrackerClient, object_id: str | None) -> object:
        return await engine.add_pump(
            client,
            baby_id,
            left_volume=left,
            right_volume=right,
            left_minutes=left_minutes,
            right_minutes=right_minutes,
            finish_side=FINISH_SIDES[finish_side] if finish_side is not None else None,
            time=moment,
            timezone_offset=offset,
            note=note,
            object_id=object_id,
        )

    await _add(coordinator, object_id, add)


def _most_recent_own(engine: SyncEngine, baby_id: str | None) -> str | None:
    # This device's adds, oldest first by the number of its own add: edits by a phone don't move
    # a record, and two writes in the same second keep their order.
    for object_id, _number in reversed(engine.own_writes()):
        record = engine.records.get(object_id)
        if record is not None and (
            baby_id is None or (record.baby is not None and record.baby.object_id == baby_id)
        ):
            return object_id
    return None


async def async_undo(
    coordinator: BabyTrackerCoordinator,
    *,
    object_id: str | None = None,
    baby_id: str | None = None,
) -> None:
    """Delete a record Home Assistant created: the given one, else the most recent.

    With a ``baby_id`` and no ``object_id``, only that baby's records are considered. With both,
    the record must belong to that baby.
    """

    async def delete(engine: SyncEngine, client: BabyTrackerClient) -> None:
        if object_id is not None:
            target = object_id.upper()
            record = engine.records.get(target)
            if (
                baby_id is not None
                and record is not None
                and (record.baby is None or record.baby.object_id != baby_id)
            ):
                raise validation_error("record_not_for_baby")
        else:
            found = _most_recent_own(engine, baby_id)
            if found is None:
                raise validation_error("nothing_to_undo")
            target = found
        await engine.delete_record(client, target)

    await _write(coordinator, delete)
