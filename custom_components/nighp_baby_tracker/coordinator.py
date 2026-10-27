"""Sync coordinator: keeps the sync engine current and exposes each baby's latest records."""

import asyncio
import logging
from collections.abc import Awaitable, Callable  # Core PR 9
from dataclasses import dataclass
from datetime import datetime, timedelta  # Core PR 7
from typing import TYPE_CHECKING, Any, override

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import (
    HomeAssistant,
    callback,  # Core PR 7
)
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers import device_registry as dr  # Core PR 6
from homeassistant.helpers.event import async_track_time_change  # Core PR 7
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util  # Core PR 7

from .const import (
    CONF_DEVICE_UUID,
    DOMAIN,
    SCAN_INTERVAL,
    STORAGE_VERSION,
    is_account_identifier,  # Core PR 6
)
from .guard import TimerGuard  # Core PR 18
from .pybabytracker import (
    AuthenticationError,
    BabyTrackerClient,
    BabyTrackerError,
    Record,
    Sleep,  # Core PR 8
    SyncEngine,
)
from .timer_state import TimerState  # Core PR 18
from .timers import (  # Core PR 18
    NursingTimer,
    PumpTimer,  # Core PR 21
)
from .totals import DayTotals, day_totals  # Core PR 7

_LOGGER = logging.getLogger(__name__)

type BabyTrackerConfigEntry = ConfigEntry[BabyTrackerCoordinator]

FEED_TYPES = ("Nursing", "Formula", "Pumped")

# Core PR 8
# How often to sync while a sleep, nursing, or pump timer runs, so a phone's stop shows up quickly.
TIMER_INTERVAL = timedelta(minutes=1)


@dataclass(frozen=True, slots=True)
class BabyData:
    """One baby's newest record of each kind the sensors show."""

    baby_id: str
    name: str | None
    diaper: Record | None = None
    feed: Record | None = None
    sleep: Record | None = None
    today: DayTotals = DayTotals()  # Core PR 7
    nursing: NursingTimer | None = None  # Core PR 18

    # Core PR 8
    @property
    def running_sleep(self) -> Sleep | None:
        """The newest sleep, if it's still running. The asleep sensor follows the same rule."""
        return self.sleep if isinstance(self.sleep, Sleep) and self.sleep.is_running else None


@dataclass(frozen=True, slots=True)
class BabyTrackerData:
    """Everything the entities show, compared by value so an unchanged refresh writes nothing."""

    babies: dict[str, BabyData]
    # Core PR 5
    # The newest pump on the account, whoever logged it. A pump belongs to the account, not a
    # baby, so it isn't part of any BabyData.
    last_pump: Record | None = None
    # Core PR 21
    # Home Assistant's own pump timer, while it runs. It belongs to the account too.
    pump: PumpTimer | None = None


def storage_key(entry_id: str) -> str:
    """Return the storage key for an entry's sync state."""
    return f"{DOMAIN}.{entry_id}"


def _newest(records: list[Record]) -> Record | None:
    def key(record: Record) -> tuple[float, float]:
        if TYPE_CHECKING:
            # latest_records only returns records with a time.
            assert record.time is not None
        stamp = record.timestamp.timestamp() if record.timestamp is not None else 0.0
        return (record.time.timestamp(), stamp)

    return max(records, key=key, default=None)


