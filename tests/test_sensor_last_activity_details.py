"""Core PR 4: the last-activity details (diaper type, feed kind and amounts, sleep duration,
note)."""

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
)

from . import BABY_ONE
from .factories import (
    DEVICE_A,
    DEVICE_B,
    OP_DELETE,
    OP_UPDATE,
    at,
    volume,
)
from .fake_server import FakeServer
from .helpers import (
    DIAPER,
    NOW,
    tick,
)


def _entity_id(baby: str, key: str) -> str:
    return f"sensor.{baby}_{key}"


def _sensor_state(hass: HomeAssistant, baby: str, key: str):
    state = hass.states.get(_entity_id(baby, key))
    assert state is not None, f"{_entity_id(baby, key)} does not exist"
    return state


def _object_id(number: int) -> str:
    return f"A0000000-0000-4000-8000-{number:012d}"


@pytest.fixture(autouse=True)
def _start_at_now(freezer: FrozenDateTimeFactory) -> None:
    """Freeze the clock at NOW before anything is set up.

    Timers scheduled at setup then share the clock the tests move, so moving it forward and
    ticking reaches them the way it does in core's tests.
    """
    freezer.move_to(NOW)


async def test_a_newer_diaper_from_another_device_updates_the_sensor_with_its_type(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    server.add_record(DEVICE_B, "Diaper", _object_id(1), when=at(75), status=2)
    await tick(hass)

    state = _sensor_state(hass, "baby_one", "last_diaper")
    assert state.state == at(75).isoformat()
    assert state.attributes["type"] == "mixed"


@pytest.mark.parametrize(("status", "expected"), [(0, "wet"), (1, "dirty"), (2, "mixed")])
async def test_the_diaper_type_names_the_status(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    server: FakeServer,
    status: int,
    expected: str,
) -> None:
    server.add_record(DEVICE_A, "Diaper", _object_id(2), when=at(40), status=status)
    await tick(hass)
    assert _sensor_state(hass, "baby_one", "last_diaper").attributes["type"] == expected


async def test_an_unknown_diaper_status_has_no_type(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    server.add_record(DEVICE_A, "Diaper", _object_id(3), when=at(40), status=9)
    await tick(hass)
    state = _sensor_state(hass, "baby_one", "last_diaper")
    assert state.state == at(40).isoformat()
    assert state.attributes["type"] is None


async def test_the_diaper_note_is_an_attribute_when_there_is_one(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    server.add_record(DEVICE_A, "Diaper", _object_id(4), when=at(40), status=0, note="rash cream")
    await tick(hass)
    assert _sensor_state(hass, "baby_one", "last_diaper").attributes["note"] == "rash cream"
    assert _sensor_state(hass, "baby_two", "last_diaper").attributes["note"] is None


@pytest.mark.engine
async def test_an_edit_from_another_device_updates_the_details(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    server.add_record(
        DEVICE_B,
        "Diaper",
        "D0000000-0000-4000-8000-000000000001",
        when=at(0),
        changed=at(500),
        status=2,
        op=1,
    )
    await tick(hass)
    assert _sensor_state(hass, "baby_one", "last_diaper").attributes["type"] == "mixed"


async def test_a_formula_feed_shows_its_amount_in_ounces(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    server.add_record(DEVICE_A, "Formula", _object_id(30), when=at(30), amount=volume(4))
    await tick(hass)
    state = _sensor_state(hass, "baby_one", "last_feed")
    assert state.attributes["kind"] == "formula"
    assert state.attributes["amount"] == 4
    assert state.attributes["unit"] == "fl. oz."


async def test_a_pumped_bottle_in_milliliters_shows_its_unit(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    server.add_record(
        DEVICE_A, "Pumped", _object_id(31), when=at(30), amount=volume(90, english=False)
    )
    await tick(hass)
    state = _sensor_state(hass, "baby_one", "last_feed")
    assert state.attributes["amount"] == 90
    assert state.attributes["unit"] == "mL"


async def test_a_nursing_feed_shows_sides_and_durations(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    server.add_record(
        DEVICE_A,
        "Nursing",
        _object_id(32),
        when=at(30),
        leftDuration=7,
        rightDuration=12,
        bothDuration=0,
        finishSide=1,
        note="sleepy",
    )
    await tick(hass)
    state = _sensor_state(hass, "baby_one", "last_feed")
    assert state.attributes["kind"] == "nursing"
    assert state.attributes["left_duration_minutes"] == 7
    assert state.attributes["right_duration_minutes"] == 12
    assert state.attributes["finish_side"] == "right"
    assert state.attributes["note"] == "sleepy"
    assert state.attributes["amount"] is None


async def test_the_last_sleep_shows_its_duration_and_note(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    server.add_record(DEVICE_A, "Sleep", _object_id(50), when=at(60), duration=45, note="nap")
    await tick(hass)
    state = _sensor_state(hass, "baby_one", "last_sleep")
    assert state.state == at(60).isoformat()
    assert state.attributes["duration_minutes"] == 45
    assert state.attributes["note"] == "nap"


async def test_a_sleep_in_progress_has_zero_duration(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    server.add_record(DEVICE_A, "Sleep", _object_id(51), when=at(60), duration=0)
    await tick(hass)
    assert _sensor_state(hass, "baby_one", "last_sleep").attributes["duration_minutes"] == 0


async def test_ending_a_sleep_updates_the_duration(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    server.add_record(DEVICE_A, "Sleep", _object_id(52), when=at(60), duration=0)
    await tick(hass)
    server.add_record(
        DEVICE_A, "Sleep", _object_id(52), when=at(60), changed=at(100), duration=40, op=1
    )
    await tick(hass)
    state = _sensor_state(hass, "baby_one", "last_sleep")
    assert state.state == at(60).isoformat()
    assert state.attributes["duration_minutes"] == 40


@pytest.mark.engine
async def test_a_feed_with_an_amount_in_an_unexpected_shape_still_shows(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    server.add_record(DEVICE_A, "Formula", _object_id(64), when=at(5), amount="lots")
    await tick(hass)
    state = _sensor_state(hass, "baby_one", "last_feed")
    assert state.state == at(5).isoformat()
    assert state.attributes["kind"] == "formula"
    assert state.attributes["amount"] is None


async def test_a_record_changed_by_another_phone_still_updates_the_sensor(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    """The old record and its replacement must compare unequal, or always_update=False hides it."""
    before = dict(hass.states.get(DIAPER).attributes)
    server.add_record(
        "BBBBBBBB-0000-4000-8000-00000000000B",
        "Diaper",
        "D0000000-0000-4000-8000-000000000001",
        when=at(0),
        changed=at(30),
        op=OP_UPDATE,
        status=2,
    )
    await tick(hass)
    assert dict(hass.states.get(DIAPER).attributes) != before
    assert hass.states.get(DIAPER).attributes["type"] == "mixed"


@pytest.mark.engine
async def test_a_deleted_latest_diaper_falls_back_to_the_previous_one(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    server.add_record(DEVICE_A, "Diaper", _object_id(6), when=at(100), status=1)
    await tick(hass)
    assert _sensor_state(hass, "baby_one", "last_diaper").state == at(100).isoformat()

    server.add_record(DEVICE_A, "Diaper", _object_id(6), when=at(100), status=1, op=OP_DELETE)
    await tick(hass)
    state = _sensor_state(hass, "baby_one", "last_diaper")
    assert state.state == at(0).isoformat()
    assert state.attributes["type"] == "wet"


async def test_deleting_every_diaper_leaves_the_sensor_unknown(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    server.add_record(
        DEVICE_A,
        "Diaper",
        "D0000000-0000-4000-8000-000000000001",
        when=at(0),
        status=0,
        op=OP_DELETE,
    )
    await tick(hass)
    state = _sensor_state(hass, "baby_one", "last_diaper")
    assert state.state == "unknown"
    assert state.attributes["type"] is None


async def test_the_last_feed_is_the_newest_of_nursing_formula_and_pumped(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    server.add_record(DEVICE_A, "Formula", _object_id(10), when=at(10), amount=volume(4))
    server.add_record(DEVICE_A, "Nursing", _object_id(11), when=at(15), leftDuration=5)
    server.add_record(DEVICE_A, "Pumped", _object_id(12), when=at(20), amount=volume(3))
    await tick(hass)

    state = _sensor_state(hass, "baby_one", "last_feed")
    assert state.state == at(20).isoformat()
    assert state.attributes["kind"] == "pumped"


@pytest.mark.parametrize("newest", ["Nursing", "Formula", "Pumped"])
async def test_each_feed_kind_can_be_the_newest(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, newest: str
) -> None:
    for number, kind in enumerate(["Nursing", "Formula", "Pumped"]):
        when = at(100 if kind == newest else number)
        server.add_record(DEVICE_A, kind, _object_id(20 + number), when=when)
    await tick(hass)

    state = _sensor_state(hass, "baby_one", "last_feed")
    assert state.state == at(100).isoformat()
    assert state.attributes["kind"] == newest.lower()


async def test_a_baby_with_only_a_diaper_has_unknown_other_sensors(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    assert _sensor_state(hass, "baby_one", "last_diaper").state != "unknown"
    for key, attribute in (("last_feed", "kind"), ("last_sleep", "duration_minutes")):
        state = _sensor_state(hass, "baby_one", key)
        assert state.state == "unknown"
        assert state.attributes[attribute] is None


@pytest.mark.engine
async def test_a_feed_kept_raw_by_the_library_breaks_nothing(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    payload = {
        "BCObjectType": "Formula",
        "objectID": _object_id(80),
        "time": at(500).strftime("%Y-%m-%d %H:%M:%S %z"),
        "timestamp": "not a time",
        "amount": volume(4),
        "baby": BABY_ONE,
    }
    server.add_payload(DEVICE_A, payload)
    await tick(hass)
    assert init_integration.state is ConfigEntryState.LOADED
    state = _sensor_state(hass, "baby_one", "last_feed")
    assert state.state == "unknown"  # a raw record carries no baby, so it isn't shown
    assert state.attributes["amount"] is None
