"""Core PRs 4 and 5: the last-activity sensors keep the same attribute keys whether or not a
value is set. The pump cases belong to PR 5."""

from typing import Any

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from .factories import BABY_THREE_ID, DEVICE_A, OP_DELETE, at, baby_payload, volume
from .fake_server import FakeServer
from .helpers import state, state_of, tick

DIAPER_KEYS = {"type", "note"}
FEED_KEYS = {
    "kind",
    "left_duration_minutes",
    "right_duration_minutes",
    "both_duration_minutes",
    "finish_side",
    "amount",
    "unit",
    "note",
}
SLEEP_KEYS = {"duration_minutes", "note"}
PUMP_KEYS = {
    "amount",
    "unit",
    "left_amount",
    "right_amount",
    "left_duration_minutes",
    "right_duration_minutes",
    "sides",
    "note",
}

LAST_DIAPER = "sensor.baby_one_last_diaper"
LAST_FEED = "sensor.baby_one_last_feed"
LAST_SLEEP = "sensor.baby_one_last_sleep"

# Home Assistant adds these to a state; they aren't the integration's attributes.
FRAMEWORK_KEYS = {"friendly_name", "device_class", "icon", "unit_of_measurement"}


def _attributes(hass: HomeAssistant, entity_id: str) -> dict[str, Any]:
    found = state(hass, entity_id)
    return {key: value for key, value in found.attributes.items() if key not in FRAMEWORK_KEYS}


def _pump_attributes(hass: HomeAssistant, entry: MockConfigEntry) -> dict[str, Any]:
    return _attributes(hass, state_of(hass, entry).entity_id)


def _id(number: int) -> str:
    return f"A0000000-0000-4000-8000-{number:012d}"


# A record with every value set


