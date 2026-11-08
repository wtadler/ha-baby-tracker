"""Sensors for Baby Tracker (Nighp)."""

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import (
    datetime,
    timedelta,  # Core PR 19
)
from typing import (
    Any,  # Core PR 4
    override,
)

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,  # Core PR 7
)
from homeassistant.const import (  # Core PR 4
    UnitOfTime,
    UnitOfVolume,  # Core PR 7
)
from homeassistant.core import (
    HomeAssistant,
    callback,  # Core PR 6
)
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_track_time_interval  # Core PR 19
from homeassistant.util import dt as dt_util  # Core PR 19
from homeassistant.util.unit_conversion import VolumeConverter  # Core PR 7
from pybabytracker import (
    SIDE_LEFT,  # Core PR 4
    SIDE_RIGHT,  # Core PR 4
    DayTotals,  # Core PR 7
    Diaper,  # Core PR 4
    Formula,  # Core PR 4
    Nursing,  # Core PR 4
    Pump,  # Core PR 5
    Pumped,  # Core PR 4
    Record,
    Sleep,  # Core PR 4
    numeric_code,  # Core PR 4
)
from pybabytracker.timers import LEFT, RIGHT  # Core PR 19

from .coordinator import BabyData, BabyTrackerConfigEntry, BabyTrackerCoordinator
from .entity import (
    BabyTrackerAccountEntity,  # Core PR 5
    BabyTrackerBabyEntity,
)
from .helpers import volume_parts  # Core PR 4

PARALLEL_UPDATES = 0

DIAPER_TYPES = {0: "wet", 1: "dirty", 2: "mixed"}  # Core PR 4
FEED_KINDS = ((Nursing, "nursing"), (Formula, "formula"), (Pumped, "pumped"))  # Core PR 4
SIDES = {SIDE_LEFT: "left", SIDE_RIGHT: "right"}  # Core PR 4


# Core PR 4
def _note(record: Record | None) -> str | None:
    note: object = record.note if record is not None else None
    # The library doesn't check payload types at runtime, so the annotation can't be trusted.
    return note if isinstance(note, str) and note else None


# Core PR 4
def _label(labels: dict[int, str], code: object) -> str | None:
    """Return the label for a record's numeric code, or None when it has no known code."""
    # iOS sends a code as an int and Android as a digit string. The payload is unvalidated at
    # runtime, so anything else can arrive too, and numeric_code reads that as no code.
    number = numeric_code(code)
    return labels.get(number) if number is not None else None


# Core PR 4
def _diaper_attributes(record: Record | None) -> dict[str, Any]:
    status = record.status if isinstance(record, Diaper) else None
    return {"type": _label(DIAPER_TYPES, status), "note": _note(record)}


# Core PR 4
def _feed_attributes(record: Record | None) -> dict[str, Any]:
    # A NursingSession is a Nursing, so it reads as "nursing" here.
    kind = next((name for feed, name in FEED_KINDS if isinstance(record, feed)), None)
    left = right = both = finish_side = None
    if isinstance(record, Nursing):
        left, right, both = record.left_duration, record.right_duration, record.both_duration
        finish_side = record.finish_side
    amount, unit = volume_parts(record.volume if isinstance(record, Formula | Pumped) else None)
    return {
        "kind": kind,
        "left_duration_minutes": left,
        "right_duration_minutes": right,
        "both_duration_minutes": both,
        "finish_side": _label(SIDES, finish_side),
        "amount": amount,
        "unit": unit,
        "note": _note(record),
    }


# Core PR 5
def _pump_attributes(record: Record | None) -> dict[str, Any]:
    pump = record if isinstance(record, Pump) else None
    amount, unit = volume_parts(pump.volume if pump else None)
    if amount == 0:
        # A timed pump saved with no volume carries an amount of 0: that is no volume, not 0 fl. oz.
        amount = unit = None
    return {
        "amount": amount,
        "unit": unit,
        "left_amount": volume_parts(pump.left_volume if pump else None)[0],
        "right_amount": volume_parts(pump.right_volume if pump else None)[0],
        "left_duration_minutes": pump.left_duration if pump else None,
        "right_duration_minutes": pump.right_duration if pump else None,
        "sides": pump.sides if pump else None,
        "note": _note(pump),
    }


