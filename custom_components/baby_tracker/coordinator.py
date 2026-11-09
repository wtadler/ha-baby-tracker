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
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    HomeAssistantError,
)
from homeassistant.helpers import device_registry as dr  # Core PR 6
from homeassistant.helpers.event import async_track_time_change  # Core PR 7
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util  # Core PR 7
from pybabytracker import (
    AuthenticationError,
    Baby,  # Core PR 23
    BabyTrackerClient,
    BabyTrackerError,
    DayTotals,  # Core PR 7
    Formula,
    LastBreast,  # Core PR 7
    Medication,  # Core PR 26
    Nursing,
    NursingTimer,  # Core PR 18
    Pumped,
    PumpTimer,  # Core PR 21
    Record,
    Sleep,  # Core PR 8
    SyncEngine,
    day_totals,  # Core PR 7
    last_breast,  # Core PR 7
)

from .const import (
    CONF_DEVICE_UUID,
    DOMAIN,
    SCAN_INTERVAL,
    STORAGE_VERSION,
    is_account_identifier,  # Core PR 6
)
from .guard import TimerGuard  # Core PR 18
from .pictures import (  # Core PR 23
    AttachedPicture,
    PictureCache,
    newest_attached_picture,
)
from .timer_state import TimerState  # Core PR 18

_LOGGER = logging.getLogger(__name__)

type BabyTrackerConfigEntry = ConfigEntry[BabyTrackerCoordinator]

# A NursingSession (Android's timed nursing) is a Nursing, so an isinstance check covers it.
FEED_CLASSES = (Nursing, Formula, Pumped)


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
    # Core PR 23
    # The baby's photo in the app (the cover photo), as a file on the service.
    photo_file: str | None = None
    # Core PR 23
    # The newest picture attached to any of the baby's records.
    latest_picture: AttachedPicture | None = None
    milestone: Record | None = None  # Core PR 24
    vaccine: Record | None = None  # Core PR 25
    medicine: Record | None = None  # Core PR 26
    # Core PR 26
    # The newest dose of each medicine, by the medicine's ID (or its name when it has none), so
    # the next dose of every medicine is known when two of them alternate.
    medicine_doses: dict[str, Medication] | None = None
    # Core PR 4
    # The newest nursing, whatever feed came after it, so its minutes stay visible.
    last_nursing: Record | None = None

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
    # Core PR 7
    # The newest nursing (any baby) or pump, and its side, as the app shows before the next.
    last_breast: LastBreast | None = None


def storage_key(entry_id: str) -> str:
    """Return the storage key for an entry's sync state."""
    return f"{DOMAIN}.{entry_id}"


# Core PR 23
def _photo_file(baby: Baby | None) -> str | None:
    return baby.picture_file if baby is not None else None


# Core PR 26
def medicine_key(dose: Medication) -> str | None:
    """Return what tells a dose's medicine from the others: its ID, or else its name."""
    selection = dose.medication_selection
    object_id = selection.get("objectID") if isinstance(selection, dict) else None
    if isinstance(object_id, str) and object_id.strip():
        return object_id
    name = dose.medicine_name
    return name if name is not None and name.strip() else None


# Core PR 26
def _dose_order(dose: Medication) -> tuple[float, str]:
    """Order doses by time, then by record ID, so equal times give the same answer every time."""
    return (dose.time.timestamp() if dose.time is not None else 0.0, dose.object_id)


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
        # Core PR 23
        # A picture download is a background task of the entry, which stops it on unload.
        self.pictures = PictureCache(
            client,
            lambda download, name: entry.async_create_background_task(hass, download, name),
        )
        self.session = session
        self._device_uuid: str = entry.data[CONF_DEVICE_UUID]
        self.engine = SyncEngine(self._device_uuid)
        # The sync state holds the babies' care history, so only the owner may read the file.
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, storage_key(entry.entry_id), private=True
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
            _LOGGER.exception(
                "Couldn't save the sync state; it's saved again after the next sync that brings "
                "new records"
            )

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
                feed=_newest(
                    [record for record in latest.values() if isinstance(record, FEED_CLASSES)]
                ),
                sleep=latest.get("Sleep"),
                today=self._today(baby_id),  # Core PR 7
                nursing=self.timers.nursing.get(baby_id),  # Core PR 18
                photo_file=_photo_file(self.engine.babies().get(baby_id)),  # Core PR 23
                latest_picture=self._latest_picture(baby_id),  # Core PR 23
                milestone=latest.get("Milestone"),  # Core PR 24
                vaccine=latest.get("Vaccine"),  # Core PR 25
                medicine=latest.get("Medication"),  # Core PR 26
                medicine_doses=self._medicine_doses(baby_id),  # Core PR 26
                last_nursing=self._last_nursing(latest),  # Core PR 4
            )
        return BabyTrackerData(
            babies=babies,
            last_pump=self.engine.latest_pump(),  # Core PR 5
            pump=self.timers.pump,  # Core PR 21
            last_breast=last_breast(self.engine.records.values(), dt_util.utcnow()),  # Core PR 7
        )

    # Core PR 23
    def _latest_picture(self, baby_id: str) -> AttachedPicture | None:
        return newest_attached_picture(self.engine.records.values(), baby_id)

    # Core PR 26
    def _medicine_doses(self, baby_id: str) -> dict[str, Medication]:
        """Return the newest dose of each medicine the baby was given."""
        newest: dict[str, Medication] = {}
        for record in self.engine.records.values():
            if not isinstance(record, Medication) or record.baby_id != baby_id:
                continue
            key = medicine_key(record)
            if key is None or record.time is None:
                continue
            kept = newest.get(key)
            if kept is None or _dose_order(record) > _dose_order(kept):
                newest[key] = record
        return newest

    # Core PR 4
    def _last_nursing(self, latest: dict[str, Record]) -> Record | None:
        """Return the newest nursing of any kind.

        An iPhone's timer and a manual entry sync as ``Nursing``, but the Android app's timer
        syncs as ``NursingSession`` (a ``Nursing`` subclass), and the app stores both as rows of
        its one Nursing table. So the newest is picked by class, not by type name.
        """
        return _newest([record for record in latest.values() if isinstance(record, Nursing)])

    def _rebuild(self) -> BabyTrackerData:
        """Build the data, then do what a new snapshot calls for.

        Every path that publishes data comes through here. It remembers the babies' names for the
        next snapshot.
        """
        data = self._snapshot()
        self._remove_deleted_devices(data)  # Core PR 6
        self._baby_names = {baby_id: baby.name for baby_id, baby in data.babies.items()}
        self.pictures.set_babies(len(data.babies))  # Core PR 23
        # Core PR 18
        # The saved copy of an orphaned nursing timer is dropped in a task the entry tracks.
        self.timers.drop_orphaned_nursing(data.babies)
        self.guard.check(self._baby_names)  # Core PR 18
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
