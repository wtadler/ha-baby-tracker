"""The per-baby devices and their last-activity sensors."""

from __future__ import annotations

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.nighp_baby_tracker.const import DOMAIN

from .conftest import BABY_ONE, FakeServer
from .factories import (
    BABY_ONE_ID,
    BABY_THREE_ID,
    BABY_TWO_ID,
    DEVICE_A,
    DEVICE_B,
    OP_DELETE,
    at,
    baby_payload,
    volume,
)
from .helpers import tick

KEYS = ("last_diaper", "last_feed", "last_pump", "last_sleep")
# The counts and the asleep sensor are covered in test_today.py; each baby also has these four.
TODAY_KEYS = ("diapers_today", "feeds_today", "bottle_volume_today", "asleep")
PER_BABY = len(KEYS) + len(TODAY_KEYS)
# The buttons share each baby's device, so entity counts here look at readings only.
READINGS = ("sensor", "binary_sensor")
BABY_THREE = baby_payload("Baby Three", BABY_THREE_ID)


def _id(baby: str, key: str) -> str:
    return f"sensor.{baby}_{key}"


def _state(hass: HomeAssistant, baby: str, key: str):
    state = hass.states.get(_id(baby, key))
    assert state is not None, f"{_id(baby, key)} does not exist"
    return state


def _object_id(number: int) -> str:
    return f"A0000000-0000-4000-8000-{number:012d}"


# Devices and entities


async def test_two_babies_make_two_devices_each_with_four_sensors(
    hass: HomeAssistant, setup_entry: MockConfigEntry
) -> None:
    devices = dr.async_get(hass)
    entities = er.async_get(hass)
    for baby_id, name in ((BABY_ONE_ID, "Baby One"), (BABY_TWO_ID, "Baby Two")):
        device = devices.async_get_device(identifiers={(DOMAIN, baby_id)})
        assert device is not None
        assert device.name == name
        sensors = [
            e for e in er.async_entries_for_device(entities, device.id) if e.domain in READINGS
        ]
        assert len(sensors) == PER_BABY
        assert {sensor.translation_key for sensor in sensors} == set(KEYS + TODAY_KEYS)
    assert len(dr.async_entries_for_config_entry(devices, setup_entry.entry_id)) == 2


async def test_entities_are_named_after_the_baby_and_the_activity(
    hass: HomeAssistant, setup_entry: MockConfigEntry
) -> None:
    for baby, name in (("baby_one", "Baby One"), ("baby_two", "Baby Two")):
        for key, label in zip(
            KEYS, ("Last diaper", "Last feed", "Last pump", "Last sleep"), strict=True
        ):
            assert _state(hass, baby, key).attributes["friendly_name"] == f"{name} {label}"


async def test_entities_have_unique_ids(hass: HomeAssistant, setup_entry: MockConfigEntry) -> None:
    entries = er.async_entries_for_config_entry(er.async_get(hass), setup_entry.entry_id)
    unique_ids = [entry.unique_id for entry in entries if entry.domain in READINGS]
    assert len(unique_ids) == 2 * PER_BABY
    assert len(set(unique_ids)) == 2 * PER_BABY
    assert all(unique_id for unique_id in unique_ids)


async def test_unique_ids_are_built_from_the_baby_id(
    hass: HomeAssistant, setup_entry: MockConfigEntry
) -> None:
    entries = er.async_entries_for_config_entry(er.async_get(hass), setup_entry.entry_id)
    entries = [e for e in entries if e.domain in READINGS]
    assert len(entries) == 2 * PER_BABY
    assert {e.unique_id for e in entries} == {
        f"{baby}_{key}" for baby in (BABY_ONE_ID, BABY_TWO_ID) for key in KEYS + TODAY_KEYS
    }


async def test_the_sensors_are_timestamps(
    hass: HomeAssistant, setup_entry: MockConfigEntry
) -> None:
    for key in KEYS:
        assert _state(hass, "baby_one", key).attributes["device_class"] == "timestamp"


async def test_the_last_diaper_shows_when_it_happened(
    hass: HomeAssistant, setup_entry: MockConfigEntry
) -> None:
    assert _state(hass, "baby_one", "last_diaper").state == at(0).isoformat()
    assert _state(hass, "baby_two", "last_diaper").state == at(10).isoformat()


async def test_babies_do_not_share_records(
    hass: HomeAssistant, setup_entry: MockConfigEntry
) -> None:
    assert (
        _state(hass, "baby_one", "last_diaper").state
        != _state(hass, "baby_two", "last_diaper").state
    )