async def test_a_diaper_with_a_type_and_a_note_has_every_diaper_key(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    server.add_record(DEVICE_A, "Diaper", _id(1), when=at(40), status=1, note="rash cream")
    await tick(hass)

    assert _attributes(hass, LAST_DIAPER) == {"type": "dirty", "note": "rash cream"}


async def test_a_nursing_feed_with_everything_set_has_every_feed_key(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    server.add_record(
        DEVICE_A,
        "Nursing",
        _id(2),
        when=at(40),
        leftDuration=7,
        rightDuration=12,
        bothDuration=3,
        finishSide=1,
        note="sleepy",
    )
    await tick(hass)

    assert _attributes(hass, LAST_FEED) == {
        "kind": "nursing",
        "left_duration_minutes": 7,
        "right_duration_minutes": 12,
        "both_duration_minutes": 3,
        "finish_side": "right",
        "amount": None,
        "unit": None,
        "note": "sleepy",
    }


async def test_a_bottle_feed_with_everything_set_has_every_feed_key(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    server.add_record(DEVICE_A, "Formula", _id(3), when=at(40), amount=volume(4), note="fussy")
    await tick(hass)

    assert _attributes(hass, LAST_FEED) == {
        "kind": "formula",
        "left_duration_minutes": None,
        "right_duration_minutes": None,
        "both_duration_minutes": None,
        "finish_side": None,
        "amount": 4,
        "unit": "fl. oz.",
        "note": "fussy",
    }


async def test_a_sleep_with_a_duration_and_a_note_has_every_sleep_key(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    server.add_record(DEVICE_A, "Sleep", _id(4), when=at(40), duration=45, note="nap")
    await tick(hass)

    assert _attributes(hass, LAST_SLEEP) == {"duration_minutes": 45, "note": "nap"}


async def test_a_pump_with_everything_set_has_every_pump_key(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    server.add_pump(
        DEVICE_A,
        _id(5),
        when=at(40),
        amount=volume(5),
        leftAmount=volume(2),
        rightAmount=volume(3),
        leftDuration=10,
        rightDuration=12,
        sides=3,
        note="morning",
    )
    await tick(hass)

    assert _pump_attributes(hass, init_integration) == {
        "amount": 5,
        "unit": "fl. oz.",
        "left_amount": 2,
        "right_amount": 3,
        "left_duration_minutes": 10,
        "right_duration_minutes": 12,
        "sides": 3,
        "note": "morning",
    }


# A record whose optional fields are all missing


async def test_a_diaper_with_no_type_and_no_note_still_has_every_diaper_key(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    server.add_record(DEVICE_A, "Diaper", _id(10), when=at(40), status=9)
    await tick(hass)

    assert _attributes(hass, LAST_DIAPER) == {"type": None, "note": None}


async def test_a_bottle_with_no_amount_and_no_note_still_has_every_feed_key(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    server.add_record(DEVICE_A, "Formula", _id(11), when=at(40))
    await tick(hass)

    assert _attributes(hass, LAST_FEED) == {"kind": "formula"} | dict.fromkeys(FEED_KEYS - {"kind"})


async def test_a_nursing_feed_with_no_amounts_and_no_note_still_has_every_feed_key(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    server.add_record(DEVICE_A, "Nursing", _id(12), when=at(40))
    await tick(hass)

    attributes = _attributes(hass, LAST_FEED)
    assert set(attributes) == FEED_KEYS
    assert attributes["kind"] == "nursing"
    assert attributes["amount"] is None
    assert attributes["unit"] is None
    assert attributes["note"] is None


@pytest.mark.engine
async def test_a_feed_with_a_volume_in_an_unexpected_shape_still_has_every_feed_key(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    server.add_record(DEVICE_A, "Formula", _id(13), when=at(40), amount="four")
    await tick(hass)

    attributes = _attributes(hass, LAST_FEED)
    assert set(attributes) == FEED_KEYS
    assert attributes["amount"] is None
    assert attributes["unit"] is None


async def test_a_sleep_with_no_note_still_has_every_sleep_key(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    server.add_record(DEVICE_A, "Sleep", _id(14), when=at(40), duration=30)
    await tick(hass)

    assert _attributes(hass, LAST_SLEEP) == {"duration_minutes": 30, "note": None}


async def test_a_total_only_pump_still_has_every_pump_key(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    server.add_pump(DEVICE_A, _id(15), when=at(40), amount=volume(4))
    await tick(hass)

    assert _pump_attributes(hass, init_integration) == {
        "amount": 4,
        "unit": "fl. oz.",
        "left_amount": None,
        "right_amount": None,
        "left_duration_minutes": None,
        "right_duration_minutes": None,
        "sides": None,
        "note": None,
    }


async def test_a_timed_pump_with_a_zero_amount_reads_as_no_amount_but_keeps_its_keys(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    server.add_pump(DEVICE_A, _id(16), when=at(40), amount=volume(0), leftDuration=5)
    await tick(hass)

    attributes = _pump_attributes(hass, init_integration)
    assert set(attributes) == PUMP_KEYS
    assert attributes["amount"] is None
    assert attributes["unit"] is None
    assert attributes["left_duration_minutes"] == 5


# Nothing logged yet


async def test_a_baby_with_no_records_has_every_key_on_every_last_activity_sensor(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, server: FakeServer
) -> None:
    server.add_payload(DEVICE_A, baby_payload("Baby Three", BABY_THREE_ID))
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert mock_config_entry.state is ConfigEntryState.LOADED

    for entity_id, keys in (
        ("sensor.baby_three_last_diaper", DIAPER_KEYS),
        ("sensor.baby_three_last_feed", FEED_KEYS),
        ("sensor.baby_three_last_sleep", SLEEP_KEYS),
    ):
        assert state(hass, entity_id).state == "unknown"
        assert _attributes(hass, entity_id) == dict.fromkeys(keys)


async def test_last_pump_with_nothing_pumped_has_every_pump_key(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    assert state_of(hass, init_integration).state == "unknown"
    assert _pump_attributes(hass, init_integration) == dict.fromkeys(PUMP_KEYS)


async def test_deleting_the_only_diaper_leaves_every_diaper_key_with_no_value(
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

    assert state(hass, LAST_DIAPER).state == "unknown"
    assert _attributes(hass, LAST_DIAPER) == dict.fromkeys(DIAPER_KEYS)


async def test_a_baby_with_only_a_diaper_has_every_key_on_the_feed_and_sleep_sensors(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    assert _attributes(hass, LAST_FEED) == dict.fromkeys(FEED_KEYS)
    assert _attributes(hass, LAST_SLEEP) == dict.fromkeys(SLEEP_KEYS)


# Values that are falsy but set stay as they are


async def test_a_sleep_with_a_zero_duration_keeps_the_zero(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    server.add_record(DEVICE_A, "Sleep", _id(20), when=at(40), duration=0)
    await tick(hass)

    assert _attributes(hass, LAST_SLEEP)["duration_minutes"] == 0


async def test_a_nursing_feed_with_a_zero_duration_on_a_side_keeps_the_zero(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    server.add_record(
        DEVICE_A, "Nursing", _id(21), when=at(40), leftDuration=0, rightDuration=9, bothDuration=0
    )
    await tick(hass)

    attributes = _attributes(hass, LAST_FEED)
    assert attributes["left_duration_minutes"] == 0
    assert attributes["right_duration_minutes"] == 9
    assert attributes["both_duration_minutes"] == 0


async def test_a_finish_side_of_left_keeps_its_label_though_its_code_is_zero(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    server.add_record(DEVICE_A, "Nursing", _id(22), when=at(40), finishSide=0)
    await tick(hass)

    assert _attributes(hass, LAST_FEED)["finish_side"] == "left"


async def test_a_wet_diaper_keeps_its_type_though_its_code_is_zero(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    server.add_record(DEVICE_A, "Diaper", _id(23), when=at(40), status=0)
    await tick(hass)

    assert _attributes(hass, LAST_DIAPER)["type"] == "wet"


async def test_an_empty_note_reads_as_no_note_and_keeps_its_key(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    server.add_record(DEVICE_A, "Diaper", _id(24), when=at(40), status=2, note="")
    await tick(hass)

    assert _attributes(hass, LAST_DIAPER) == {"type": "mixed", "note": None}


async def test_the_key_set_does_not_change_when_a_value_comes_and_goes(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    server.add_record(DEVICE_A, "Diaper", _id(30), when=at(40), status=1, note="one")
    await tick(hass)
    with_note = set(_attributes(hass, LAST_DIAPER))
    server.add_record(DEVICE_A, "Diaper", _id(31), when=at(50), status=1)
    await tick(hass)
    without_note = set(_attributes(hass, LAST_DIAPER))

    assert with_note == without_note == DIAPER_KEYS
