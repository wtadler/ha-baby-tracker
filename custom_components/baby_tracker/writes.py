"""Writing diapers, bottles, nursing, sleeps, and pumps, running the timers, and undoing writes.

Every write goes through the coordinator's ``async_write``, which holds the lock, syncs first (so
the baby is known and the numbering is current), saves the engine state even when the write
failed, and publishes the new state. This module adds the Home Assistant error translation and
the checks that refuse bad input before anything is sent.
"""

import logging
import uuid
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta

from homeassistant.const import CONF_EMAIL
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util
from homeassistant.util.hass_dict import HassKey
from pybabytracker import (
    DIAPER_DIRTY,
    DIAPER_MIXED,
    DIAPER_WET,
    SIDE_LEFT,
    SIDE_RIGHT,
    AmountTooSmallError,
    AuthenticationError,
    BabyTrackerClient,
    BabyTrackerError,
    EndBeforeStartError,
    InvalidAmountError,
    MinutesTooLongError,
    NotOwnRecordError,
    ObjectIdInUseError,
    ProtocolError,
    RecordNotFoundError,
    Sleep,
    SleepStartOutOfRangeError,
    SleepTooLongError,
    SyncEngine,
    TimerNotRunningError,
    UnknownBabyError,
    ValidationError,
    Volume,
    WriteOutcomeUnknownError,
    WrongRecordTypeError,
)
from pybabytracker.times import standard_offset_seconds
from pybabytracker.validation import (
    check_amount,
    check_end_after_start,
    check_minutes,
    check_pump_amounts,
    check_sleep_range,
    check_sleep_start,
)

from .bottle import BOTTLE_KINDS, OUNCES, VOLUME_UNITS, configured_bottle
from .const import DOMAIN
from .coordinator import BabyTrackerCoordinator, SyncBeforeWriteError
from .timers import SIDES, NursingTimer, PumpTimer, other_side

_LOGGER = logging.getLogger(__name__)

DIAPER_STATUS = {"wet": DIAPER_WET, "dirty": DIAPER_DIRTY, "mixed": DIAPER_MIXED}

FINISH_SIDES = {"left": SIDE_LEFT, "right": SIDE_RIGHT}


def _error(key: str, **placeholders: str) -> HomeAssistantError:
    return HomeAssistantError(
        translation_domain=DOMAIN, translation_key=key, translation_placeholders=placeholders
    )


def validation_error(key: str) -> ServiceValidationError:
    """Return a translated validation error for ``key``."""
    return ServiceValidationError(translation_domain=DOMAIN, translation_key=key)


# The translation key for each error the library's checks raise.
VALIDATION_KEYS: dict[type[ValidationError], str] = {
    InvalidAmountError: "invalid_amount",
    AmountTooSmallError: "amount_too_small",
    MinutesTooLongError: "minutes_too_long",
    EndBeforeStartError: "end_before_start",
    SleepTooLongError: "sleep_too_long",
    SleepStartOutOfRangeError: "sleep_start_out_of_range",
}


@contextmanager
def _validated() -> Iterator[None]:
    """Run the library's checks, turning the error one raises into its translated one."""
    try:
        yield
    except ValidationError as err:
        raise validation_error(VALIDATION_KEYS[type(err)]) from err


def issue_id(entry_id: str) -> str:
    """Return the repair issue ID for an entry."""
    return f"own_log_gap_{entry_id}"


def _reauth(coordinator: BabyTrackerCoordinator) -> HomeAssistantError:
    coordinator.config_entry.async_start_reauth(coordinator.hass)
    return _error("auth_failed")


# A gap in the device's own log can clear by itself, so it only becomes a repair once it has
# kept writes failing for this long. The clock runs per entry, from the first failure, and stops
# at the next write that succeeds. It lives in memory, so a restart starts it again.
GAP_REPAIR_AFTER = timedelta(hours=1)
_GAP_SINCE: HassKey[dict[str, datetime]] = HassKey(f"{DOMAIN}_own_log_gap_since")


def forget_gap(hass: HomeAssistant, entry_id: str) -> None:
    """Stop timing an entry's gap and remove its repair issue, if it has one."""
    hass.data.get(_GAP_SINCE, {}).pop(entry_id, None)
    ir.async_delete_issue(hass, DOMAIN, issue_id(entry_id))