# Core PR 9
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
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, storage_key(entry.entry_id)
        )
        self._baby_names: dict[str, str | None] = {}
        self.lock = asyncio.Lock()
        self.timers = TimerState(hass, entry, self._publish)  # Core PR 18
        self.guard = TimerGuard(hass, entry, self.timers, self._async_republish)  # Core PR 18

    @override
    async def _async_setup(self) -> None:
        """Load what was saved, so a restart fetches only what's newer."""
        self.guard.async_setup()  # Core PR 18
        self._async_track_midnight()  # Core PR 7
        await self.timers.async_load()  # Core PR 18
        await self._async_load_engine()

    async def _async_load_engine(self) -> None:
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

    @override
    async def _async_update_data(self) -> BabyTrackerData:
        async with self.lock:
            return await self._async_sync()

    async def _async_sync(self) -> BabyTrackerData:
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

    def _snapshot(self) -> BabyTrackerData:
        """Build the data, changing nothing."""
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
                today=self._today(baby_id),  # Core PR 7
                nursing=self.timers.nursing.get(baby_id),  # Core PR 18
            )
        return BabyTrackerData(
            babies=babies,
            last_pump=self.engine.latest_pump(),  # Core PR 5
            pump=self.timers.pump,  # Core PR 21
        )

    def _rebuild(self) -> BabyTrackerData:
        """Build the data, then do what a new snapshot calls for.

        Every path that publishes data comes through here. It remembers the babies' names for the
        next snapshot.
        """
        data = self._snapshot()
        self._remove_deleted_devices(data)  # Core PR 6
        self._baby_names = {baby_id: baby.name for baby_id, baby in data.babies.items()}
        # Core PR 18
        # The saved copy of an orphaned nursing timer is dropped in a task the entry tracks.
        self.timers.drop_orphaned_nursing(data.babies)
        self.guard.check(self._baby_names)  # Core PR 18
        # Core PR 8
        # Set here, so the next refresh is scheduled on it after a sync, a write, or a timer change.
        self.update_interval = self.wanted_interval(data)
        return data

    # Core PR 9
    def _publish(self) -> None:
        """Rebuild the data and hand it to the entities at once, without waiting for a poll."""
        self.async_set_updated_data(self._rebuild())

    # Core PR 7
    @callback
    def _async_republish(self) -> None:
        """Publish fresh data, leaving ``last_update_success`` alone.

        Entities that are unavailable because a sync is failing stay unavailable.
        """
        self.data = self._rebuild()
        self.async_update_listeners()

    # Core PR 8
    def wanted_interval(self, data: BabyTrackerData | None = None) -> timedelta:
        """Return the sync interval: one minute while a timer runs, else five.

        A sleep timer (synced), a nursing timer, or the pump timer (both Home Assistant's) counts
        as running. A nursing or pump timer that has run past its guard hour is probably
        forgotten, so it no longer counts. A synced sleep always does: every phone shows it, and
        any can end it.
        """
        current = data if data is not None else self.data
        babies = current.babies if current is not None else {}
        now = dt_util.utcnow()
        if (
            self.timers.nursing_running(babies, now)
            or self._sleep_running(babies)
            or self.timers.pump_running(now)
        ):
            return TIMER_INTERVAL
        return SCAN_INTERVAL

    # Core PR 8
    @staticmethod
    def _sleep_running(babies: dict[str, BabyData]) -> bool:
        return any(baby.running_sleep is not None for baby in babies.values())

    # Core PR 7
    def _today(self, baby_id: str) -> DayTotals:
        """Return one baby's counts for today.

        Today is the local calendar day, so a day with a clock change is 23 or 25 hours long.
        """
        today = dt_util.now().date()
        start = dt_util.start_of_local_day(today)
        end = dt_util.start_of_local_day(today + timedelta(days=1))
        return day_totals(self.engine.records.values(), baby_id, start, end)

    # Core PR 7
    def _async_track_midnight(self) -> None:
        """Start a new day of counts at local midnight even when nothing syncs."""
        self.config_entry.async_on_unload(
            async_track_time_change(self.hass, self._async_midnight, hour=0, minute=0, second=0)
        )

    # Core PR 7
    @callback
    def _async_midnight(self, _now: datetime) -> None:
        """Start a new day of counts without syncing."""
        if self.data is not None:
            self._async_republish()

    # Core PR 6
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

    # Core PR 9
    def exclusive(self) -> asyncio.Lock:
        """Return the lock that syncs and writes take.

        Use it when several steps must run with neither a sync nor a write in between (a nursing
        timer change). Pass ``locked=True`` to ``async_write`` while holding it.
        """
        return self.lock

    # Core PR 9
    async def async_write[T](
        self,
        operation: Callable[[SyncEngine, BabyTrackerClient], Awaitable[T]],
        *,
        locked: bool = False,
    ) -> T:
        """Run a write under the lock: sync first, call ``operation``, save, then publish.

        The engine state is saved after every attempt, failed ones included, because the engine's
        pending post and its grace window live only in that state. The new data reaches the
        entities at once, without waiting for the next poll. A refused or failed write still
        publishes what the sync before it learned. Library
        errors propagate as they are; a failed sync before the write is wrapped in
        ``SyncBeforeWriteError`` (an authentication error passes through as itself). Pass
        ``locked=True`` when the caller already holds ``exclusive()`` (a nursing stop).
        """
        if locked:
            return await self._async_write_locked(operation)
        async with self.lock:
            return await self._async_write_locked(operation)

    # Core PR 9
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
            try:
                result = await operation(self.engine, self.client)
            except Exception:
                # The sync before the write succeeded, so what it learned is true even though the
                # write was refused (a phone already ended the sleep): show it now.
                self._publish()
                raise
        finally:
            await self._async_save()
        self._publish()
        return result