# Last diaper


async def test_a_newer_diaper_from_another_device_updates_the_sensor_with_its_type(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer
) -> None:
    server.add_record(DEVICE_B, "Diaper", _object_id(1), when=at(75), status=2)
    await tick(hass)

    state = _state(hass, "baby_one", "last_diaper")
    assert state.state == at(75).isoformat()
    assert state.attributes["type"] == "mixed"


@pytest.mark.parametrize(("status", "expected"), [(0, "wet"), (1, "dirty"), (2, "mixed")])
async def test_the_diaper_type_names_the_status(
    hass: HomeAssistant,
    setup_entry: MockConfigEntry,
    server: FakeServer,
    status: int,
    expected: str,
) -> None:
    server.add_record(DEVICE_A, "Diaper", _object_id(2), when=at(40), status=status)
    await tick(hass)
    assert _state(hass, "baby_one", "last_diaper").attributes["type"] == expected


async def test_an_unknown_diaper_status_has_no_type(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer
) -> None:
    server.add_record(DEVICE_A, "Diaper", _object_id(3), when=at(40), status=9)
    await tick(hass)
    state = _state(hass, "baby_one", "last_diaper")
    assert state.state == at(40).isoformat()
    assert "type" not in state.attributes


async def test_the_diaper_note_is_an_attribute_when_there_is_one(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer
) -> None:
    server.add_record(DEVICE_A, "Diaper", _object_id(4), when=at(40), status=0, note="rash cream")
    await tick(hass)
    assert _state(hass, "baby_one", "last_diaper").attributes["note"] == "rash cream"
    assert "note" not in _state(hass, "baby_two", "last_diaper").attributes


async def test_an_older_record_arriving_later_does_not_replace_the_newest(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer
) -> None:
    server.add_record(DEVICE_B, "Diaper", _object_id(5), when=at(-500), status=1)
    await tick(hass)
    assert _state(hass, "baby_one", "last_diaper").state == at(0).isoformat()


async def test_a_deleted_latest_diaper_falls_back_to_the_previous_one(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer
) -> None:
    server.add_record(DEVICE_A, "Diaper", _object_id(6), when=at(100), status=1)
    await tick(hass)
    assert _state(hass, "baby_one", "last_diaper").state == at(100).isoformat()

    server.add_record(DEVICE_A, "Diaper", _object_id(6), when=at(100), status=1, op=OP_DELETE)
    await tick(hass)
    state = _state(hass, "baby_one", "last_diaper")
    assert state.state == at(0).isoformat()
    assert state.attributes["type"] == "wet"


