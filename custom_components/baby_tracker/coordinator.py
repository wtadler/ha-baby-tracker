"""Sync coordinator: keeps the sync engine current and exposes each baby's latest records."""

import asyncio
import logging
import math  # Core PR 29
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
    ConfigEntryError,  # Core PR 27
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
    Growth,  # Core PR 29
    LastBreast,  # Core PR 7
    Measurement,  # Core PR 29
    Medication,  # Core PR 26
    Nursing,
    NursingTimer,  # Core PR 18
    OtherActivity,  # Core PR 28
    Pumped,
    PumpTimer,  # Core PR 21
    Record,
    Sleep,  # Core PR 8
    SyncEngine,
    Temperature,  # Core PR 29
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
from .pictures import PictureCache  # Core PR 23
from .sync_group import (  # Core PR 27
    forget_group_change,
    refuse_if_group_changed,
    stop_for_group_change,
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
    medicine: Record | None = None  # Core PR 26
    # Core PR 26
    # The newest dose of each medicine, by the medicine's ID (or its name when it has none), so
    # the next dose of every medicine is known when two of them alternate.
    medicine_doses: dict[str, Medication] | None = None
    # Core PR 28
    # The newest activity of each activity type the baby has one of, by the type's ID.
    activities: dict[str, OtherActivity] | None = None
    # Core PR 29
    # The newest temperature with a reading, and the newest growth record with each measurement.
    # A checkup can record a weight alone, so each measurement has its own newest record.
    temperature: Record | None = None
    weight: Record | None = None  # Core PR 29
    length: Record | None = None  # Core PR 29
    head: Record | None = None  # Core PR 29
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
    # Core PR 28
    # The name of each activity type a baby has an activity of, by the type's ID.
    activity_names: dict[str, str] | None = None


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


# Core PR 28
def activity_type_id(activity: OtherActivity) -> str | None:
    """Return the ID of an activity's type, which the activity embeds, or None without one."""
    desc: object = activity.desc
    type_id = desc.get("objectID") if isinstance(desc, dict) else None
    return type_id if isinstance(type_id, str) and type_id.strip() else None


# Core PR 28
def _activity_order(activity: OtherActivity) -> tuple[float, float, str]:
    """Order activities by time, then when they were saved, then by record ID."""
    if TYPE_CHECKING:
        # Only activities with a time are ordered.
        assert activity.time is not None
    stamp = activity.timestamp.timestamp() if activity.timestamp is not None else 0.0
    return (activity.time.timestamp(), stamp, activity.object_id)


# Core PR 29
def _finite(reading: Measurement | None) -> Measurement | None:
    """Return a reading whose number is finite: a payload's JSON can carry NaN or Infinity."""
    return reading if reading is not None and math.isfinite(reading.value) else None


# Core PR 29
def temperature_reading(record: Record | None) -> Measurement | None:
    """Return a temperature record's reading in °F or °C, or None when it has none."""
    return _finite(record.reading) if isinstance(record, Temperature) else None


# Core PR 29
def weight_reading(record: Record | None) -> Measurement | None:
    """Return a growth record's weight in pounds or kilograms, or None when it has none."""
    return _finite(record.weight_reading) if isinstance(record, Growth) else None


# Core PR 29
def length_reading(record: Record | None) -> Measurement | None:
    """Return a growth record's length in inches or centimeters, or None when it has none."""
    return _finite(record.length_reading) if isinstance(record, Growth) else None


# Core PR 29
def head_reading(record: Record | None) -> Measurement | None:
    """Return a growth record's head circumference in inches or centimeters, or None."""
    return _finite(record.head_reading) if isinstance(record, Growth) else None


# Core PR 29
def _reading_order(record: Record) -> tuple[float, float, str]:
    """Order readings by time, then when they were saved, then by record ID."""
    if TYPE_CHECKING:
        # Only records with a time are ordered.
        assert record.time is not None
    stamp = record.timestamp.timestamp() if record.timestamp is not None else 0.0
    return (record.time.timestamp(), stamp, record.object_id)


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
        self.group_changed = False  # Core PR 27
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
        if self.group_changed:  # Core PR 27
            # Nothing is sent: a client that met a new sync group refuses it until it's replaced.
            raise ConfigEntryError(translation_domain=DOMAIN, translation_key="account_changed")
        before = dict(self.engine.last_seen)
        was_seeded = self.engine.seeded  # Core PR 27
        try:
            await self.engine.sync(self.client)
        except AuthenticationError as err:
            raise ConfigEntryAuthFailed(
                translation_domain=DOMAIN, translation_key="auth_failed"
            ) from err
        except BabyTrackerError as err:
            if stop_for_group_change(self, err):  # Core PR 27
                raise ConfigEntryError(
                    translation_domain=DOMAIN, translation_key="account_changed"
                ) from err
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="sync_failed_update",
                translation_placeholders={"error": str(err)},
            ) from err
        finally:
            # Keep progress even when a later device failed.
            if dict(self.engine.last_seen) != before:
                await self._async_save()
            # Core PR 27
            # Reading the account's seed adds its records without moving any log forward, and
            # a sync that fails after reading it must still keep it.
            if self.engine.seeded != was_seeded and dict(self.engine.last_seen) == before:
                await self._async_save()
        # Core PR 27
        # A sync that went through means the account is in one sync group again.
        forget_group_change(self.hass, self.config_entry.entry_id)
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
                medicine=latest.get("Medication"),  # Core PR 26
                medicine_doses=self._medicine_doses(baby_id),  # Core PR 26
                activities=self._activities(baby_id),  # Core PR 28
                temperature=self._newest_reading(baby_id, temperature_reading),  # Core PR 29
                weight=self._newest_reading(baby_id, weight_reading),  # Core PR 29
                length=self._newest_reading(baby_id, length_reading),  # Core PR 29
                head=self._newest_reading(baby_id, head_reading),  # Core PR 29
                last_nursing=self._last_nursing(latest),  # Core PR 4
            )
        return BabyTrackerData(
            babies=babies,
            last_pump=self.engine.latest_pump(),  # Core PR 5
            pump=self.timers.pump,  # Core PR 21
            last_breast=last_breast(self.engine.records.values(), dt_util.utcnow()),  # Core PR 7
            activity_names=self._activity_names(babies),  # Core PR 28
        )

    # Core PR 29
    def _newest_reading(
        self, baby_id: str, reading: Callable[[Record | None], Measurement | None]
    ) -> Record | None:
        """Return the baby's newest record that has the reading, or None."""
        return max(
            (
                record
                for record in self.engine.records.values()
                if record.baby_id == baby_id
                and record.time is not None
                and reading(record) is not None
            ),
            key=_reading_order,
            default=None,
        )

    # Core PR 28
    def _activities(self, baby_id: str) -> dict[str, OtherActivity]:
        """Return the newest activity of each type the baby has one of."""
        newest: dict[str, OtherActivity] = {}
        for record in self.engine.records.values():
            if not isinstance(record, OtherActivity) or record.baby_id != baby_id:
                continue
            type_id = activity_type_id(record)
            if type_id is None or record.time is None:
                continue
            kept = newest.get(type_id)
            if kept is None or _activity_order(record) > _activity_order(kept):
                newest[type_id] = record
        return newest

    # Core PR 28
    def _activity_names(self, babies: dict[str, BabyData]) -> dict[str, str]:
        """Return the name of each activity type the babies have an activity of.

        The library chooses the current name (a type synced on its own wins over the copies
        activities embed), so a type renamed in the app is named anew. A type without a name is
        named by its ID.
        """
        names = {choice.object_id: choice.name for choice in self.engine.activity_choices()}
        return {
            type_id: (names.get(type_id) or "").strip() or type_id
            for baby in babies.values()
            for type_id in baby.activities or {}
        }

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
        refuse_if_group_changed(self)  # Core PR 27
        try:
            try:
                await self.engine.sync(self.client)
            except AuthenticationError:
                raise
            except BabyTrackerError as err:
                if stop_for_group_change(self, err):  # Core PR 27
                    raise HomeAssistantError(
                        translation_domain=DOMAIN, translation_key="account_changed"
                    ) from err
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
