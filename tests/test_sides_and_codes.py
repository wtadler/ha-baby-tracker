"""Core PR 4: the app numbers a nursing side 0 (not set), 1 (left), 2 (right), iOS sends codes as
ints and Android as digit strings, and Android's timed nursing syncs as NursingSession. The
last-feed and last-diaper attributes read all of that."""

from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from pybabytracker import (
    OP_ADD,  # Core PR 12
    SIDE_LEFT,
    SIDE_NONE,
    SIDE_RIGHT,
    Diaper,
    Formula,
    Nursing,
    NursingSession,
    Pump,  # Core PR 12
)
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.baby_tracker.sensor import (
    SIDES,
    _diaper_attributes,
    _feed_attributes,
)

from . import tick
from .factories import DEVICE_A, at, volume
from .fake_server import FakeServer
from .helpers import (
    call,  # Core PR 10
    nursing_posts,  # Core PR 10
    posted,  # Core PR 12
    state,
)

OBJECT_ID = "A0000000-0000-4000-8000-000000000001"
LAST_FEED = "sensor.baby_one_last_feed"


def _id(number: int) -> str:
    return f"B0000000-0000-4000-8000-{number:012d}"


def test_the_side_labels_follow_the_library_constants() -> None:
    assert SIDES == {SIDE_LEFT: "left", SIDE_RIGHT: "right"}
    assert SIDE_NONE not in SIDES


@pytest.mark.parametrize(
    ("code", "label"),
    [
        (0, None),
        (1, "left"),
        (2, "right"),
        ("0", None),
        ("1", "left"),
        ("2", "right"),
        ("x", None),
        (None, None),
        (True, None),
        (3, None),
        (-1, None),
        (1.0, None),
        (" 1", None),
        ("", None),
        (["1"], None),
    ],
)
def test_a_finish_side_reads_as_its_label_whatever_the_platform_sent(
    code: Any, label: str | None
) -> None:
    record = Nursing(object_id=OBJECT_ID, finish_side=code)

    assert _feed_attributes(record)["finish_side"] == label


@pytest.mark.parametrize(
    ("code", "label"),
    [
        (0, "wet"),
        (1, "dirty"),
        (2, "mixed"),
        ("0", "wet"),
        ("1", "dirty"),
        ("2", "mixed"),
        ("x", None),
        (None, None),
        (True, None),
        (3, None),
        (1.5, None),
    ],
)
def test_a_diaper_type_reads_as_its_label_for_an_int_or_a_digit_string(
    code: Any, label: str | None
) -> None:
    assert _diaper_attributes(Diaper(object_id=OBJECT_ID, status=code))["type"] == label


def test_a_nursing_session_reads_as_a_nursing_feed_with_its_side() -> None:
    record = NursingSession(
        object_id=OBJECT_ID,
        left_duration=4,
        right_duration=6,
        both_duration=0,
        finish_side="2",
    )

    attributes = _feed_attributes(record)

    assert attributes["kind"] == "nursing"
    assert attributes["finish_side"] == "right"
    assert attributes["left_duration_minutes"] == 4
    assert attributes["right_duration_minutes"] == 6
    assert attributes["both_duration_minutes"] == 0


def test_a_record_that_is_not_a_feed_has_no_kind() -> None:
    assert _feed_attributes(Diaper(object_id=OBJECT_ID))["kind"] is None
    assert _feed_attributes(None)["kind"] is None


async def test_a_nursing_session_as_the_newest_feed_is_the_last_feed(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    server.add_record(DEVICE_A, "Formula", _id(1), when=at(20), amount=volume(4))
    server.add_record(
        DEVICE_A,
        "NursingSession",
        _id(2),
        when=at(40),
        leftDuration=7,
        rightDuration=12,
        bothDuration=0,
        finishSide="1",
    )
    await tick(hass)

    last_feed = state(hass, LAST_FEED)
    assert last_feed.attributes["kind"] == "nursing"
    assert last_feed.attributes["finish_side"] == "left"
    assert last_feed.attributes["left_duration_minutes"] == 7
    assert last_feed.attributes["right_duration_minutes"] == 12


async def test_a_nursing_session_older_than_a_formula_is_not_the_last_feed(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    server.add_record(
        DEVICE_A, "NursingSession", _id(3), when=at(20), leftDuration=7, finishSide="2"
    )
    server.add_record(DEVICE_A, "Formula", _id(4), when=at(40), amount=volume(4))
    await tick(hass)

    last_feed = state(hass, LAST_FEED)
    assert last_feed.attributes["kind"] == "formula"
    assert last_feed.attributes["finish_side"] is None


async def test_a_nursing_session_and_a_nursing_record_compete_by_time(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    server.add_record(DEVICE_A, "Nursing", _id(5), when=at(40), leftDuration=3, finishSide=2)
    server.add_record(
        DEVICE_A, "NursingSession", _id(6), when=at(20), leftDuration=9, finishSide="1"
    )
    await tick(hass)

    last_feed = state(hass, LAST_FEED)
    assert last_feed.attributes["left_duration_minutes"] == 3
    assert last_feed.attributes["finish_side"] == "right"


async def test_an_android_digit_string_side_labels_the_last_feed(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    server.add_record(DEVICE_A, "Nursing", _id(7), when=at(40), finishSide="2")
    await tick(hass)

    assert state(hass, LAST_FEED).attributes["finish_side"] == "right"


async def test_a_diaper_with_a_digit_string_status_labels_the_last_diaper(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    server.add_record(DEVICE_A, "Diaper", _id(8), when=at(40), status="2")
    await tick(hass)

    assert state(hass, "sensor.baby_one_last_diaper").attributes["type"] == "mixed"


async def test_a_formula_is_still_a_feed_alongside_a_nursing_session(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    server.add_record(DEVICE_A, "NursingSession", _id(9), when=at(10), finishSide="1")
    server.add_record(DEVICE_A, "Pumped", _id(10), when=at(30), amount=volume(3))
    await tick(hass)

    assert state(hass, LAST_FEED).attributes["kind"] == "pumped"


def test_the_feed_classes_the_sensors_read_include_a_nursing_session() -> None:
    # A NursingSession is a Nursing, so an isinstance check covers it without naming it.
    assert issubclass(NursingSession, Nursing)
    assert not issubclass(NursingSession, Formula)


# Core PR 10
@pytest.mark.parametrize(("side", "code"), [("left", 1), ("right", 2)])
async def test_log_nursing_posts_the_numeric_code_for_the_chosen_side(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    server: FakeServer,
    side: str,
    code: int,
) -> None:
    await call(hass, "log_nursing", left_minutes=3, right_minutes=4, finish_side=side)

    [record] = nursing_posts(server)
    assert record.finish_side == code
    assert record.to_payload()["finishSide"] == code


# Core PR 12
@pytest.mark.parametrize(("side", "code"), [("left", 1), ("right", 2)])
async def test_log_pump_posts_the_numeric_code_for_the_chosen_side(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    server: FakeServer,
    side: str,
    code: int,
) -> None:
    await call(hass, "log_pump", amount=3, minutes=10, finish_side=side)

    [record] = [r for _, op, r in posted(server) if op == OP_ADD and isinstance(r, Pump)]
    assert record.finish_side == code
