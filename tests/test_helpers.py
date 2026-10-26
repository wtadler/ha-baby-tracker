"""Reading amounts and fields from records, typed or raw."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from custom_components.nighp_baby_tracker.helpers import record_field, volume_of
from custom_components.nighp_baby_tracker.pybabytracker import (
    Formula,
    Pump,
    decode_payload,
)

from .factories import BABY_ONE_ID, at, baby_payload, record_payload, volume

BABY = baby_payload("Baby One", BABY_ONE_ID)
OBJECT_ID = "F0000000-0000-4000-8000-000000000001"


def _payload(type_name: str = "Formula", **fields: Any) -> dict[str, Any]:
    return record_payload(type_name, OBJECT_ID, when=at(0), baby=BABY, **fields)


def test_reads_a_sync_format_volume_in_ounces() -> None:
    record = decode_payload(_payload(amount=volume(4)))
    assert volume_of(record) == (4, "oz")


def test_reads_a_sync_format_volume_in_milliliters() -> None:
    record = decode_payload(_payload(amount=volume(120, english=False)))
    assert volume_of(record) == (120, "mL")


def test_reads_a_plain_number_with_the_database_scale_flag() -> None:
    record = Formula(object_id=OBJECT_ID, amount=4.5, is_english_scale=1)
    assert volume_of(record) == (4.5, "oz")
    metric = Formula(object_id=OBJECT_ID, amount=90.0, is_english_scale=0)
    assert volume_of(metric) == (90.0, "mL")


def test_reads_a_plain_number_with_no_scale_flag_without_a_unit() -> None:
    record = Formula(object_id=OBJECT_ID, amount=4.5)
    assert volume_of(record) == (4.5, None)


def test_a_missing_amount_is_nothing() -> None:
    assert volume_of(Formula(object_id=OBJECT_ID)) == (None, None)


def test_reads_a_second_amount_field_by_its_python_name() -> None:
    record = decode_payload(
        _payload("Pump", amount=volume(5), leftAmount=volume(2), rightAmount=volume(3))
    )
    assert isinstance(record, Pump)
    assert volume_of(record, "left_amount") == (2, "oz")
    assert volume_of(record, "right_amount") == (3, "oz")


def test_reads_a_volume_from_the_raw_payload_of_a_record_kept_raw() -> None:
    record = decode_payload(_payload(amount=volume(6)) | {"time": "not a time"})
    assert not isinstance(record, Formula)  # kept raw because the time didn't parse
    assert volume_of(record) == (6, "oz")


def test_reads_a_volume_from_the_extra_payload_when_the_model_lacks_the_field() -> None:
    record = decode_payload(_payload("Diaper", status=1, amount=volume(2)))
    assert volume_of(record) == (2, "oz")


def test_reads_a_future_typed_volume_object() -> None:
    record = SimpleNamespace(amount=SimpleNamespace(value=3, english_measure=True), extra={})
    assert volume_of(record) == (3, "oz")
    metric = SimpleNamespace(amount=SimpleNamespace(value=90, english_measure=False), extra={})
    assert volume_of(metric) == (90, "mL")


@pytest.mark.parametrize("bad", ["4", True, [], {"value": "x"}, {"englishMeasure": "true"}])
def test_ignores_values_it_cannot_read_as_a_number(bad: Any) -> None:
    record = decode_payload(_payload(amount=bad))
    assert volume_of(record) == (None, None)


def test_record_field_reads_typed_and_raw_records() -> None:
    typed = decode_payload(_payload("Diaper", status=2))
    assert record_field(typed, "status") == 2
    raw = decode_payload(_payload("Diaper", status=1) | {"time": "not a time"})
    assert record_field(raw, "status") == 1
    assert record_field(raw, "left_duration") is None


def test_record_field_converts_python_names_to_payload_keys() -> None:
    raw = decode_payload(_payload("Nursing", leftDuration=7) | {"time": "not a time"})
    assert record_field(raw, "left_duration") == 7