async def test_deleting_every_diaper_leaves_the_sensor_unknown(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer
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
    state = _state(hass, "baby_one", "last_diaper")
    assert state.state == "unknown"
    assert "type" not in state.attributes


async def test_an_edit_from_another_device_updates_the_details(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer
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
    assert _state(hass, "baby_one", "last_diaper").attributes["type"] == "mixed"


# Last feed


async def test_the_last_feed_is_the_newest_of_nursing_formula_and_pumped(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer
) -> None:
    server.add_record(DEVICE_A, "Formula", _object_id(10), when=at(10), amount=volume(4))
    server.add_record(DEVICE_A, "Nursing", _object_id(11), when=at(15), leftDuration=5)
    server.add_record(DEVICE_A, "Pumped", _object_id(12), when=at(20), amount=volume(3))
    await tick(hass)

    state = _state(hass, "baby_one", "last_feed")
    assert state.state == at(20).isoformat()
    assert state.attributes["kind"] == "pumped"


@pytest.mark.parametrize("newest", ["Nursing", "Formula", "Pumped"])
async def test_each_feed_kind_can_be_the_newest(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer, newest: str
) -> None:
    for number, kind in enumerate(["Nursing", "Formula", "Pumped"]):
        when = at(100 if kind == newest else number)
        server.add_record(DEVICE_A, kind, _object_id(20 + number), when=when)
    await tick(hass)

    state = _state(hass, "baby_one", "last_feed")
    assert state.state == at(100).isoformat()
    assert state.attributes["kind"] == newest.lower()


async def test_a_formula_feed_shows_its_amount_in_ounces(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer
) -> None:
    server.add_record(DEVICE_A, "Formula", _object_id(30), when=at(30), amount=volume(4))
    await tick(hass)
    state = _state(hass, "baby_one", "last_feed")
    assert state.attributes["kind"] == "formula"
    assert state.attributes["amount"] == 4
    assert state.attributes["unit"] == "fl. oz."


async def test_a_pumped_bottle_in_milliliters_shows_its_unit(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer
) -> None:
    server.add_record(
        DEVICE_A, "Pumped", _object_id(31), when=at(30), amount=volume(90, english=False)
    )
    await tick(hass)
    state = _state(hass, "baby_one", "last_feed")
    assert state.attributes["amount"] == 90
    assert state.attributes["unit"] == "mL"


async def test_a_nursing_feed_shows_sides_and_durations(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer
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
    state = _state(hass, "baby_one", "last_feed")
    assert state.attributes["kind"] == "nursing"
    assert state.attributes["left_duration_minutes"] == 7
    assert state.attributes["right_duration_minutes"] == 12
    assert state.attributes["finish_side"] == "right"
    assert state.attributes["note"] == "sleepy"
    assert "amount" not in state.attributes


async def test_a_feed_that_is_not_nursing_formula_or_pumped_is_ignored(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer
) -> None:
    server.add_record(DEVICE_A, "Formula", _object_id(33), when=at(10), amount=volume(2))
    server.add_record(DEVICE_A, "OtherFeed", _object_id(34), when=at(99), amount=2.5)
    await tick(hass)
    assert _state(hass, "baby_one", "last_feed").state == at(10).isoformat()


# Last pump


async def test_the_last_pump_shows_amounts_and_durations(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer
) -> None:
    server.add_record(
        DEVICE_A,
        "Pump",
        _object_id(40),
        when=at(45),
        amount=volume(5),
        leftAmount=volume(2),
        rightAmount=volume(3),
        leftDuration=10,
        rightDuration=12,
        sides=3,
        note="morning",
    )
    await tick(hass)

    state = _state(hass, "baby_one", "last_pump")
    assert state.state == at(45).isoformat()
    assert state.attributes["amount"] == 5
    assert state.attributes["unit"] == "fl. oz."
    assert state.attributes["left_amount"] == 2
    assert state.attributes["right_amount"] == 3
    assert state.attributes["left_duration_minutes"] == 10
    assert state.attributes["right_duration_minutes"] == 12
    assert state.attributes["sides"] == 3
    assert state.attributes["note"] == "morning"


async def test_a_pumped_bottle_is_not_a_pump_session(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer
) -> None:
    server.add_record(DEVICE_A, "Pumped", _object_id(41), when=at(45), amount=volume(3))
    await tick(hass)
    assert _state(hass, "baby_one", "last_pump").state == "unknown"


# Last sleep


async def test_the_last_sleep_shows_its_duration_and_note(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer
) -> None:
    server.add_record(DEVICE_A, "Sleep", _object_id(50), when=at(60), duration=45, note="nap")
    await tick(hass)
    state = _state(hass, "baby_one", "last_sleep")
    assert state.state == at(60).isoformat()
    assert state.attributes["duration_minutes"] == 45
    assert state.attributes["note"] == "nap"


async def test_a_sleep_in_progress_has_zero_duration(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer
) -> None:
    server.add_record(DEVICE_A, "Sleep", _object_id(51), when=at(60), duration=0)
    await tick(hass)
    assert _state(hass, "baby_one", "last_sleep").attributes["duration_minutes"] == 0


async def test_ending_a_sleep_updates_the_duration(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer
) -> None:
    server.add_record(DEVICE_A, "Sleep", _object_id(52), when=at(60), duration=0)
    await tick(hass)
    server.add_record(
        DEVICE_A, "Sleep", _object_id(52), when=at(60), changed=at(100), duration=40, op=1
    )
    await tick(hass)
    state = _state(hass, "baby_one", "last_sleep")
    assert state.state == at(60).isoformat()
    assert state.attributes["duration_minutes"] == 40


# Babies with little or odd data


async def test_a_baby_with_no_records_yet_still_gets_a_device_and_sensors(
    hass: HomeAssistant, config_entry: MockConfigEntry, server: FakeServer
) -> None:
    server.add_payload(DEVICE_A, BABY_THREE)
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    assert config_entry.state is ConfigEntryState.LOADED
    device = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, BABY_THREE_ID)})
    assert device is not None
    assert device.name == "Baby Three"
    for key in KEYS:
        assert _state(hass, "baby_three", key).state == "unknown"


