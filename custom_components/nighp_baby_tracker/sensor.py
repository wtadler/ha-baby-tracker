"""Last-activity timestamp sensors, one set per baby."""

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
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import BabyData, BabyTrackerConfigEntry, BabyTrackerCoordinator
from .helpers import record_field, volume_of
from .pybabytracker import Record

PARALLEL_UPDATES = 0

DIAPER_TYPES = {0: "wet", 1: "dirty", 2: "mixed"}
FEED_KINDS = {"Nursing": "nursing", "Formula": "formula", "Pumped": "pumped"}
SIDES = {0: "left", 1: "right"}


def _present(attributes: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in attributes.items() if value is not None}


def _note(record: Record) -> str | None:
    note = record_field(record, "note")
    return note if isinstance(note, str) and note else None


def _diaper_attributes(record: Record) -> dict[str, Any]:
    return _present(
        {"type": DIAPER_TYPES.get(record_field(record, "status")), "note": _note(record)}
    )


def _feed_attributes(record: Record) -> dict[str, Any]:
    kind = FEED_KINDS.get(record.record_type)
    attributes: dict[str, Any] = {"kind": kind}
    if kind == "nursing":
        attributes["left_duration_minutes"] = record_field(record, "left_duration")
        attributes["right_duration_minutes"] = record_field(record, "right_duration")
        attributes["both_duration_minutes"] = record_field(record, "both_duration")
        attributes["finish_side"] = SIDES.get(record_field(record, "finish_side"))
    else:
        attributes["amount"], attributes["unit"] = volume_of(record)
    attributes["note"] = _note(record)
    return _present(attributes)


def _pump_attributes(record: Record) -> dict[str, Any]:
    amount, unit = volume_of(record)
    return _present(
        {
            "amount": amount,
            "unit": unit,
            "left_amount": volume_of(record, "left_amount")[0],
            "right_amount": volume_of(record, "right_amount")[0],
            "left_duration_minutes": record_field(record, "left_duration"),
            "right_duration_minutes": record_field(record, "right_duration"),
            "sides": record_field(record, "sides"),
            "note": _note(record),
        }
    )


def _sleep_attributes(record: Record) -> dict[str, Any]:
    return _present({"duration_minutes": record_field(record, "duration"), "note": _note(record)})


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
        key="last_pump",
        translation_key="last_pump",
        device_class=SensorDeviceClass.TIMESTAMP,
        record_fn=lambda baby: baby.pump,
        attributes_fn=_pump_attributes,
    ),
    BabyTrackerSensorDescription(
        key="last_sleep",
        translation_key="last_sleep",
        device_class=SensorDeviceClass.TIMESTAMP,
        record_fn=lambda baby: baby.sleep,
        attributes_fn=_sleep_attributes,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BabyTrackerConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the sensors for every baby, and for babies that appear in later syncs."""
    coordinator = entry.runtime_data
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
        entry_id = coordinator.config_entry.entry_id
        self._attr_unique_id = f"{entry_id}_{baby_id}_{description.key}"
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