# Core PR 4
def _sleep_attributes(record: Record | None) -> dict[str, Any]:
    duration = record.duration if isinstance(record, Sleep) else None
    return {"duration_minutes": duration, "note": _note(record)}


@dataclass(frozen=True, kw_only=True)
class BabyTrackerSensorDescription(SensorEntityDescription):
    """A sensor for one kind of record."""

    record_fn: Callable[[BabyData], Record | None]
    attributes_fn: Callable[[Record | None], dict[str, Any]]  # Core PR 4


SENSORS: tuple[BabyTrackerSensorDescription, ...] = (
    BabyTrackerSensorDescription(
        key="last_diaper",
        translation_key="last_diaper",
        device_class=SensorDeviceClass.TIMESTAMP,
        record_fn=lambda baby: baby.diaper,
        attributes_fn=_diaper_attributes,  # Core PR 4
    ),
    BabyTrackerSensorDescription(
        key="last_feed",
        translation_key="last_feed",
        device_class=SensorDeviceClass.TIMESTAMP,
        record_fn=lambda baby: baby.feed,
        attributes_fn=_feed_attributes,  # Core PR 4
    ),
    BabyTrackerSensorDescription(
        key="last_sleep",
        translation_key="last_sleep",
        device_class=SensorDeviceClass.TIMESTAMP,
        record_fn=lambda baby: baby.sleep,
        attributes_fn=_sleep_attributes,  # Core PR 4
    ),
)


# Core PR 5
LAST_PUMP = SensorEntityDescription(
    key="last_pump",
    translation_key="last_pump",
    device_class=SensorDeviceClass.TIMESTAMP,
)


class BabyTrackerSensor(BabyTrackerBabyEntity, SensorEntity):
    """When a baby's last diaper, feed, or sleep happened."""

    entity_description: BabyTrackerSensorDescription

    @property
    def _record(self) -> Record | None:
        baby = self._baby
        return self.entity_description.record_fn(baby) if baby is not None else None

    @property
    @override
    def native_value(self) -> datetime | None:
        """Return the time of the baby's newest matching record."""
        record = self._record
        return record.time if record is not None else None

    # Core PR 4
    @property
    @override
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the attributes of the baby's newest matching record, each None when unset."""
        return self.entity_description.attributes_fn(self._record)


# Core PR 5
class BabyTrackerLastPumpSensor(BabyTrackerAccountEntity, SensorEntity):
    """When the account's last pump happened, with its details.

    A pump belongs to the account, not a baby, so this sits on an account-level device (one per
    config entry) and shows the newest pump whoever logged it.
    """

    @property
    @override
    def native_value(self) -> datetime | None:
        """Return the time of the last pump."""
        pump = self.coordinator.data.last_pump
        return pump.time if pump is not None else None

    @property
    @override
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the attributes of the last pump, each None when unset."""
        return _pump_attributes(self.coordinator.data.last_pump)


# Core PR 7
LAST_BREAST = SensorEntityDescription(
    key="last_breast",
    translation_key="last_breast",
    device_class=SensorDeviceClass.ENUM,
    options=[*SIDES.values(), "none"],
)


# Core PR 7
class BabyTrackerLastBreastSensor(BabyTrackerAccountEntity, SensorEntity):
    """The side the newest nursing (any baby) or pump started on.

    It is the side the session began on, as the app stores and shows it, not the side it ended on
    and not the side to use next: a session that started left and ended right reads ``left``. A
    two-sided session logged from Home Assistant stores the side it ended on instead.

    It's the account's, not a baby's: one mother nurses every baby and pumps. ``none`` is a
    session with no side; unknown is no session at all.
    """

    @property
    @override
    def native_value(self) -> str | None:
        """Return the side it started on (left or right) or none, or None before any session."""
        last = self.coordinator.data.last_breast
        if last is None:
            return None
        return _label(SIDES, last.side) or "none"

    @property
    @override
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return whether it was a nursing or a pump, and when."""
        last = self.coordinator.data.last_breast
        return {
            "kind": last.kind if last is not None else None,
            "time": last.time.isoformat() if last is not None else None,
        }


