"""Sync coordinator: keeps the sync engine current and exposes each baby's latest records."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.event import async_track_time_change
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
    CONF_DEVICE_UUID,
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL_MINUTES,
    DOMAIN,
    STORAGE_VERSION,
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

    A pump belongs to the account, not a baby, so it isn't here (see the
    coordinator's ``last_pump``).
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


def storage_key(entry_id: str) -> str:
    return f"{DOMAIN}.{entry_id}"


def nursing_storage_key(entry_id: str) -> str:
    return f"{DOMAIN}.{entry_id}.nursing"


def scan_interval(entry: ConfigEntry) -> timedelta:
    return timedelta(minutes=entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL_MINUTES))


def _newest(records: list[Record]) -> Record | None:
    def key(record: Record) -> tuple[float, float]:
        assert record.time is not None  # latest_records only returns records with a time
        stamp = record.timestamp.timestamp() if record.timestamp is not None else 0.0
        return (record.time.timestamp(), stamp)

    return max(records, key=key, default=None)


class SyncBeforeWriteError(BabyTrackerError):
    """The sync that precedes every write failed, so nothing was written."""


class BabyTrackerCoordinator(DataUpdateCoordinator[dict[str, BabyData]]):
    """Syncs with the service, saves the engine's state, and rebuilds each baby's latest records."""

    config_entry: BabyTrackerConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: BabyTrackerConfigEntry,
        client: BabyTrackerClient,
        session: aiohttp.ClientSession,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=scan_interval(entry),
        )
        self.client = client
        self.session = session
        self._device_uuid: str = entry.data[CONF_DEVICE_UUID]
        self.engine = SyncEngine(self._device_uuid)
        # Everything that uses the engine takes this lock, so a later write path can share it.
        self.lock = asyncio.Lock()
        self._store: Store = Store(hass, STORAGE_VERSION, storage_key(entry.entry_id))
        self._baby_names: dict[str, str | None] = {}
        # The newest pump on the account, whoever logged it. A pump belongs to the account, not a
        # baby, so it isn't part of any BabyData.
        self.last_pump: Record | None = None
        # Home Assistant's own nursing timers, by baby ID (see timers.py).
        self.nursing: dict[str, NursingTimer] = {}
        self._nursing_store: Store = Store(
            hass, STORAGE_VERSION, nursing_storage_key(entry.entry_id)
        )

    async def _async_setup(self) -> None:
        """Load the saved sync state, so a restart fetches only what's newer, and any running
        nursing timers."""
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

    async def _async_update_data(self) -> dict[str, BabyData]:
        async with self.lock:
            before = dict(self.engine.last_seen)
            try:
                await self.engine.sync(self.client)
            except AuthenticationError as err:
                raise ConfigEntryAuthFailed("The service rejected the email or password") from err
            except BabyTrackerError as err:
                raise UpdateFailed(f"Couldn't sync with the service: {err}") from err
            finally:
                # Keep progress even when a later device failed.
                if dict(self.engine.last_seen) != before:
                    await self._async_save()
            return self._snapshot()

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
        self.async_set_updated_data(self._snapshot())
        return result

    def wanted_interval(self, data: dict[str, BabyData] | None = None) -> timedelta:
        """One minute while any sleep (synced) or nursing timer (Home Assistant's) runs, and the
        configured interval otherwise."""
        babies = data if data is not None else self.data or {}
        if any(baby_id in babies for baby_id in self.nursing) or any(
            baby.running_sleep is not None for baby in babies.values()
        ):
            return TIMER_INTERVAL
        return scan_interval(self.config_entry)

    def exclusive(self) -> asyncio.Lock:
        """The lock syncs and writes take, for a caller that needs several steps with neither
        running in between (a nursing timer change). Pass ``locked=True`` to ``async_write``
        while holding it."""
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
        self.async_set_updated_data(self._snapshot())
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
            self.data = self._snapshot()
            self.async_update_listeners()

    def _snapshot(self) -> dict[str, BabyData]:
        # The local calendar day, so a day with a clock change is 23 or 25 hours long.
        today = dt_util.now().date()
        start = dt_util.start_of_local_day(today)
        end = dt_util.start_of_local_day(today + timedelta(days=1))
        for baby_id, baby in self.engine.babies().items():
            self._baby_names[baby_id] = baby.name
        self.last_pump = self.engine.latest_pump()
        data: dict[str, BabyData] = {}
        for baby_id, name in self._baby_names.items():
            latest = self.engine.latest_records(baby_id)
            data[baby_id] = BabyData(
                baby_id=baby_id,
                name=name,
                diaper=latest.get("Diaper"),
                feed=_newest([latest[kind] for kind in FEED_TYPES if kind in latest]),
                sleep=latest.get("Sleep"),
                today=day_totals(self.engine.records.values(), baby_id, start, end),
                nursing=self.nursing.get(baby_id),
            )
        # A saved timer for a baby no longer in the account can't be stopped: drop it.
        if orphaned := [baby_id for baby_id in self.nursing if baby_id not in data]:
            for baby_id in orphaned:
                _LOGGER.warning("Dropping the nursing timer of baby %s, no longer known", baby_id)
                del self.nursing[baby_id]
            self.hass.async_create_task(self._async_save_nursing())
        # Set here, so the next refresh is scheduled on it after a sync, a write, or a timer change.
        self.update_interval = self.wanted_interval(data)
        return data

    async def async_close(self) -> None:
        """Stop syncing and free the client.

        Home Assistant detaches the session when the config entry unloads (it was created during
        setup), and warns if an integration closes one itself, so this leaves the session alone.
        """
        await self.async_shutdown()
        await self.client.close()
