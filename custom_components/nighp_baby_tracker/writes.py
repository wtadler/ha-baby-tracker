"""Writing diapers, bottles, nursing, sleeps, and pumps, running the timers, and undoing writes.

Every write goes through the coordinator's ``async_write``, which holds the lock, syncs first (so
the baby is known and the numbering is current), saves the engine state even when the write
failed, and publishes the new state. This module adds the Home Assistant error translation and
the checks that refuse bad input before anything is sent.
"""

from __future__ import annotations

import logging
import math
import uuid
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime
from typing import Any

from homeassistant.const import UnitOfVolume
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
    ObjectIdInUseError,
    ProtocolError,
    RecordNotFoundError,
    Sleep,
    SyncEngine,
    TimerNotRunningError,
    UnknownBabyError,
    Volume,
    WriteOutcomeUnknownError,
    WrongRecordTypeError,
)
from .pybabytracker.builders import MAX_MINUTES_PER_SIDE, MAX_SLEEP, START_SKEW
from .timers import SIDES, NursingTimer, other_side

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
    *,
    locked: bool = False,
) -> T:
    """Run a write through the coordinator and turn library errors into Home Assistant's.
    ``locked`` says the caller already holds the coordinator's lock."""
    try:
        result = await coordinator.async_write(operation, locked=locked)
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
    except TimerNotRunningError as err:
        raise validation_error("no_running_sleep") from err
    except WrongRecordTypeError as err:
        raise validation_error("not_a_sleep") from err
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


def default_bottle() -> tuple[float, str, str]:
    """The bottle size, unit, and type used until the options say otherwise.

    Fluid ounces in every unit system: milliliter writes aren't supported yet.
    """
    return DEFAULT_BOTTLE_OUNCES, OUNCES, DEFAULT_BOTTLE_KIND


def configured_bottle(options: Mapping[str, Any]) -> tuple[float, str, str]:
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
    return default_bottle()


def _volume(amount: float, unit: str | None) -> Volume:
    """An amount in a Home Assistant unit (default fl. oz.) as a library volume."""
    chosen = unit if unit is not None else OUNCES
    if chosen == MILLILITERS:
        # The library refuses metric writes until the app's metric format is confirmed.
        raise validation_error("metric_not_supported")
    if chosen not in VOLUME_UNITS:
        raise validation_error("invalid_unit")
    return Volume(amount, VOLUME_UNITS[chosen])


def _checked_amount(amount: float) -> float:
    if not math.isfinite(amount) or amount <= 0:
        raise validation_error("invalid_amount")
    if round(amount, 2) <= 0:  # the library writes two decimals
        raise validation_error("amount_too_small")
    return amount


def _checked_minutes(*minutes: int) -> None:
    if any(m > MAX_MINUTES_PER_SIDE for m in minutes):
        raise validation_error("minutes_too_long")


async def _add(
    coordinator: BabyTrackerCoordinator,
    object_id: str | None,
    add: Callable[[SyncEngine, BabyTrackerClient, str | None], Awaitable[object]],
    *,
    locked: bool = False,
) -> None:
    """Run one add through ``_write``, naming a clash with another record's ID."""

    async def operation(engine: SyncEngine, client: BabyTrackerClient) -> object:
        try:
            return await add(engine, client, object_id)
        except ObjectIdInUseError as err:
            # Only the engine's ID-collision refusal means the ID is taken; anything else surfaces.
            if object_id is None:
                raise
            raise validation_error("object_id_in_use") from err

    await _write(coordinator, operation, locked=locked)


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
    """Add a formula or pumped-milk bottle. ``unit`` is fl. oz. (the default); mL is refused for
    now (``metric_not_supported``). ``time`` defaults to now."""
    if kind not in BOTTLE_KINDS:
        raise validation_error("invalid_bottle_kind")
    volume = _volume(_checked_amount(amount), unit)
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
    size, unit, kind = configured_bottle(coordinator.config_entry.options)
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
    _checked_minutes(left_minutes, right_minutes)
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
    if finish - begin > MAX_SLEEP:
        raise validation_error("sleep_too_long")

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
    if round(left_amount or 0, 2) + round(right_amount or 0, 2) <= 0:
        raise validation_error("amount_too_small")  # each side is saved to two decimals
    _checked_minutes(left_minutes, right_minutes)
    if finish_side is not None and finish_side not in FINISH_SIDES:
        raise validation_error("invalid_finish_side")
    left = _volume(left_amount, unit) if left_amount is not None else None
    right = _volume(right_amount, unit) if right_amount is not None else None
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


def running_sleep(engine: SyncEngine, baby_id: str) -> Sleep | None:
    """The baby's newest sleep if it's running, from any device (the asleep sensor's rule)."""
    newest = engine.latest_records(baby_id).get("Sleep")
    return newest if isinstance(newest, Sleep) and newest.is_running else None


def _new_id() -> str:
    return str(uuid.uuid4()).upper()


async def async_press_sleep(coordinator: BabyTrackerCoordinator, baby_id: str) -> None:
    """The Sleep button: end the running sleep the button showed, else start one now.

    What the press means is fixed by what Home Assistant showed when it was pressed. If a phone
    changed things since (the sync before the write finds that sleep already ended, or a sleep
    already started), the press writes nothing, so it never starts a second sleep or a new one
    the parent meant to stop, and raises a validation error saying so (after the new state is
    shown).
    """
    shown = coordinator.data.get(baby_id) if coordinator.data else None
    seen = shown.running_sleep if shown is not None else None
    moment, offset = _in_home_assistant_time(None)
    new_id = _new_id()

    async def operation(engine: SyncEngine, client: BabyTrackerClient) -> str | None:
        if seen is not None:
            record = engine.records.get(seen.object_id)
            if not (isinstance(record, Sleep) and record.is_running):
                return "sleep_already_ended"
            await engine.end_sleep(client, seen.object_id, end=moment)
            return None
        if running_sleep(engine, baby_id) is not None:
            return "sleep_already_started"
        await engine.start_sleep(
            client, baby_id, start=moment, timezone_offset=offset, object_id=new_id
        )
        return None

    if (stale := await _write(coordinator, operation)) is not None:
        raise validation_error(stale)