# Core PR 21
PUMP_TIMER = SensorEntityDescription(
    key="pump_timer",
    translation_key="pump_timer",
    device_class=SensorDeviceClass.TIMESTAMP,
)


# Core PR 21
class BabyTrackerPumpTimerSensor(BabyTrackerAccountEntity, SensorEntity):
    """When Home Assistant's pump timer started, or unknown when none runs.

    Like the nursing timer sensor: the app doesn't sync a running pump, so this follows Home
    Assistant's own timer only.
    """

    @property
    @override
    def native_value(self) -> datetime | None:
        """Return the timer's start."""
        timer = self.coordinator.data.pump
        return timer.started if timer is not None else None


# Core PR 7
@dataclass(frozen=True, kw_only=True)
class BabyTrackerTodaySensorDescription(SensorEntityDescription):
    """A sensor that totals a baby's local calendar day."""

    value_fn: Callable[[DayTotals], float | int]
    attributes_fn: Callable[[DayTotals], dict[str, Any]] | None = None


# Core PR 7
TODAY_SENSORS: tuple[BabyTrackerTodaySensorDescription, ...] = (
    BabyTrackerTodaySensorDescription(
        key="diapers_today",
        translation_key="diapers_today",
        state_class=SensorStateClass.TOTAL,
        value_fn=lambda totals: totals.diapers,
        attributes_fn=lambda totals: {
            "wet": totals.wet,
            "dirty": totals.dirty,
            "mixed": totals.mixed,
        },
    ),
    BabyTrackerTodaySensorDescription(
        key="feeds_today",
        translation_key="feeds_today",
        state_class=SensorStateClass.TOTAL,
        value_fn=lambda totals: totals.feeds,
    ),
    BabyTrackerTodaySensorDescription(
        key="bottle_volume_today",
        translation_key="bottle_volume_today",
        device_class=SensorDeviceClass.VOLUME,
        state_class=SensorStateClass.TOTAL,
        # The value is in milliliters; the entity converts it to the unit system's unit.
        value_fn=lambda totals: totals.bottle_milliliters,
    ),
    BabyTrackerTodaySensorDescription(
        key="nursing_minutes_today",
        translation_key="nursing_minutes_today",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.MINUTES,
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=0,
        # An Android timer's NursingSession counts like any nursing.
        value_fn=lambda totals: totals.nursing_minutes,
        attributes_fn=lambda totals: {"nursings": totals.nursings},
    ),
)


# Core PR 7
class BabyTrackerTodaySensor(BabyTrackerBabyEntity, SensorEntity):
    """Diapers, feeds, or bottle volume since local midnight."""

    entity_description: BabyTrackerTodaySensorDescription

    @property
    @override
    def native_unit_of_measurement(self) -> str | None:
        """Return the unit of measurement, avoiding gallons for bottle volumes."""
        if self.entity_description.device_class is not SensorDeviceClass.VOLUME:
            return self.entity_description.native_unit_of_measurement
        # Home Assistant's US customary system measures volume in gallons: too big for a bottle.
        if self.hass.config.units.volume_unit == UnitOfVolume.GALLONS:
            return UnitOfVolume.FLUID_OUNCES
        return UnitOfVolume.MILLILITERS

    @property
    @override
    def last_reset(self) -> datetime | None:
        """Return the start of the local day the totals cover.

        It is the boundary the totals are counted from, so the two can't disagree. A count that
        drops within the day (an undo, a deletion) keeps it, and it moves only at midnight.
        """
        baby = self._baby
        return baby.today.start if baby is not None else None

    @property
    @override
    def native_value(self) -> float | int | None:
        """Return today's value for the baby."""
        baby = self._baby
        if baby is None:
            return None
        value = self.entity_description.value_fn(baby.today)
        unit = self.native_unit_of_measurement
        if unit is None or self.entity_description.device_class is not SensorDeviceClass.VOLUME:
            return value
        return round(VolumeConverter.convert(value, UnitOfVolume.MILLILITERS, unit), 2)

    @property
    @override
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return the attributes for today's value."""
        baby = self._baby
        attributes_fn = self.entity_description.attributes_fn
        return attributes_fn(baby.today) if baby is not None and attributes_fn else None


