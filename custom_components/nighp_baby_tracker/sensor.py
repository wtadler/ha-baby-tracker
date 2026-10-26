"""Last-activity, today, and timer sensors, one set per baby, and the account's last pump."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
)
from homeassistant.const import UnitOfVolume
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util.unit_conversion import VolumeConverter

from .const import ACCOUNT_DEVICE_MODEL, ACCOUNT_DEVICE_NAME, DOMAIN, account_identifier
from .coordinator import BabyData, BabyTrackerConfigEntry, BabyTrackerCoordinator
from .helpers import volume_parts
from .pybabytracker import Record
from .today_entity import BabyTrackerBabyEntity
from .totals import DayTotals

PARALLEL_UPDATES = 0

DIAPER_TYPES = {0: "wet", 1: "dirty", 2: "mixed"}
FEED_KINDS = {"Nursing": "nursing", "Formula": "formula", "Pumped": "pumped"}
SIDES = {0: "left", 1: "right"}


def _present(attributes: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in attributes.items() if value is not None}


def _note(record: Record) -> str | None:
    note = getattr(record, "note", None)
    return note if isinstance(note, str) and note else None


def _diaper_attributes(record: Record) -> dict[str, Any]:
    return _present(
        {"type": DIAPER_TYPES.get(getattr(record, "status", None)), "note": _note(record)}
    )


def _feed_attributes(record: Record) -> dict[str, Any]:
    kind = FEED_KINDS.get(record.record_type)
    attributes: dict[str, Any] = {"kind": kind}
    if kind == "nursing":
        attributes["left_duration_minutes"] = getattr(record, "left_duration", None)
        attributes["right_duration_minutes"] = getattr(record, "right_duration", None)
        attributes["both_duration_minutes"] = getattr(record, "both_duration", None)
        attributes["finish_side"] = SIDES.get(getattr(record, "finish_side", None))
    else:
        attributes["amount"], attributes["unit"] = volume_parts(getattr(record, "volume", None))
    attributes["note"] = _note(record)
    return _present(attributes)


def _pump_attributes(record: Record) -> dict[str, Any]:
    amount, unit = volume_parts(getattr(record, "volume", None))
    return _present(
        {
            "amount": amount,
            "unit": unit,
            "left_amount": volume_parts(getattr(record, "left_volume", None))[0],
            "right_amount": volume_parts(getattr(record, "right_volume", None))[0],
            "left_duration_minutes": getattr(record, "left_duration", None),
            "right_duration_minutes": getattr(record, "right_duration", None),
            "sides": getattr(record, "sides", None),
            "note": _note(record),
        }
    )


def _sleep_attributes(record: Record) -> dict[str, Any]:
    return _present({"duration_minutes": getattr(record, "duration", None), "note": _note(record)})


@dataclass(frozen=True, kw_only=True)
class BabyTrackerSensorDescription(SensorEntityDescription):
    """A sensor for one kind of record."""

    record_fn: Callable[[BabyData], Record | None]
    attributes_fn: Callable[[Record], dict[str, Any]]


SENSORS: tuple[BabyTrackerSensorDescription, ...] = (
    BabyTrackerSensorDescription(
        key="last_diaper",
        translation_key="last_diaper",
        device_class=SensorDeviceClass.TIMESTAMP,
        record_fn=lambda baby: baby.diaper,
        attributes_fn=_diaper_attributes,
    ),
    BabyTrackerSensorDescription(
        key="last_feed",
        translation_key="last_feed",
        device_class=SensorDeviceClass.TIMESTAMP,
        record_fn=lambda baby: baby.feed,
        attributes_fn=_feed_attributes,
    ),
    BabyTrackerSensorDescription(
        key="last_sleep",
        translation_key="last_sleep",
        device_class=SensorDeviceClass.TIMESTAMP,
        record_fn=lambda baby: baby.sleep,
        attributes_fn=_sleep_attributes,
    ),
)


LAST_PUMP = SensorEntityDescription(
    key="last_pump",
    translation_key="last_pump",
    device_class=SensorDeviceClass.TIMESTAMP,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BabyTrackerConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the account's last-pump sensor, the sensors for every baby, and those for babies that
    appear in later syncs."""
    coordinator = entry.runtime_data
    async_add_entities([BabyTrackerLastPumpSensor(coordinator, entry)])
    known: set[str] = set()

    @callback
    def _add_new_babies() -> None:
        new = [baby_id for baby_id in coordinator.data if baby_id not in known]
        known.update(new)
        async_add_entities(
            BabyTrackerSensor(coordinator, description, baby_id)
            for baby_id in new
            for description in SENSORS
        )
        async_add_entities(
            BabyTrackerTodaySensor(coordinator, description, baby_id)
            for baby_id in new
            for description in TODAY_SENSORS
        )
        async_add_entities(
            BabyTrackerTimerSensor(coordinator, description, baby_id)
            for baby_id in new
            for description in TIMER_SENSORS
        )

    _add_new_babies()
    entry.async_on_unload(coordinator.async_add_listener(_add_new_babies))


