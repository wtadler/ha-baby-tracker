"""Sync coordinator: keeps the sync engine current and exposes each baby's latest records."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_track_point_in_utc_time, async_track_time_change
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
    CONF_DEVICE_UUID,
    DOMAIN,
    NURSING_TIMER_GUARD_AFTER,
    SCAN_INTERVAL,
    STORAGE_VERSION,
    is_account_identifier,
    nursing_guard_issue_id,
)
from .pybabytracker import (
    AuthenticationError,
    BabyTrackerClient,
    BabyTrackerError,
    Record,
    Sleep,
    SyncEngine,
)
from .timers import NursingTimer, load_timers
from .totals import DayTotals, day_totals

_LOGGER = logging.getLogger(__name__)

type BabyTrackerConfigEntry = ConfigEntry[BabyTrackerCoordinator]

FEED_TYPES = ("Nursing", "Formula", "Pumped")

# How often to sync while a sleep or nursing timer runs, so a phone's stop shows up quickly.
TIMER_INTERVAL = timedelta(minutes=1)


@dataclass(frozen=True, slots=True)
class BabyData:
    """One baby's newest record of each kind the sensors show.

    A pump belongs to the account, not a baby, so it isn't here (see ``BabyTrackerData``).
    """

    baby_id: str
    name: str | None
    diaper: Record | None = None
    feed: Record | None = None
    sleep: Record | None = None
    today: DayTotals = DayTotals()
    nursing: NursingTimer | None = None

    @property
    def running_sleep(self) -> Sleep | None:
        """The newest sleep, if it's still running. The asleep sensor follows the same rule."""
        return self.sleep if isinstance(self.sleep, Sleep) and self.sleep.is_running else None


@dataclass(frozen=True, slots=True)
class BabyTrackerData:
    """Everything the entities show, compared by value so an unchanged refresh writes nothing."""

    babies: dict[str, BabyData]
    # The newest pump on the account, whoever logged it. A pump belongs to the account, not a
    # baby, so it isn't part of any BabyData.
    last_pump: Record | None = None


def storage_key(entry_id: str) -> str:
    """Return the storage key for an entry's sync state."""
    return f"{DOMAIN}.{entry_id}"


def nursing_storage_key(entry_id: str) -> str:
    """Return the storage key for an entry's nursing timers."""
    return f"{DOMAIN}.{entry_id}.nursing"


def _newest(records: list[Record]) -> Record | None:
    def key(record: Record) -> tuple[float, float]:
        assert record.time is not None  # latest_records only returns records with a time
        stamp = record.timestamp.timestamp() if record.timestamp is not None else 0.0
        return (record.time.timestamp(), stamp)

    return max(records, key=key, default=None)


class SyncBeforeWriteError(BabyTrackerError):
    """The sync that precedes every write failed, so nothing was written."""