async def test_a_baby_with_only_a_diaper_has_unknown_other_sensors(
    hass: HomeAssistant, setup_entry: MockConfigEntry
) -> None:
    assert _state(hass, "baby_one", "last_diaper").state != "unknown"
    for key in ("last_feed", "last_pump", "last_sleep"):
        state = _state(hass, "baby_one", key)
        assert state.state == "unknown"
        assert "kind" not in state.attributes


async def test_a_record_type_the_integration_does_not_model_breaks_nothing(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer
) -> None:
    server.add_record(DEVICE_A, "Quux", _object_id(60), when=at(500), flavor="odd")
    server.add_record(DEVICE_A, "Growth", _object_id(61), when=at(501), weight=7.5)
    server.add_record(DEVICE_A, "Temperature", _object_id(62), when=at(502), temp=98.6)
    await tick(hass)

    assert setup_entry.state is ConfigEntryState.LOADED
    assert _state(hass, "baby_one", "last_diaper").state == at(0).isoformat()
    for key in ("last_feed", "last_pump", "last_sleep"):
        assert _state(hass, "baby_one", key).state == "unknown"


async def test_a_modeled_type_with_values_the_library_cannot_read_breaks_nothing(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer
) -> None:
    payload = {
        "BCObjectType": "Diaper",
        "objectID": _object_id(63),
        "time": "not a time",
        "timestamp": "not a time",
        "status": 1,
        "baby": BABY_ONE,
    }
    server.add_payload(DEVICE_A, payload)
    await tick(hass)
    assert setup_entry.state is ConfigEntryState.LOADED
    assert _state(hass, "baby_one", "last_diaper").state == at(0).isoformat()


async def test_an_undecodable_transaction_breaks_nothing(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer
) -> None:
    server.add_raw(DEVICE_A, {"SyncID": 3, "OPCode": 0, "Transaction": "!!not base64!!"})
    await tick(hass)
    assert setup_entry.state is ConfigEntryState.LOADED
    assert _state(hass, "baby_one", "last_diaper").state == at(0).isoformat()


async def test_a_feed_with_an_amount_in_an_unexpected_shape_still_shows(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer
) -> None:
    server.add_record(DEVICE_A, "Formula", _object_id(64), when=at(5), amount="lots")
    await tick(hass)
    state = _state(hass, "baby_one", "last_feed")
    assert state.state == at(5).isoformat()
    assert state.attributes["kind"] == "formula"
    assert "amount" not in state.attributes


# Babies that appear later


async def test_a_baby_that_appears_later_is_added_without_a_reload(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer
) -> None:
    assert hass.states.get(_id("baby_three", "last_diaper")) is None
    clients = len(server.clients)

    server.add_record(DEVICE_B, "Diaper", _object_id(70), when=at(300), baby=BABY_THREE, status=1)
    await tick(hass)

    assert len(server.clients) == clients
    assert _state(hass, "baby_three", "last_diaper").state == at(300).isoformat()
    assert _state(hass, "baby_three", "last_diaper").attributes["type"] == "dirty"
    device = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, BABY_THREE_ID)})
    assert device is not None
    entries = er.async_entries_for_device(er.async_get(hass), device.id)
    assert len([e for e in entries if e.domain in READINGS]) == PER_BABY


async def test_syncing_again_does_not_duplicate_a_baby(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer
) -> None:
    server.add_record(DEVICE_B, "Diaper", _object_id(71), when=at(300), baby=BABY_THREE)
    await tick(hass)
    await tick(hass)
    entries = er.async_entries_for_config_entry(er.async_get(hass), setup_entry.entry_id)
    assert len([e for e in entries if e.domain in READINGS]) == 3 * PER_BABY


async def test_babies_are_restored_from_saved_state_after_a_restart(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer
) -> None:
    server.add_record(DEVICE_B, "Diaper", _object_id(72), when=at(300), baby=BABY_THREE)
    await tick(hass)
    assert await hass.config_entries.async_unload(setup_entry.entry_id)
    assert await hass.config_entries.async_setup(setup_entry.entry_id)
    await hass.async_block_till_done()

    for baby in ("baby_one", "baby_two", "baby_three"):
        assert _state(hass, baby, "last_diaper").state != "unknown"


async def test_a_feed_kept_raw_by_the_library_breaks_nothing(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer
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
    assert setup_entry.state is ConfigEntryState.LOADED
    state = _state(hass, "baby_one", "last_feed")
    assert state.state == "unknown"  # a raw record carries no baby, so it isn't shown
    assert "amount" not in state.attributes