class BabyTrackerSensor(CoordinatorEntity[BabyTrackerCoordinator], SensorEntity):
    """When a baby's last diaper, feed, pump, or sleep happened, with its details."""

    _attr_has_entity_name = True
    entity_description: BabyTrackerSensorDescription

    def __init__(
        self,
        coordinator: BabyTrackerCoordinator,
        description: BabyTrackerSensorDescription,
        baby_id: str,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._baby_id = baby_id
        # Same scope as the device identifier: the baby's ID. One baby is in one sync group.
        self._attr_unique_id = f"{baby_id}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, baby_id)},
            name=coordinator.data[baby_id].name or "Baby",
            manufacturer="Nighp",
            model="Baby Tracker",
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def _record(self) -> Record | None:
        baby = self.coordinator.data.get(self._baby_id)
        return self.entity_description.record_fn(baby) if baby is not None else None

    @property
    def available(self) -> bool:
        return super().available and self._baby_id in self.coordinator.data

    @property
    def native_value(self) -> datetime | None:
        record = self._record
        return record.time if record is not None else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        record = self._record
        return self.entity_description.attributes_fn(record) if record is not None else None


class BabyTrackerLastPumpSensor(CoordinatorEntity[BabyTrackerCoordinator], SensorEntity):
    """When the account's last pump happened, with its details.

    A pump belongs to the account, not a baby, so this sits on an account-level device (one per
    config entry) and shows the newest pump whoever logged it.
    """

    _attr_has_entity_name = True
    entity_description = LAST_PUMP

    def __init__(self, coordinator: BabyTrackerCoordinator, entry: BabyTrackerConfigEntry) -> None:
        super().__init__(coordinator)
        # The entry's ID scopes it, as a baby's ID scopes a baby's sensors.
        self._attr_unique_id = f"{entry.entry_id}_{LAST_PUMP.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, account_identifier(entry.entry_id))},
            name=ACCOUNT_DEVICE_NAME,
            manufacturer="Nighp",
            model=ACCOUNT_DEVICE_MODEL,
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def native_value(self) -> datetime | None:
        pump = self.coordinator.last_pump
        return pump.time if pump is not None else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        pump = self.coordinator.last_pump
        return _pump_attributes(pump) if pump is not None else None


@dataclass(frozen=True, kw_only=True)
class BabyTrackerTodaySensorDescription(SensorEntityDescription):
    """A sensor that totals a baby's local calendar day."""

    value_fn: Callable[[DayTotals], float | int]
    attributes_fn: Callable[[DayTotals], dict[str, Any]] | None = None


TODAY_SENSORS: tuple[BabyTrackerTodaySensorDescription, ...] = (
    BabyTrackerTodaySensorDescription(
        key="diapers_today",
        translation_key="diapers_today",
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
        value_fn=lambda totals: totals.feeds,
    ),
    BabyTrackerTodaySensorDescription(
        key="bottle_volume_today",
        translation_key="bottle_volume_today",
        device_class=SensorDeviceClass.VOLUME,
        # The value is in milliliters; the entity converts it to the unit system's unit.
        value_fn=lambda totals: totals.bottle_milliliters,
    ),
)


class BabyTrackerTodaySensor(BabyTrackerBabyEntity, SensorEntity):
    """Diapers, feeds, or bottle volume since local midnight."""

    entity_description: BabyTrackerTodaySensorDescription

    @property
    def native_unit_of_measurement(self) -> str | None:
        if self.entity_description.device_class is not SensorDeviceClass.VOLUME:
            return None
        # Home Assistant's US customary system measures volume in gallons: too big for a bottle.
        if self.hass.config.units.volume_unit == UnitOfVolume.GALLONS:
            return UnitOfVolume.FLUID_OUNCES
        return UnitOfVolume.MILLILITERS

    @property
    def native_value(self) -> float | int | None:
        baby = self._baby
        if baby is None:
            return None
        value = self.entity_description.value_fn(baby.today)
        unit = self.native_unit_of_measurement
        if unit is None:
            return value
        return round(VolumeConverter.convert(value, UnitOfVolume.MILLILITERS, unit), 2)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        baby = self._baby
        attributes_fn = self.entity_description.attributes_fn
        return attributes_fn(baby.today) if baby is not None and attributes_fn else None


def _sleep_timer(baby: BabyData) -> tuple[datetime | None, dict[str, Any] | None]:
    sleep = baby.running_sleep
    if sleep is None:
        return None, None
    return sleep.time, {"object_id": sleep.object_id}


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


@dataclass(frozen=True, kw_only=True)
class BabyTrackerTimerSensorDescription(SensorEntityDescription):
    """A running timer: its start, or unknown when none runs."""

    timer_fn: Callable[[BabyData], tuple[datetime | None, dict[str, Any] | None]]


TIMER_SENSORS: tuple[BabyTrackerTimerSensorDescription, ...] = (
    # The newest sleep while it runs, from any device.
    BabyTrackerTimerSensorDescription(
        key="sleep_timer",
        translation_key="sleep_timer",
        device_class=SensorDeviceClass.TIMESTAMP,
        timer_fn=_sleep_timer,
    ),
    # Home Assistant's own nursing timer (a phone's doesn't sync until it stops). The attributes
    # are what a live activity needs: the side now, when it started, and the seconds finished on
    # each side before it (the current side's time is now minus side_started).
    BabyTrackerTimerSensorDescription(
        key="nursing_timer",
        translation_key="nursing_timer",
        device_class=SensorDeviceClass.TIMESTAMP,
        timer_fn=_nursing_timer,
    ),
)


class BabyTrackerTimerSensor(BabyTrackerBabyEntity, SensorEntity):
    """When a baby's running sleep or nursing timer started, or unknown when none runs."""

    entity_description: BabyTrackerTimerSensorDescription

    @property
    def native_value(self) -> datetime | None:
        baby = self._baby
        return self.entity_description.timer_fn(baby)[0] if baby is not None else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        baby = self._baby
        return self.entity_description.timer_fn(baby)[1] if baby is not None else None