# Core PR 8
def _sleep_timer(baby: BabyData) -> tuple[datetime | None, dict[str, Any] | None]:
    sleep = baby.running_sleep
    if sleep is None:
        return None, None
    return sleep.time, {"object_id": sleep.object_id}


# Core PR 19
def _nursing_timer(baby: BabyData) -> tuple[datetime | None, dict[str, Any] | None]:
    timer = baby.nursing
    if timer is None:
        return None, None
    return timer.started, {
        "side": timer.side,
        "side_started": timer.side_started.isoformat(),
        "left_seconds": round(timer.left_seconds),
        "right_seconds": round(timer.right_seconds),
    }


# Core PR 8
@dataclass(frozen=True, kw_only=True)
class BabyTrackerTimerSensorDescription(SensorEntityDescription):
    """A running timer: its start, or unknown when none runs."""

    timer_fn: Callable[[BabyData], tuple[datetime | None, dict[str, Any] | None]]


# Core PR 8
# The newest sleep while it runs, from any device.
SLEEP_TIMER = BabyTrackerTimerSensorDescription(
    key="sleep_timer",
    translation_key="sleep_timer",
    device_class=SensorDeviceClass.TIMESTAMP,
    timer_fn=_sleep_timer,
)
# Core PR 19
# Home Assistant's own nursing timer (a phone's doesn't sync until it stops). The attributes are
# what a live activity needs: the side now, when it started, and the seconds finished on each side
# before it (the current side's time is now minus side_started).
NURSING_TIMER = BabyTrackerTimerSensorDescription(
    key="nursing_timer",
    translation_key="nursing_timer",
    device_class=SensorDeviceClass.TIMESTAMP,
    timer_fn=_nursing_timer,
)


# Core PR 8
class BabyTrackerTimerSensor(BabyTrackerBabyEntity, SensorEntity):
    """When a baby's running sleep or nursing timer started, or unknown when none runs."""

    entity_description: BabyTrackerTimerSensorDescription

    @property
    @override
    def native_value(self) -> datetime | None:
        """Return the timer's time for the baby."""
        baby = self._baby
        return self.entity_description.timer_fn(baby)[0] if baby is not None else None

    @property
    @override
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return the timer's attributes for the baby."""
        baby = self._baby
        return self.entity_description.timer_fn(baby)[1] if baby is not None else None


# Core PR 19
# How often a running session's time is written, so a dashboard counts up without any sync.
NURSING_TIME_INTERVAL = timedelta(seconds=30)


# Core PR 19
@dataclass(frozen=True, kw_only=True)
class BabyTrackerNursingTimeSensorDescription(SensorEntityDescription):
    """The time on one side of Home Assistant's nursing session."""

    side: str


# Core PR 19
NURSING_TIME_SENSORS: tuple[BabyTrackerNursingTimeSensorDescription, ...] = tuple(
    BabyTrackerNursingTimeSensorDescription(
        key=f"nursing_{side}_time",
        translation_key=f"nursing_{side}_time",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.MINUTES,
        suggested_display_precision=1,
        side=side,
    )
    for side in (LEFT, RIGHT)
)