class BabyTrackerCoordinator(DataUpdateCoordinator[BabyTrackerData]):
    """Syncs with the service, saves the engine's state, and rebuilds each baby's latest records."""

    config_entry: BabyTrackerConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: BabyTrackerConfigEntry,
        client: BabyTrackerClient,
        session: aiohttp.ClientSession,
    ) -> None:
        """Set up the coordinator for one account."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=SCAN_INTERVAL,
            always_update=False,
        )
        self.client = client
        self.session = session
        self._device_uuid: str = entry.data[CONF_DEVICE_UUID]
        self.engine = SyncEngine(self._device_uuid)
        # Everything that uses the engine takes this lock, so a later write path can share it.
        self.lock = asyncio.Lock()
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, storage_key(entry.entry_id)
        )
        self._baby_names: dict[str, str | None] = {}
        # Home Assistant's own nursing timers, by baby ID (see timers.py).
        self.nursing: dict[str, NursingTimer] = {}
        self._nursing_store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, nursing_storage_key(entry.entry_id)
        )
        # The guard on a forgotten nursing timer (see ``_guard_nursing_timers``): the babies whose
        # repair is raised, and the callback waiting for each other timer's hour to end.
        self._guard_raised: set[str] = set()
        self._guard_waiting: dict[str, tuple[datetime, CALLBACK_TYPE]] = {}

    async def _async_setup(self) -> None:
        """Load the saved sync state and any running nursing timers.

        A restart then fetches only what's newer.
        """
        self.config_entry.async_on_unload(self._async_stop_guarding)
        # Today's counts change at local midnight even when nothing syncs.
        self.config_entry.async_on_unload(
            async_track_time_change(self.hass, self._async_midnight, hour=0, minute=0, second=0)
        )
        try:
            self.nursing = load_timers(await self._nursing_store.async_load())
        except HomeAssistantError as err:
            _LOGGER.warning(
                "The saved nursing timers couldn't be read (%s); starting with none", err
            )
        try:
            saved = await self._store.async_load()
        except HomeAssistantError as err:
            _LOGGER.warning("The saved sync state couldn't be read (%s); loading all history", err)
            return
        if saved is None:
            return
        try:
            self.engine = SyncEngine.from_dict(saved, self._device_uuid)
        except (KeyError, TypeError, ValueError, AttributeError, BabyTrackerError) as err:
            _LOGGER.warning(
                "The saved sync state couldn't be read (%r); loading all history again", err
            )

    async def _async_update_data(self) -> BabyTrackerData:
        async with self.lock:
            before = dict(self.engine.last_seen)
            try:
                await self.engine.sync(self.client)
            except AuthenticationError as err:
                raise ConfigEntryAuthFailed(
                    translation_domain=DOMAIN, translation_key="auth_failed"
                ) from err
            except BabyTrackerError as err:
                raise UpdateFailed(
                    translation_domain=DOMAIN,
                    translation_key="sync_failed_update",
                    translation_placeholders={"error": str(err)},
                ) from err
            finally:
                # Keep progress even when a later device failed.
                if dict(self.engine.last_seen) != before:
                    await self._async_save()
            return self._rebuild()

    async def _async_save(self) -> None:
        """Save the engine state. A failed save is logged, and never hides a sync error."""
        try:
            await self._store.async_save(self.engine.to_dict())
        except Exception:
            _LOGGER.exception("Couldn't save the sync state; it will be saved after the next sync")

    async def async_write[T](
        self,
        operation: Callable[[SyncEngine, BabyTrackerClient], Awaitable[T]],
        *,
        locked: bool = False,
    ) -> T:
        """Run a write under the lock: sync first, call ``operation``, save, then publish.

        The engine state is saved after every attempt, failed ones included, because the engine's
        pending post and its grace window live only in that state. The new data reaches the
        entities at once, without waiting for the next poll, but only after a success. Library
        errors propagate as they are; a failed sync before the write is wrapped in
        ``SyncBeforeWriteError`` (an authentication error passes through as itself). Pass
        ``locked=True`` when the caller already holds ``exclusive()`` (a nursing stop).
        """
        if locked:
            return await self._async_write_locked(operation)
        async with self.lock:
            return await self._async_write_locked(operation)

    async def _async_write_locked[T](
        self, operation: Callable[[SyncEngine, BabyTrackerClient], Awaitable[T]]
    ) -> T:
        try:
            try:
                await self.engine.sync(self.client)
            except AuthenticationError:
                raise
            except BabyTrackerError as err:
                raise SyncBeforeWriteError(str(err)) from err
            result = await operation(self.engine, self.client)
        finally:
            await self._async_save()
        self.async_set_updated_data(self._rebuild())
        return result

    def wanted_interval(self, data: BabyTrackerData | None = None) -> timedelta:
        """Return the sync interval: one minute while a timer runs, else five.

        A sleep timer (synced) or a nursing timer (Home Assistant's) counts as running. A nursing
        timer that has run past ``NURSING_TIMER_GUARD_AFTER`` is probably forgotten, so it no
        longer counts. A synced sleep always does: every phone shows it, and any can end it.
        """
        current = data if data is not None else self.data
        babies = current.babies if current is not None else {}
        now = dt_util.utcnow()
        if any(
            baby_id in babies and not timer.is_overdue(now, NURSING_TIMER_GUARD_AFTER)
            for baby_id, timer in self.nursing.items()
        ) or any(baby.running_sleep is not None for baby in babies.values()):
            return TIMER_INTERVAL
        return SCAN_INTERVAL

    def exclusive(self) -> asyncio.Lock:
        """Return the lock that syncs and writes take.

        Use it when several steps must run with neither a sync nor a write in between (a nursing
        timer change). Pass ``locked=True`` to ``async_write`` while holding it.
        """
        return self.lock

    async def async_set_nursing(self, baby_id: str, timer: NursingTimer | None) -> None:
        """Start, change, or (with ``None``) clear a baby's nursing timer, publish it, and save it.

        Hold ``exclusive()``. The change applies in memory and on the entities even if the save
        fails; the failure then raises ``HomeAssistantError`` (``nursing_not_saved``), because
        the timer won't survive a restart.
        """
        if timer is None:
            self.nursing.pop(baby_id, None)
        else:
            self.nursing[baby_id] = timer
        self.async_set_updated_data(self._rebuild())
        if not await self._async_save_nursing():
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="nursing_not_saved")

    async def _async_save_nursing(self) -> bool:
        try:
            await self._nursing_store.async_save(
                {baby: saved.to_dict() for baby, saved in self.nursing.items()}
            )
        except Exception:
            _LOGGER.exception("Couldn't save the nursing timers")
            return False
        return True

    @callback
    def _async_midnight(self, _now: datetime) -> None:
        """Start a new day of counts without syncing."""
        if self.data is not None:
            self.data = self._rebuild()
            self.async_update_listeners()

    def _snapshot(self) -> BabyTrackerData:
        """Build the data from the engine and the timers, changing nothing."""
        # The local calendar day, so a day with a clock change is 23 or 25 hours long.
        today = dt_util.now().date()
        start = dt_util.start_of_local_day(today)
        end = dt_util.start_of_local_day(today + timedelta(days=1))
        names = {
            baby_id: name
            for baby_id, name in self._baby_names.items()
            if not self.engine.is_deleted(baby_id)
        }
        for baby_id, baby in self.engine.babies().items():
            if not self.engine.is_deleted(baby_id):
                names[baby_id] = baby.name
        babies: dict[str, BabyData] = {}
        for baby_id, name in names.items():
            latest = self.engine.latest_records(baby_id)
            babies[baby_id] = BabyData(
                baby_id=baby_id,
                name=name,
                diaper=latest.get("Diaper"),
                feed=_newest([latest[kind] for kind in FEED_TYPES if kind in latest]),
                sleep=latest.get("Sleep"),
                today=day_totals(self.engine.records.values(), baby_id, start, end),
                nursing=self.nursing.get(baby_id),
            )
        return BabyTrackerData(babies=babies, last_pump=self.engine.latest_pump())

    def _rebuild(self) -> BabyTrackerData:
        """Build the data, then do what a new snapshot calls for.

        Every path that publishes data (a sync, a write, a timer change, midnight) comes through
        here. It removes the devices of babies the sync shows as deleted, remembers the babies'
        names, and drops the nursing timers of babies no longer in the account (their saved copy
        is dropped in a task the config entry tracks). It also sets the next refresh's interval.
        """
        data = self._snapshot()
        self._remove_deleted_devices(data)
        self._baby_names = {baby_id: baby.name for baby_id, baby in data.babies.items()}
        # A saved timer for a baby no longer in the account can't be stopped: drop it.
        if orphaned := [baby_id for baby_id in self.nursing if baby_id not in data.babies]:
            for baby_id in orphaned:
                _LOGGER.warning("Dropping the nursing timer of baby %s, no longer known", baby_id)
                del self.nursing[baby_id]
            self.config_entry.async_create_task(self.hass, self._async_save_nursing())
        self._guard_nursing_timers(data)
        # Set here, so the next refresh is scheduled on it after a sync, a write, or a timer change.
        self.update_interval = self.wanted_interval(data)
        return data

    def _guard_nursing_timers(self, data: BabyTrackerData) -> None:
        """Raise a repair for each nursing timer past its hour, and clear the ones that are done.

        A nursing timer lives only in Home Assistant, so a forgotten one can't be seen or ended
        anywhere else. A timer not yet an hour old gets a callback for the moment it turns one, so
        the repair doesn't wait for a sync. Nothing here touches the network.
        """
        now = dt_util.utcnow()
        for baby_id, timer in self.nursing.items():
            due = timer.started + NURSING_TIMER_GUARD_AFTER
            if now >= due:
                self._cancel_guard_wait(baby_id)
                if baby_id not in self._guard_raised:
                    self._guard_raised.add(baby_id)
                    name = data.babies[baby_id].name or "a baby"
                    ir.async_create_issue(
                        self.hass,
                        DOMAIN,
                        nursing_guard_issue_id(self.config_entry.entry_id, baby_id),
                        is_fixable=False,
                        severity=ir.IssueSeverity.WARNING,
                        translation_key="nursing_timer_long",
                        translation_placeholders={"baby": name},
                    )
            else:
                self._clear_guard_issue(baby_id)
                waiting = self._guard_waiting.get(baby_id)
                if waiting is None or waiting[0] != due:
                    self._cancel_guard_wait(baby_id)
                    self._guard_waiting[baby_id] = (
                        due,
                        async_track_point_in_utc_time(self.hass, self._async_guard_due, due),
                    )
        for baby_id in [*self._guard_raised, *self._guard_waiting]:
            if baby_id not in self.nursing:
                self._clear_guard_issue(baby_id)
                self._cancel_guard_wait(baby_id)

    def _clear_guard_issue(self, baby_id: str) -> None:
        if baby_id in self._guard_raised:
            self._guard_raised.discard(baby_id)
            ir.async_delete_issue(
                self.hass, DOMAIN, nursing_guard_issue_id(self.config_entry.entry_id, baby_id)
            )

    def _cancel_guard_wait(self, baby_id: str) -> None:
        if (waiting := self._guard_waiting.pop(baby_id, None)) is not None:
            waiting[1]()

    @callback
    def _async_guard_due(self, now: datetime) -> None:
        """Publish when a nursing timer reaches its hour: raise its repair and ease polling."""
        for baby_id, (due, _) in list(self._guard_waiting.items()):
            if due <= now:
                del self._guard_waiting[baby_id]
        self.async_set_updated_data(self._rebuild())

    @callback
    def _async_stop_guarding(self) -> None:
        """On unload or removal, drop the repairs and callbacks, which only a loaded entry owns."""
        for baby_id in [*self._guard_raised, *self._guard_waiting]:
            self._clear_guard_issue(baby_id)
            self._cancel_guard_wait(baby_id)

    def _remove_deleted_devices(self, data: BabyTrackerData) -> None:
        """Remove the device, and with it the entities, of every baby the sync shows as deleted.

        Only a baby whose record carries a delete is judged gone. A baby merely missing from the
        data is left alone, and so is the account's own device. A baby added again later gets a
        new device with the same identifiers.
        """
        entry_id = self.config_entry.entry_id
        registry = dr.async_get(self.hass)
        for device in dr.async_entries_for_config_entry(registry, entry_id):
            for domain, identifier in device.identifiers:
                if (
                    domain == DOMAIN
                    and not is_account_identifier(identifier)
                    and identifier not in data.babies
                    and self.engine.is_deleted(identifier)
                ):
                    registry.async_remove_device(device.id)
                    break
