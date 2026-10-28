"""Core PR 4: the library does not check payload types at runtime, so a record can carry a value
of the wrong type. The attributes read it as no value instead of leaking it or raising."""

from typing import Any

import pytest
from pybabytracker import Diaper, Nursing, Sleep

from custom_components.baby_tracker.sensor import (
    _diaper_attributes,
    _feed_attributes,
    _sleep_attributes,
)

OBJECT_ID = "A0000000-0000-4000-8000-000000000001"


@pytest.mark.parametrize("note", [5, ["a"], {"a": 1}, 1.5])
def test_a_note_that_is_not_a_string_reads_as_no_note(note: Any) -> None:
    assert _sleep_attributes(Sleep(object_id=OBJECT_ID, note=note))["note"] is None
    assert _diaper_attributes(Diaper(object_id=OBJECT_ID, note=note))["note"] is None
    assert _feed_attributes(Nursing(object_id=OBJECT_ID, note=note))["note"] is None


@pytest.mark.parametrize("code", [["x"], {"a": 1}, "0", 1.5])
def test_a_diaper_status_that_is_not_an_int_reads_as_no_type(code: Any) -> None:
    assert _diaper_attributes(Diaper(object_id=OBJECT_ID, status=code))["type"] is None


@pytest.mark.parametrize("code", [["x"], {"a": 1}, "0", 1.5])
def test_a_finish_side_that_is_not_an_int_reads_as_no_side(code: Any) -> None:
    assert _feed_attributes(Nursing(object_id=OBJECT_ID, finish_side=code))["finish_side"] is None