# Core PR 19
class BabyTrackerNursingTimeSensor(BabyTrackerBabyEntity, SensorEntity):
    """Minutes on one side this nursing session, the running side's time so far included.

    Unknown while no session runs, as the timer sensors are. The value is worked out from the
    clock, so a timer of its own refreshes it while a session runs (a sync isn't needed).
    """

    entity_description: BabyTrackerNursingTimeSensorDescription

    @override
    async def async_added_to_hass(self) -> None:
        """Count up while a session runs."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_track_time_interval(self.hass, self._async_tick, NURSING_TIME_INTERVAL)
        )

    @callback
    def _async_tick(self, _now: datetime) -> None:
        if self.native_value is not None:
            self.async_write_ha_state()

    @property
    @override
    def native_value(self) -> float | None:
        """Return the minutes on this side, or None when no session runs."""
        baby = self._baby
        if baby is None or baby.nursing is None:
            return None
        left, right = baby.nursing.totals(dt_util.utcnow())
        seconds = left if self.entity_description.side == LEFT else right
        return round(seconds / 60, 1)


# Core PR 4
def _minutes(value: object) -> int | None:
    """Return a duration in whole minutes, or None when the payload has none.

    The library's day totals read minutes the same way: a digit string (Android) counts, and a
    float, a bool, other text, or a negative number is no minutes.
    """
    # The payload is unvalidated at runtime, so a string or list can arrive where an int is typed.
    minutes = numeric_code(value)
    return minutes if minutes is not None and minutes >= 0 else None


# Core PR 4
def _last_nursing(baby: BabyData) -> Nursing | None:
    return baby.last_nursing if isinstance(baby.last_nursing, Nursing) else None


# Core PR 4
def _nursing_time(baby: BabyData) -> datetime | None:
    nursing = _last_nursing(baby)
    return nursing.time if nursing is not None else None


# Core PR 4
def _nursing_minutes(baby: BabyData) -> int | None:
    nursing = _last_nursing(baby)
    if nursing is None:
        return None
    sides = [
        _minutes(minutes)
        for minutes in (nursing.left_duration, nursing.right_duration, nursing.both_duration)
    ]
    known = [minutes for minutes in sides if minutes is not None]
    return sum(known) if known else None


# Core PR 4
def _nursing_left_minutes(baby: BabyData) -> int | None:
    nursing = _last_nursing(baby)
    return _minutes(nursing.left_duration) if nursing is not None else None


# Core PR 4
def _nursing_right_minutes(baby: BabyData) -> int | None:
    nursing = _last_nursing(baby)
    return _minutes(nursing.right_duration) if nursing is not None else None


# Core PR 4
def _sleep_minutes(baby: BabyData) -> int | None:
    sleep = baby.sleep
    # A running sleep's duration of 0 isn't a length yet; the sleep timer sensor covers it.
    if not isinstance(sleep, Sleep) or sleep.is_running:
        return None
    return _minutes(sleep.duration)


# Core PR 4
@dataclass(frozen=True, kw_only=True)
class BabyTrackerLastDetailSensorDescription(SensorEntityDescription):
    """One reading of a baby's newest nursing or sleep."""

    value_fn: Callable[[BabyData], datetime | int | None]


