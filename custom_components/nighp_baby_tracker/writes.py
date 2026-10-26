"""Writing diapers and undoing them, one baby tracker entry at a time.

Every write takes the coordinator's lock, syncs first (so the baby is known and the numbering
is current), writes, pushes the new state to the entities, and saves the engine state, even when
the write failed: the engine's pending post and its grace window live only in that saved state.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import datetime

from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util

from .const import CONF_EMAIL, DOMAIN
from .coordinator import BabyTrackerCoordinator
from .pybabytracker import (
    DIAPER_DIRTY,
    DIAPER_MIXED,
    DIAPER_WET,
    AuthenticationError,
    BabyTrackerError,
    NotOwnRecordError,
    ProtocolError,
    Record,
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


def _validation_error(key: str) -> ServiceValidationError:
    return ServiceValidationError(translation_domain=DOMAIN, translation_key=key)


def _issue_id(coordinator: BabyTrackerCoordinator) -> str:
    return f"own_log_gap_{coordinator.config_entry.entry_id}"


def _reauth(coordinator: BabyTrackerCoordinator) -> HomeAssistantError:
    coordinator.config_entry.async_start_reauth(coordinator.hass)
    return _error("auth_failed")


def _own_log_gap(coordinator: BabyTrackerCoordinator) -> HomeAssistantError:
    entry = coordinator.config_entry
    ir.async_create_issue(
        coordinator.hass,
        DOMAIN,
        _issue_id(coordinator),
        is_fixable=False,
        severity=ir.IssueSeverity.ERROR,
        translation_key="own_log_gap",
        translation_placeholders={"account": entry.data[CONF_EMAIL]},
    )
    return _error("own_log_gap")


async def _write[T](
    coordinator: BabyTrackerCoordinator,
    operation: Callable[[SyncEngine], Awaitable[T]],
) -> T:
    """Sync, run ``operation`` on the engine, then publish and save the result."""
    async with coordinator.lock:
        try:
            try:
                await coordinator.engine.sync(coordinator.client)
            except AuthenticationError as err:
                raise _reauth(coordinator) from err
            except BabyTrackerError as err:
                raise _error("sync_failed", error=str(err)) from err
            try:
                result = await operation(coordinator.engine)
            except AuthenticationError as err:
                raise _reauth(coordinator) from err
            except UnknownBabyError as err:
                raise _validation_error("baby_not_found") from err
            except WriteOutcomeUnknownError as err:
                raise _error("write_outcome_unknown") from err
            except NotOwnRecordError as err:
                raise _validation_error("not_own_record") from err
            except RecordNotFoundError as err:
                raise _validation_error("record_not_found") from err
            except ProtocolError as err:
                _LOGGER.error("This device's own log can't be caught up: %s", err)
                raise _own_log_gap(coordinator) from err
            except BabyTrackerError as err:
                raise _error("write_failed", error=str(err)) from err
        finally:
            # Save after every attempt, failed ones included: the pending post lives here.
            await coordinator._async_save()  # noqa: SLF001
        ir.async_delete_issue(coordinator.hass, DOMAIN, _issue_id(coordinator))
        coordinator.async_set_updated_data(coordinator._snapshot())  # noqa: SLF001
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
) -> None:
    """Add a diaper for a baby. ``kind`` is wet, dirty, or mixed; ``time`` defaults to now."""
    moment, offset = _in_home_assistant_time(time)

    async def add(engine: SyncEngine) -> object:
        # No object_id: each call is a new logical write, even after a failed one.
        return await engine.add_diaper(
            coordinator.client,
            baby_id,
            DIAPER_STATUS[kind],
            time=moment,
            timezone_offset=offset,
            note=note,
        )

    await _write(coordinator, add)


def _most_recent_own(engine: SyncEngine, baby_id: str | None) -> str | None:
    candidates = [
        record
        for object_id in engine.created_ids
        if (record := engine.records.get(object_id)) is not None
        and (baby_id is None or (record.baby is not None and record.baby.object_id == baby_id))
    ]
    if not candidates:
        return None
    # Order by this device's own log number (the order of its writes), which the saved state
    # carries per record. A record another device has since edited carries that device's
    # number, so it sorts first (-1) and falls back on its timestamp.
    own = engine.device_uuid
    numbers = {
        entry["payload"]["objectID"]: entry["version"]["sync_id"]
        for entry in engine.to_dict()["records"]
        if entry["version"]["device"] == own
    }

    def order(record: Record) -> tuple[int, float]:
        stamp = record.timestamp.timestamp() if record.timestamp is not None else 0.0
        return (numbers.get(record.object_id, -1), stamp)

    return max(candidates, key=order).object_id


async def async_undo(
    coordinator: BabyTrackerCoordinator,
    *,
    object_id: str | None = None,
    baby_id: str | None = None,
) -> None:
    """Delete a record Home Assistant created: the given one, else the most recent.

    With a ``baby_id`` and no ``object_id``, only that baby's records are considered.
    """

    async def delete(engine: SyncEngine) -> None:
        target = object_id if object_id is not None else _most_recent_own(engine, baby_id)
        if target is None:
            raise _validation_error("nothing_to_undo")
        await engine.delete_record(coordinator.client, target)

    await _write(coordinator, delete)