async def async_start_sleep(
    coordinator: BabyTrackerCoordinator,
    baby_id: str,
    *,
    start: datetime | None = None,
    note: str = "",
    object_id: str | None = None,
) -> None:
    """Start a sleep timer at ``start`` (default now). Refused while the baby's newest sleep is
    running, unless that sleep is the one ``object_id`` names (a retry)."""
    begin, offset = _in_home_assistant_time(start)
    now = dt_util.utcnow()
    if begin - now > START_SKEW or now - begin > MAX_SLEEP:
        raise validation_error("sleep_start_out_of_range")

    async def add(engine: SyncEngine, client: BabyTrackerClient, object_id: str | None) -> object:
        running = running_sleep(engine, baby_id)
        if running is not None and running.object_id != object_id:
            raise validation_error("sleep_already_running")
        return await engine.start_sleep(
            client, baby_id, start=begin, timezone_offset=offset, note=note, object_id=object_id
        )

    await _add(coordinator, object_id, add)


async def async_stop_sleep(
    coordinator: BabyTrackerCoordinator, baby_id: str, *, end: datetime | None = None
) -> None:
    """End the baby's running sleep, whichever device started it, at ``end`` (default now)."""
    finish, _ = _in_home_assistant_time(end)

    async def operation(engine: SyncEngine, client: BabyTrackerClient) -> object:
        running = running_sleep(engine, baby_id)
        if running is None:
            raise validation_error("no_running_sleep")
        if running.time is not None and finish <= running.time:
            raise validation_error("end_before_start")
        return await engine.end_sleep(client, running.object_id, end=finish)

    await _write(coordinator, operation)


def _check_baby(coordinator: BabyTrackerCoordinator, baby_id: str) -> None:
    if not coordinator.data or baby_id not in coordinator.data:
        raise validation_error("baby_not_found")


async def async_start_nursing(coordinator: BabyTrackerCoordinator, baby_id: str, side: str) -> None:
    """Start Home Assistant's nursing timer on ``side``. If it's running on the other side, switch
    to ``side``; if it's on ``side`` already, nothing changes. Nothing is written until the stop.

    Every nursing timer change holds the coordinator's lock (``exclusive()``), so one can't land
    in the middle of a stop's write and be erased when the stop clears the timer.
    """
    if side not in SIDES:
        raise validation_error("invalid_finish_side")
    async with coordinator.exclusive():
        await _start_nursing_locked(coordinator, baby_id, side)


async def _start_nursing_locked(
    coordinator: BabyTrackerCoordinator, baby_id: str, side: str
) -> None:
    _check_baby(coordinator, baby_id)
    now = dt_util.utcnow()
    timer = coordinator.nursing.get(baby_id)
    if timer is None:
        await coordinator.async_set_nursing(baby_id, NursingTimer.start(_new_id(), side, now))
    elif timer.side != side:
        await coordinator.async_set_nursing(baby_id, timer.switched(side, now))


async def async_switch_nursing_side(coordinator: BabyTrackerCoordinator, baby_id: str) -> None:
    """Move the running nursing timer to the other side, keeping the time on the first."""
    async with coordinator.exclusive():
        timer = coordinator.nursing.get(baby_id)
        if timer is None:
            raise validation_error("no_nursing_timer")
        await coordinator.async_set_nursing(
            baby_id, timer.switched(other_side(timer.side), dt_util.utcnow())
        )


async def async_stop_nursing(
    coordinator: BabyTrackerCoordinator, baby_id: str, *, note: str = ""
) -> None:
    """Stop the nursing timer and write one finished record: whole minutes per side, the side it
    ended on, and the start time. The minutes count to when the lock is acquired. The timer is
    cleared only once the write succeeds, and the record's ID was fixed at the start, so stopping
    again after an error can't write twice."""
    async with coordinator.exclusive():
        await _stop_nursing_locked(coordinator, baby_id, note=note)


async def _stop_nursing_locked(
    coordinator: BabyTrackerCoordinator, baby_id: str, *, note: str = ""
) -> None:
    timer = coordinator.nursing.get(baby_id)
    if timer is None:
        raise validation_error("no_nursing_timer")
    left, right = timer.minutes(dt_util.utcnow())
    start, offset = _in_home_assistant_time(timer.started)

    async def add(engine: SyncEngine, client: BabyTrackerClient, object_id: str | None) -> object:
        return await engine.add_nursing(
            client,
            baby_id,
            left_minutes=left,
            right_minutes=right,
            finish_side=FINISH_SIDES[timer.side],
            start_time=start,
            timezone_offset=offset,
            note=note,
            object_id=object_id,
        )

    await _add(coordinator, timer.object_id, add, locked=True)
    await coordinator.async_set_nursing(baby_id, None)


async def async_press_nurse(coordinator: BabyTrackerCoordinator, baby_id: str, side: str) -> None:
    """A Nurse button: start ``side``, switch to it from the other side, or stop if it's the side
    already running. Decided under the lock, against the timer as it is then."""
    async with coordinator.exclusive():
        timer = coordinator.nursing.get(baby_id)
        if timer is not None and timer.side == side:
            await _stop_nursing_locked(coordinator, baby_id)
        else:
            await _start_nursing_locked(coordinator, baby_id, side)


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