# Core PR 4
# The last feed sensor follows the newest feed of any kind, so a bottle after a nursing hides the
# nursing's minutes: the nursing sensors follow the newest nursing alone. The minutes are their own
# sensors, not only attributes, so they show on the device page, keep history, and trigger
# automations without a template.
LAST_DETAIL_SENSORS: tuple[BabyTrackerLastDetailSensorDescription, ...] = (
    BabyTrackerLastDetailSensorDescription(
        key="last_nursing",
        translation_key="last_nursing",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=_nursing_time,
    ),
    BabyTrackerLastDetailSensorDescription(
        key="last_nursing_duration",
        translation_key="last_nursing_duration",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.MINUTES,
        entity_registry_enabled_default=False,
        value_fn=_nursing_minutes,
    ),
    BabyTrackerLastDetailSensorDescription(
        key="last_nursing_left_time",
        translation_key="last_nursing_left_time",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.MINUTES,
        entity_registry_enabled_default=False,
        value_fn=_nursing_left_minutes,
    ),
    BabyTrackerLastDetailSensorDescription(
        key="last_nursing_right_time",
        translation_key="last_nursing_right_time",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.MINUTES,
        entity_registry_enabled_default=False,
        value_fn=_nursing_right_minutes,
    ),
    BabyTrackerLastDetailSensorDescription(
        key="last_sleep_duration",
        translation_key="last_sleep_duration",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.MINUTES,
        value_fn=_sleep_minutes,
    ),
)


# Core PR 4
class BabyTrackerLastDetailSensor(BabyTrackerBabyEntity, SensorEntity):
    """When a baby's newest nursing happened, or the minutes of its newest nursing or sleep.

    A nursing timed on a phone syncs only once it stops, so the nursing sensors show the last
    finished one. The sleep duration is unknown while the newest sleep still runs.
    """

    entity_description: BabyTrackerLastDetailSensorDescription

    @property
    @override
    def native_value(self) -> datetime | int | None:
        """Return the reading for the baby, or None when there's nothing to read yet."""
        baby = self._baby
        return self.entity_description.value_fn(baby) if baby is not None else None


# One row per kind of sensor: its descriptions and the entity class built from each.
# Core PR 5
ACCOUNT_SENSORS: tuple[
    tuple[tuple[SensorEntityDescription, ...], type[BabyTrackerAccountEntity]], ...
] = (
    ((LAST_PUMP,), BabyTrackerLastPumpSensor),
    ((LAST_BREAST,), BabyTrackerLastBreastSensor),  # Core PR 7
    ((PUMP_TIMER,), BabyTrackerPumpTimerSensor),  # Core PR 21
)
BABY_SENSORS: tuple[
    tuple[tuple[SensorEntityDescription, ...], type[BabyTrackerBabyEntity]], ...
] = (
    (SENSORS, BabyTrackerSensor),
    (TODAY_SENSORS, BabyTrackerTodaySensor),  # Core PR 7
    ((SLEEP_TIMER,), BabyTrackerTimerSensor),  # Core PR 8
    ((NURSING_TIMER,), BabyTrackerTimerSensor),  # Core PR 19
    (NURSING_TIME_SENSORS, BabyTrackerNursingTimeSensor),  # Core PR 19
    (LAST_DETAIL_SENSORS, BabyTrackerLastDetailSensor),  # Core PR 4
)


def _baby_sensors(
    coordinator: BabyTrackerCoordinator, baby_ids: Iterable[str]
) -> list[BabyTrackerBabyEntity]:
    """Return every sensor of each of the babies."""
    return [
        entity_class(coordinator, description, baby_id)
        for baby_id in baby_ids
        for descriptions, entity_class in BABY_SENSORS
        for description in descriptions
    ]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BabyTrackerConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the sensors of every baby on the account."""
    coordinator = entry.runtime_data
    async_add_entities(  # Core PR 5
        entity_class(coordinator, description)
        for descriptions, entity_class in ACCOUNT_SENSORS
        for description in descriptions
    )
    async_add_entities(_baby_sensors(coordinator, coordinator.data.babies))
    # Core PR 6
    # Babies that appear in later syncs get their sensors when they appear.
    known = set(coordinator.data.babies)

    # Core PR 6
    @callback
    def _add_new_babies() -> None:
        # A baby that left (its device is removed with it) is new again if it's added back.
        known.intersection_update(coordinator.data.babies)
        new = [baby_id for baby_id in coordinator.data.babies if baby_id not in known]
        known.update(new)
        async_add_entities(_baby_sensors(coordinator, new))

    entry.async_on_unload(coordinator.async_add_listener(_add_new_babies))  # Core PR 6
