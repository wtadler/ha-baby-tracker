"""Turning the library's volumes into attribute values."""

from __future__ import annotations

from typing import Any

from homeassistant.const import UnitOfVolume

from custom_components.nighp_baby_tracker.helpers import volume_parts
from custom_components.nighp_baby_tracker.pybabytracker import Formula, Pump, Volume, decode_payload

from .factories import BABY_ONE_ID, at, baby_payload, record_payload, volume

BABY = baby_payload("Baby One", BABY_ONE_ID)
OBJECT_ID = "F0000000-0000-4000-8000-000000000001"


def _record(type_name: str = "Formula", **fields: Any):
    return decode_payload(record_payload(type_name, OBJECT_ID, when=at(0), baby=BABY, **fields))


def test_the_unit_attribute_values_are_home_assistants_own_constants() -> None:
    """The attribute is an automation contract, so pin the strings."""
    assert UnitOfVolume.FLUID_OUNCES == "fl. oz."
    assert UnitOfVolume.MILLILITERS == "mL"
    assert volume_parts(Volume(4, "oz")) == (4, "fl. oz.")
    assert volume_parts(Volume(90, "ml")) == (90, "mL")


def test_no_volume_is_nothing() -> None:
    assert volume_parts(None) == (None, None)


def test_a_unit_it_does_not_know_keeps_the_value_without_a_unit() -> None:
    assert volume_parts(Volume(3, "cups")) == (3, None)


def test_reads_a_sync_format_volume_through_the_typed_model() -> None:
    record = _record(amount=volume(4))
    assert isinstance(record, Formula)
    assert volume_parts(record.volume) == (4, "fl. oz.")
    metric = _record(amount=volume(120, english=False))
    assert volume_parts(metric.volume) == (120, "mL")


def test_reads_each_pump_amount() -> None:
    record = _record("Pump", amount=volume(5), leftAmount=volume(2), rightAmount=volume(3))
    assert isinstance(record, Pump)
    assert volume_parts(record.left_volume) == (2, "fl. oz.")
    assert volume_parts(record.right_volume) == (3, "fl. oz.")
