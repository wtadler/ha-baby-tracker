"""Writing diapers and undoing them, one baby tracker entry at a time.

Every write goes through the coordinator's ``async_write``, which holds the lock, syncs first (so
the baby is known and the numbering is current), saves the engine state even when the write
failed, and publishes the new state. This module adds the Home Assistant error translation.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import datetime

from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util

from .const import CONF_EMAIL, DOMAIN
from .coordinator import BabyTrackerCoordinator, SyncBeforeWriteError
from .pybabytracker import (
    DIAPER_DIRTY,
    DIAPER_MIXED,
    DIAPER_WET,
    AuthenticationError,
    BabyTrackerClient,
    BabyTrackerError,
    NotOwnRecordError,
    ProtocolError,
    RecordNotFoundError,
    SyncEngine,
    UnknownBabyError,
    WriteOutcomeUnknownError,
)

_LOGGER = logging.getLogger(__name__)

DIAPER_STATUS = {"wet": DIAPER_WET, "dirty": DIAPER_DIRTY, "mixed": DIAPER_MIXED}


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
    is posted again. Without one, each call is a new logical write (a button press).
    """
    moment, offset = _in_home_assistant_time(time)

    async def add(engine: SyncEngine, client: BabyTrackerClient) -> object:
        try:
            return await engine.add_diaper(
                client,
                baby_id,
                DIAPER_STATUS[kind],
                time=moment,
                timezone_offset=offset,
                note=note,
                object_id=object_id,
            )
        except ValueError as err:
            if object_id is None:
                raise
            # The engine refuses an object_id that another record already holds.
            raise validation_error("object_id_in_use") from err

    await _write(coordinator, add)


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