def _own_log_gap(coordinator: BabyTrackerCoordinator) -> HomeAssistantError:
    entry = coordinator.config_entry
    now = dt_util.utcnow()
    since = coordinator.hass.data.setdefault(_GAP_SINCE, {}).setdefault(entry.entry_id, now)
    if now - since > GAP_REPAIR_AFTER:
        ir.async_create_issue(
            coordinator.hass,
            DOMAIN,
            issue_id(entry.entry_id),
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

    ``locked`` says the caller already holds the coordinator's lock.
    """
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
    forget_gap(coordinator.hass, coordinator.config_entry.entry_id)
    return result


def _in_home_assistant_time(time: datetime | None) -> tuple[datetime, int]:
    """Return the moment (default now) and the offset to record for it.

    The offset is the standard UTC offset (no daylight saving) of Home Assistant's zone at that
    moment, which is what the app writes year-round.
    """
    zone = dt_util.get_default_time_zone()
    if time is None:
        time = dt_util.now()
    elif time.tzinfo is None:
        time = time.replace(tzinfo=zone)
    return time, standard_offset_seconds(zone, time)


def _volume(amount: float, unit: str | None) -> Volume:
    """Convert an amount in a Home Assistant unit (default fl. oz.) to a library volume."""
    chosen = unit if unit is not None else OUNCES
    if chosen not in VOLUME_UNITS:
        raise validation_error("invalid_unit")
    return Volume(amount, VOLUME_UNITS[chosen])


def _checked_amount(amount: float) -> float:
    with _validated():
        checked = check_amount(amount)
    return checked


def _checked_minutes(*minutes: int) -> None:
    with _validated():
        check_minutes(*minutes)


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
    """Add a formula or pumped-milk bottle.

    ``unit`` is fl. oz. (the default) or mL.
    ``time`` defaults to now.
    """
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
    """Add a finished nursing session.

    The record starts ``left + right`` minutes before ``end_time`` (default now);
    ``finish_side`` (left or right) defaults to the last side used.
    """
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
    """Add a finished sleep from ``start`` to ``end``.

    A time without an offset is read in Home Assistant's time zone.
    """
    begin, offset = _in_home_assistant_time(start)
    finish, _ = _in_home_assistant_time(end)
    with _validated():
        check_sleep_range(begin, finish)

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
    *,
    amount: float | None = None,
    minutes: int | None = None,
    left_amount: float | None = None,
    right_amount: float | None = None,
    unit: str | None = None,
    left_minutes: int | None = None,
    right_minutes: int | None = None,
    finish_side: str | None = None,
    time: datetime | None = None,
    note: str = "",
    object_id: str | None = None,
) -> None:
    """Add a pumping session, starting at ``time`` (default now).

    Give the total as ``amount``, or the volume of one or both sides (not both: the total is then
    their sum). ``minutes`` is the length with both sides together, written as equal minutes on
    each side; ``left_minutes`` and ``right_minutes`` give each side its own (not both, even at 0).
    A total-only pump carries no per-side amounts, as the app writes it.

    A pump belongs to the account, not a baby, so it takes no baby and is written with none.
    """
    if amount is not None and (left_amount is not None or right_amount is not None):
        raise validation_error("amount_with_sides")
    if minutes is not None and (left_minutes is not None or right_minutes is not None):
        raise validation_error("minutes_with_sides")
    if amount is None and left_amount is None and right_amount is None:
        raise validation_error("no_amount")
    if amount is not None:
        total: Volume | None = _volume(_checked_amount(amount), unit)
        left = right = None
    else:
        total = None
        with _validated():
            check_pump_amounts(left_amount, right_amount)
        left = _volume(left_amount, unit) if left_amount is not None else None
        right = _volume(right_amount, unit) if right_amount is not None else None
    left_length = minutes if minutes is not None else (left_minutes or 0)
    right_length = minutes if minutes is not None else (right_minutes or 0)
    _checked_minutes(left_length, right_length)
    if finish_side is not None and finish_side not in FINISH_SIDES:
        raise validation_error("invalid_finish_side")
    moment, offset = _in_home_assistant_time(time)

    async def add(engine: SyncEngine, client: BabyTrackerClient, object_id: str | None) -> object:
        return await engine.add_pump(
            client,
            amount=total,
            left_volume=left,
            right_volume=right,
            left_minutes=left_length,
            right_minutes=right_length,
            finish_side=FINISH_SIDES[finish_side] if finish_side is not None else None,
            time=moment,
            timezone_offset=offset,
            note=note,
            object_id=object_id,
        )

    await _add(coordinator, object_id, add)


def running_sleep(engine: SyncEngine, baby_id: str) -> Sleep | None:
    """Return the baby's newest sleep if it's running, from any device."""
    newest = engine.latest_records(baby_id).get("Sleep")
    return newest if isinstance(newest, Sleep) and newest.is_running else None


def _new_id() -> str:
    return str(uuid.uuid4()).upper()


async def async_start_sleep(
    coordinator: BabyTrackerCoordinator,
    baby_id: str,
    *,
    start: datetime | None = None,
    note: str = "",
    object_id: str | None = None,
) -> None:
    """Start a sleep timer at ``start`` (default now).

    Refused while the baby's newest sleep is running, unless that sleep is the one ``object_id``
    names (a retry).
    """
    begin, offset = _in_home_assistant_time(start)
    now = dt_util.utcnow()
    with _validated():
        check_sleep_start(begin, now)

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
        if running.time is not None:
            with _validated():
                check_end_after_start(running.time, finish)
        return await engine.end_sleep(client, running.object_id, end=finish)

    await _write(coordinator, operation)


def _check_baby(coordinator: BabyTrackerCoordinator, baby_id: str) -> None:
    if not coordinator.data or baby_id not in coordinator.data.babies:
        raise validation_error("baby_not_found")


async def async_start_nursing(coordinator: BabyTrackerCoordinator, baby_id: str, side: str) -> None:
    """Start Home Assistant's nursing timer on ``side``.

    If it's running on the other side, switch to ``side``; if it's on ``side`` already, nothing
    changes. Nothing is written until the stop.

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
    timer = coordinator.timers.nursing.get(baby_id)
    if timer is None:
        await coordinator.timers.async_set_nursing(
            baby_id, NursingTimer.start(_new_id(), side, now)
        )
    elif timer.side != side:
        await coordinator.timers.async_set_nursing(baby_id, timer.switched(side, now))


async def async_switch_nursing_side(coordinator: BabyTrackerCoordinator, baby_id: str) -> None:
    """Move the running nursing timer to the other side, keeping the time on the first."""
    async with coordinator.exclusive():
        timer = coordinator.timers.nursing.get(baby_id)
        if timer is None:
            raise validation_error("no_nursing_timer")
        await coordinator.timers.async_set_nursing(
            baby_id, timer.switched(other_side(timer.side), dt_util.utcnow())
        )


async def async_stop_nursing(
    coordinator: BabyTrackerCoordinator, baby_id: str, *, note: str = ""
) -> None:
    """Stop the nursing timer and write one finished record.

    The record has whole minutes per side, the side it ended on, and the start time. The minutes
    count to when the lock is acquired. The timer is cleared only once the write succeeds, and
    the record's ID was fixed at the start, so stopping again after an error can't write twice.
    """
    async with coordinator.exclusive():
        await _stop_nursing_locked(coordinator, baby_id, note=note)


async def _stop_nursing_locked(
    coordinator: BabyTrackerCoordinator, baby_id: str, *, note: str = ""
) -> None:
    timer = coordinator.timers.nursing.get(baby_id)
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
    await coordinator.timers.async_set_nursing(baby_id, None)


async def async_discard_nursing(coordinator: BabyTrackerCoordinator, baby_id: str) -> None:
    """Throw away the running nursing timer without writing anything.

    For a timer left running by mistake. It holds the coordinator's lock like every timer change,
    so a stop already writing finishes first and this then finds no timer. It never syncs or posts.
    """
    async with coordinator.exclusive():
        if coordinator.timers.nursing.get(baby_id) is None:
            raise validation_error("no_nursing_timer")
        await coordinator.timers.async_set_nursing(baby_id, None)


async def async_turn_off_nursing_side(
    coordinator: BabyTrackerCoordinator, baby_id: str, side: str
) -> None:
    """Handle a nursing side's switch turned off: stop the session if it's running on ``side``.

    A side that isn't the running one (or no session at all) does nothing. Decided under the
    lock, against the timer as it is then, so a switch of sides in between isn't undone.
    """
    async with coordinator.exclusive():
        timer = coordinator.timers.nursing.get(baby_id)
        if timer is not None and timer.side == side:
            await _stop_nursing_locked(coordinator, baby_id)


async def async_start_pump(coordinator: BabyTrackerCoordinator) -> None:
    """Start Home Assistant's pump timer. Nothing is written until the stop.

    Refused while it's running. Like every timer change, it holds the coordinator's lock.
    """
    async with coordinator.exclusive():
        if coordinator.timers.pump is not None:
            raise validation_error("pump_already_running")
        await coordinator.timers.async_set_pump(PumpTimer.start(_new_id(), dt_util.utcnow()))


async def async_stop_pump(
    coordinator: BabyTrackerCoordinator,
    *,
    amount: float | None = None,
    unit: str | None = None,
    note: str = "",
) -> None:
    """Stop the pump timer and write one finished pump.

    The record starts when the timer did and has equal whole minutes on both sides. ``amount``
    (fl. oz. unless ``unit`` says mL) is the total; without it the record's amount is 0 (as the
    app writes a timed pump with no volume), in ``unit`` or else the Bottle button's unit. A bad
    amount is refused and the timer keeps running. The timer is cleared only once the write
    succeeds, and the record's ID was fixed at the start, so stopping again after an error can't
    write twice.
    """
    volume = _volume(_checked_amount(amount), unit) if amount is not None else None
    # The unit of the zero written when there's no amount: the one given, else the unit the
    # Bottle button uses (the options, or fl. oz. / mL by the unit system).
    if unit is None:
        unit = configured_bottle(coordinator.hass, coordinator.config_entry.options)[1]
    zero_unit = _volume(0, unit).unit
    async with coordinator.exclusive():
        timer = coordinator.timers.pump
        if timer is None:
            raise validation_error("no_pump_timer")
        length = timer.minutes(dt_util.utcnow())
        start, offset = _in_home_assistant_time(timer.started)

        async def add(
            engine: SyncEngine, client: BabyTrackerClient, object_id: str | None
        ) -> object:
            return await engine.add_pump(
                client,
                amount=volume,
                default_unit=zero_unit,
                left_minutes=length,
                right_minutes=length,
                time=start,
                timezone_offset=offset,
                note=note,
                object_id=object_id,
            )

        await _add(coordinator, timer.object_id, add, locked=True)
        await coordinator.timers.async_set_pump(None)


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
