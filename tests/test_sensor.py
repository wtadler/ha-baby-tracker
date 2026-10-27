"""The sensors: last activity, today's counts, timers, and the account's last pump."""

from collections.abc import Iterator
from datetime import datetime, timedelta
from typing import Any
from unittest.mock import patch

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import (
    EVENT_STATE_REPORTED,
    Platform,
)
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_system import METRIC_SYSTEM, US_CUSTOMARY_SYSTEM
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
    snapshot_platform,
)
from syrupy.assertion import SnapshotAssertion

from custom_components.nighp_baby_tracker.pybabytracker import BabyTrackerConnectionError

from . import BABY_ONE, BABY_TWO, EMAIL, setup_integration
from .factories import (
    BABY_ONE_ID,
    BABY_THREE_ID,
    BABY_TWO_ID,
    DEVICE_A,
    DEVICE_B,
    OP_DELETE,
    OP_UPDATE,
    at,
    baby_payload,
    volume,
)
from .fake_server import FakeServer
from .helpers import (
    ASLEEP,
    DIAPER,
    LAST_PUMP,
    LAST_SLEEP,
    LOCAL_NOW,
    MIDNIGHT,
    NOW,
    NURSE_LEFT,
    NURSE_RIGHT,
    NURSING_LEFT_TIME,
    NURSING_RIGHT_TIME,
    NURSING_TIMER,
    PHONE_SLEEP,
    SLEEP_TIMER,
    TODAY,
    TWO_ASLEEP,
    TWO_SLEEP_TIMER,
    account_device,
    baby_state,
    baby_value,
    get_baby_device,
    local,
    minutes,
    pass_midnight,
    phone_ends_sleep,
    phone_sleep,
    pump_entity,
    setup_loaded,
    state,
    state_of,
    sync,
    tick,
    toggle,
    turn_off,
    turn_on,
)

KEYS = ("last_diaper", "last_feed", "last_sleep")  # a pump belongs to the account, not a baby


# The counts and the asleep sensor are covered in test_today.py; each baby also has these four.
TODAY_KEYS = ("diapers_today", "feeds_today", "bottle_volume_today", "asleep")


TIMER_KEYS = ("sleep_timer", "nursing_timer", "nursing_left_time", "nursing_right_time")


PER_BABY = len(KEYS) + len(TODAY_KEYS) + len(TIMER_KEYS)


# The buttons share each baby's device, so entity counts here look at readings only.
READINGS = ("sensor", "binary_sensor")


BABY_THREE = baby_payload("Baby Three", BABY_THREE_ID)


def _entity_id(baby: str, key: str) -> str:
    return f"sensor.{baby}_{key}"


def _sensor_state(hass: HomeAssistant, baby: str, key: str):
    state = hass.states.get(_entity_id(baby, key))
    assert state is not None, f"{_entity_id(baby, key)} does not exist"
    return state


def _object_id(number: int) -> str:
    return f"A0000000-0000-4000-8000-{number:012d}"


ML_PER_OZ = 29.5735295625


YESTERDAY = local(2026, 10, 25, 9)


_counter = 0


def _id() -> str:
    global _counter
    _counter += 1
    return f"A0000000-0000-4000-8000-{_counter:012d}"


@pytest.fixture(autouse=True)
def _reset_counter() -> Iterator[None]:
    global _counter
    _counter = 0
    yield


def _diaper(server: FakeServer, when: datetime, status: int = 0, baby=BABY_ONE, **kw) -> str:
    object_id = _id()
    server.add_record(DEVICE_A, "Diaper", object_id, when=when, status=status, baby=baby, **kw)
    return object_id


def _breakdown(state) -> tuple[int, int, int]:
    attributes = state.attributes
    return attributes["wet"], attributes["dirty"], attributes["mixed"]


def _pump_id(number: int) -> str:
    return f"B0000000-0000-4000-8000-{number:012d}"


PHONE_SLEEP_TWO = "5EE90000-0000-4000-8000-000000000002"


@pytest.fixture(autouse=True)
def _start_at_now(freezer: Any) -> None:
    """Freeze the clock at NOW before anything is set up.

    Timers scheduled at setup then share the clock the tests move, so moving it forward and
    ticking reaches them the way it does in core's tests.
    """
    freezer.move_to(NOW)


# Devices and entities


async def test_sensors(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    snapshot: SnapshotAssertion,
    freezer: FrozenDateTimeFactory,
    mock_config_entry: MockConfigEntry,
    populated_server: FakeServer,
) -> None:
    freezer.move_to(at(180))
    with patch("custom_components.nighp_baby_tracker.PLATFORMS", [Platform.SENSOR]):
        await setup_integration(hass, mock_config_entry)

    await snapshot_platform(hass, entity_registry, snapshot, mock_config_entry.entry_id)


async def test_two_babies_make_two_devices_each_with_their_own_sensors(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    devices = dr.async_get(hass)
    entities = er.async_get(hass)
    for baby_id, name in ((BABY_ONE_ID, "Baby One"), (BABY_TWO_ID, "Baby Two")):
        device = get_baby_device(devices, baby_id)
        assert device is not None
        assert device.name == name
        sensors = [
            e for e in er.async_entries_for_device(entities, device.id) if e.domain in READINGS
        ]
        assert len(sensors) == PER_BABY
        assert {sensor.translation_key for sensor in sensors} == set(KEYS + TODAY_KEYS + TIMER_KEYS)
    # The two babies' devices and the account's (which holds the last pump).
    assert len(dr.async_entries_for_config_entry(devices, init_integration.entry_id)) == 3


async def test_entities_are_named_after_the_baby_and_the_activity(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    for baby, name in (("baby_one", "Baby One"), ("baby_two", "Baby Two")):
        for key, label in zip(KEYS, ("Last diaper", "Last feed", "Last sleep"), strict=True):
            assert _sensor_state(hass, baby, key).attributes["friendly_name"] == f"{name} {label}"


async def test_entities_have_unique_ids(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    entries = er.async_entries_for_config_entry(er.async_get(hass), init_integration.entry_id)
    unique_ids = [entry.unique_id for entry in entries if entry.domain in READINGS]
    assert len(unique_ids) == 2 * PER_BABY + 1  # and the account's last pump
    assert len(set(unique_ids)) == 2 * PER_BABY + 1
    assert all(unique_id for unique_id in unique_ids)


async def test_unique_ids_are_built_from_the_baby_id(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    entries = er.async_entries_for_config_entry(er.async_get(hass), init_integration.entry_id)
    entries = [e for e in entries if e.domain in READINGS]
    assert len(entries) == 2 * PER_BABY + 1
    assert {e.unique_id for e in entries} == {
        f"{baby}_{key}"
        for baby in (BABY_ONE_ID, BABY_TWO_ID)
        for key in KEYS + TODAY_KEYS + TIMER_KEYS
    } | {f"{init_integration.entry_id}_last_pump"}


async def test_the_sensors_are_timestamps(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    for key in KEYS:
        assert _sensor_state(hass, "baby_one", key).attributes["device_class"] == "timestamp"


async def test_the_last_diaper_shows_when_it_happened(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    assert _sensor_state(hass, "baby_one", "last_diaper").state == at(0).isoformat()
    assert _sensor_state(hass, "baby_two", "last_diaper").state == at(10).isoformat()


async def test_babies_do_not_share_records(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    assert (
        _sensor_state(hass, "baby_one", "last_diaper").state
        != _sensor_state(hass, "baby_two", "last_diaper").state
    )


# Last diaper


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
    assert "type" not in state.attributes


async def test_the_diaper_note_is_an_attribute_when_there_is_one(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    server.add_record(DEVICE_A, "Diaper", _object_id(4), when=at(40), status=0, note="rash cream")
    await tick(hass)
    assert _sensor_state(hass, "baby_one", "last_diaper").attributes["note"] == "rash cream"
    assert "note" not in _sensor_state(hass, "baby_two", "last_diaper").attributes


async def test_an_older_record_arriving_later_does_not_replace_the_newest(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    server.add_record(DEVICE_B, "Diaper", _object_id(5), when=at(-500), status=1)
    await tick(hass)
    assert _sensor_state(hass, "baby_one", "last_diaper").state == at(0).isoformat()


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
    assert "type" not in state.attributes


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


# Last feed


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
    assert "amount" not in state.attributes


async def test_a_feed_that_is_not_nursing_formula_or_pumped_is_ignored(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    server.add_record(DEVICE_A, "Formula", _object_id(33), when=at(10), amount=volume(2))
    server.add_record(DEVICE_A, "OtherFeed", _object_id(34), when=at(99), amount=2.5)
    await tick(hass)
    assert _sensor_state(hass, "baby_one", "last_feed").state == at(10).isoformat()


# Last sleep


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


# Babies with little or odd data


async def test_a_baby_with_no_records_yet_still_gets_a_device_and_sensors(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, server: FakeServer
) -> None:
    server.add_payload(DEVICE_A, BABY_THREE)
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.LOADED
    device = get_baby_device(dr.async_get(hass), BABY_THREE_ID)
    assert device is not None
    assert device.name == "Baby Three"
    for key in KEYS:
        assert _sensor_state(hass, "baby_three", key).state == "unknown"


async def test_a_baby_with_only_a_diaper_has_unknown_other_sensors(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    assert _sensor_state(hass, "baby_one", "last_diaper").state != "unknown"
    for key in ("last_feed", "last_sleep"):
        state = _sensor_state(hass, "baby_one", key)
        assert state.state == "unknown"
        assert "kind" not in state.attributes


async def test_a_record_type_the_integration_does_not_model_breaks_nothing(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    server.add_record(DEVICE_A, "Quux", _object_id(60), when=at(500), flavor="odd")
    server.add_record(DEVICE_A, "Growth", _object_id(61), when=at(501), weight=7.5)
    server.add_record(DEVICE_A, "Temperature", _object_id(62), when=at(502), temp=98.6)
    await tick(hass)

    assert init_integration.state is ConfigEntryState.LOADED
    assert _sensor_state(hass, "baby_one", "last_diaper").state == at(0).isoformat()
    for key in ("last_feed", "last_sleep"):
        assert _sensor_state(hass, "baby_one", key).state == "unknown"


async def test_a_modeled_type_with_values_the_library_cannot_read_breaks_nothing(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
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
    assert init_integration.state is ConfigEntryState.LOADED
    assert _sensor_state(hass, "baby_one", "last_diaper").state == at(0).isoformat()


async def test_an_undecodable_transaction_breaks_nothing(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    server.add_raw(DEVICE_A, {"SyncID": 3, "OPCode": 0, "Transaction": "!!not base64!!"})
    await tick(hass)
    assert init_integration.state is ConfigEntryState.LOADED
    assert _sensor_state(hass, "baby_one", "last_diaper").state == at(0).isoformat()


async def test_a_feed_with_an_amount_in_an_unexpected_shape_still_shows(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    server.add_record(DEVICE_A, "Formula", _object_id(64), when=at(5), amount="lots")
    await tick(hass)
    state = _sensor_state(hass, "baby_one", "last_feed")
    assert state.state == at(5).isoformat()
    assert state.attributes["kind"] == "formula"
    assert "amount" not in state.attributes


# Babies that appear later


async def test_a_baby_that_appears_later_is_added_without_a_reload(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    assert hass.states.get(_entity_id("baby_three", "last_diaper")) is None
    clients = len(server.clients)

    server.add_record(DEVICE_B, "Diaper", _object_id(70), when=at(300), baby=BABY_THREE, status=1)
    await tick(hass)

    assert len(server.clients) == clients
    assert _sensor_state(hass, "baby_three", "last_diaper").state == at(300).isoformat()
    assert _sensor_state(hass, "baby_three", "last_diaper").attributes["type"] == "dirty"
    device = get_baby_device(dr.async_get(hass), BABY_THREE_ID)
    assert device is not None
    entries = er.async_entries_for_device(er.async_get(hass), device.id)
    assert len([e for e in entries if e.domain in READINGS]) == PER_BABY


async def test_syncing_again_does_not_duplicate_a_baby(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    server.add_record(DEVICE_B, "Diaper", _object_id(71), when=at(300), baby=BABY_THREE)
    await tick(hass)
    await tick(hass)
    entries = er.async_entries_for_config_entry(er.async_get(hass), init_integration.entry_id)
    assert len([e for e in entries if e.domain in READINGS]) == 3 * PER_BABY + 1


async def test_babies_are_restored_from_saved_state_after_a_restart(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    server.add_record(DEVICE_B, "Diaper", _object_id(72), when=at(300), baby=BABY_THREE)
    await tick(hass)
    assert await hass.config_entries.async_unload(init_integration.entry_id)
    assert await hass.config_entries.async_setup(init_integration.entry_id)
    await hass.async_block_till_done()

    for baby in ("baby_one", "baby_two", "baby_three"):
        assert _sensor_state(hass, baby, "last_diaper").state != "unknown"


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
    assert "amount" not in state.attributes


# Entities


async def test_each_baby_gets_the_four_new_entities(
    hass: HomeAssistant, entry: MockConfigEntry, empty: FakeServer
) -> None:
    await setup_loaded(hass, entry)
    registry = er.async_get(hass)
    devices = dr.async_get(hass)
    for baby_id in (BABY_ONE_ID, BABY_TWO_ID):
        device = get_baby_device(devices, baby_id)
        assert device is not None
        by_key = {
            entity.translation_key: entity
            for entity in er.async_entries_for_device(registry, device.id)
        }
        for key in ("diapers_today", "feeds_today", "bottle_volume_today", "asleep"):
            assert by_key[key].unique_id == f"{baby_id}_{key}"


async def test_the_entities_are_named_after_the_baby(
    hass: HomeAssistant, entry: MockConfigEntry, empty: FakeServer
) -> None:
    await setup_loaded(hass, entry)
    names = {
        "diapers_today": "Diapers today",
        "feeds_today": "Feeds today",
        "bottle_volume_today": "Bottle volume today",
        "asleep": "Asleep",
    }
    for baby, name in (("baby_one", "Baby One"), ("baby_two", "Baby Two")):
        for key, label in names.items():
            assert baby_state(hass, key, baby).attributes["friendly_name"] == f"{name} {label}"


async def test_the_volume_sensor_is_a_volume(
    hass: HomeAssistant, entry: MockConfigEntry, empty: FakeServer
) -> None:
    await setup_loaded(hass, entry)
    assert baby_state(hass, "bottle_volume_today").attributes["device_class"] == "volume"


# Diapers


async def test_three_diapers_today_and_two_yesterday_count_three(
    hass: HomeAssistant, entry: MockConfigEntry, empty: FakeServer
) -> None:
    for status in (0, 1, 2):
        _diaper(empty, TODAY, status)
    for status in (0, 1):
        _diaper(empty, YESTERDAY, status)
    await setup_loaded(hass, entry)
    state = baby_state(hass, "diapers_today")
    assert state.state == "3"
    assert _breakdown(state) == (1, 1, 1)


async def test_the_diaper_attributes_break_the_count_down_by_type(
    hass: HomeAssistant, entry: MockConfigEntry, empty: FakeServer
) -> None:
    for status in (0, 0, 0, 1, 1, 2):
        _diaper(empty, TODAY, status)
    await setup_loaded(hass, entry)
    state = baby_state(hass, "diapers_today")
    assert state.state == "6"
    assert _breakdown(state) == (3, 2, 1)


async def test_an_unrecognized_diaper_status_counts_in_the_total_only(
    hass: HomeAssistant, entry: MockConfigEntry, empty: FakeServer
) -> None:
    _diaper(empty, TODAY, 0)
    _diaper(empty, TODAY, 9)
    await setup_loaded(hass, entry)
    state = baby_state(hass, "diapers_today")
    assert state.state == "2"
    assert _breakdown(state) == (1, 0, 0)


async def test_babies_do_not_share_counts(
    hass: HomeAssistant, entry: MockConfigEntry, empty: FakeServer
) -> None:
    _diaper(empty, TODAY, 0)
    _diaper(empty, TODAY, 1, baby=BABY_TWO)
    _diaper(empty, TODAY, 2, baby=BABY_TWO)
    await setup_loaded(hass, entry)
    assert baby_value(hass, "diapers_today", "baby_one") == "1"
    assert baby_value(hass, "diapers_today", "baby_two") == "2"


async def test_a_diaper_from_another_device_is_counted_after_the_next_sync(
    hass: HomeAssistant, entry: MockConfigEntry, empty: FakeServer
) -> None:
    await setup_loaded(hass, entry)
    assert baby_value(hass, "diapers_today") == "0"
    empty.add_record(DEVICE_B, "Diaper", _id(), when=TODAY, status=1)
    await tick(hass, minutes=1440)
    state = baby_state(hass, "diapers_today")
    assert state.state == "1"
    assert _breakdown(state) == (0, 1, 0)


async def test_a_diaper_dated_today_but_deleted_is_not_counted(
    hass: HomeAssistant, entry: MockConfigEntry, empty: FakeServer
) -> None:
    _diaper(empty, TODAY, 0)
    gone = _diaper(empty, TODAY, 1)
    empty.add_record(DEVICE_A, "Diaper", gone, when=TODAY, status=1, op=OP_DELETE)
    await setup_loaded(hass, entry)
    state = baby_state(hass, "diapers_today")
    assert state.state == "1"
    assert _breakdown(state) == (1, 0, 0)


async def test_a_diaper_deleted_after_the_first_sync_drops_out_of_the_count(
    hass: HomeAssistant, entry: MockConfigEntry, empty: FakeServer
) -> None:
    gone = _diaper(empty, TODAY, 1)
    await setup_loaded(hass, entry)
    assert baby_value(hass, "diapers_today") == "1"
    empty.add_record(DEVICE_A, "Diaper", gone, when=TODAY, status=1, op=OP_DELETE)
    await tick(hass, minutes=1440)
    assert baby_value(hass, "diapers_today") == "0"


async def test_a_diaper_edited_to_another_day_leaves_the_count(
    hass: HomeAssistant, entry: MockConfigEntry, empty: FakeServer
) -> None:
    object_id = _diaper(empty, TODAY, 0)
    await setup_loaded(hass, entry)
    assert baby_value(hass, "diapers_today") == "1"
    empty.add_record(
        DEVICE_A, "Diaper", object_id, when=YESTERDAY, changed=LOCAL_NOW, status=0, op=OP_UPDATE
    )
    await tick(hass, minutes=1440)
    assert baby_value(hass, "diapers_today") == "0"


# Feeds


async def test_the_feed_count_covers_nursing_formula_and_pumped(
    hass: HomeAssistant, entry: MockConfigEntry, empty: FakeServer
) -> None:
    empty.add_record(DEVICE_A, "Nursing", _id(), when=TODAY, leftDuration=5, rightDuration=3)
    empty.add_record(DEVICE_A, "Formula", _id(), when=TODAY, amount=volume(4))
    empty.add_record(DEVICE_A, "Pumped", _id(), when=TODAY, amount=volume(90, english=False))
    empty.add_record(DEVICE_A, "Nursing", _id(), when=YESTERDAY, leftDuration=5)
    await setup_loaded(hass, entry)
    assert baby_value(hass, "feeds_today") == "3"


async def test_other_feeds_pump_sessions_and_diapers_are_not_feeds(
    hass: HomeAssistant, entry: MockConfigEntry, empty: FakeServer
) -> None:
    empty.add_record(DEVICE_A, "OtherFeed", _id(), when=TODAY, amount=2.5)
    empty.add_record(DEVICE_A, "Pump", _id(), when=TODAY, amount=volume(100, english=False))
    _diaper(empty, TODAY)
    empty.add_record(DEVICE_A, "Sleep", _id(), when=TODAY, duration=30)
    await setup_loaded(hass, entry)
    assert baby_value(hass, "feeds_today") == "0"


async def test_a_deleted_feed_is_not_counted(
    hass: HomeAssistant, entry: MockConfigEntry, empty: FakeServer
) -> None:
    gone = _id()
    empty.add_record(DEVICE_A, "Formula", gone, when=TODAY, amount=volume(4))
    empty.add_record(DEVICE_A, "Formula", gone, when=TODAY, amount=volume(4), op=OP_DELETE)
    empty.add_record(DEVICE_A, "Nursing", _id(), when=TODAY, leftDuration=5)
    await setup_loaded(hass, entry)
    assert baby_value(hass, "feeds_today") == "1"


async def test_the_feed_count_does_not_mix_babies(
    hass: HomeAssistant, entry: MockConfigEntry, empty: FakeServer
) -> None:
    empty.add_record(DEVICE_A, "Nursing", _id(), when=TODAY, leftDuration=5)
    empty.add_record(DEVICE_A, "Nursing", _id(), when=TODAY, baby=BABY_TWO, leftDuration=5)
    empty.add_record(DEVICE_A, "Nursing", _id(), when=TODAY, baby=BABY_TWO, leftDuration=5)
    await setup_loaded(hass, entry)
    assert baby_value(hass, "feeds_today", "baby_one") == "1"
    assert baby_value(hass, "feeds_today", "baby_two") == "2"


# Volume


async def test_mixed_milliliter_and_ounce_bottles_sum_in_milliliters_for_metric(
    hass: HomeAssistant, entry: MockConfigEntry, empty: FakeServer
) -> None:
    hass.config.units = METRIC_SYSTEM
    empty.add_record(DEVICE_A, "Formula", _id(), when=TODAY, amount=volume(120, english=False))
    empty.add_record(DEVICE_A, "Pumped", _id(), when=TODAY, amount=volume(4))
    await setup_loaded(hass, entry)
    state = baby_state(hass, "bottle_volume_today")
    assert state.attributes["unit_of_measurement"] == "mL"
    assert float(state.state) == pytest.approx(120 + 4 * ML_PER_OZ, abs=0.1)


async def test_mixed_milliliter_and_ounce_bottles_sum_in_ounces_for_us_customary(
    hass: HomeAssistant, entry: MockConfigEntry, empty: FakeServer
) -> None:
    hass.config.units = US_CUSTOMARY_SYSTEM
    empty.add_record(DEVICE_A, "Formula", _id(), when=TODAY, amount=volume(120, english=False))
    empty.add_record(DEVICE_A, "Pumped", _id(), when=TODAY, amount=volume(4))
    await setup_loaded(hass, entry)
    state = baby_state(hass, "bottle_volume_today")
    assert state.attributes["unit_of_measurement"] == "fl. oz."
    assert float(state.state) == pytest.approx(120 / ML_PER_OZ + 4, abs=0.01)


async def test_one_ounce_bottle_reads_as_ounces_for_us_customary(
    hass: HomeAssistant, entry: MockConfigEntry, empty: FakeServer
) -> None:
    hass.config.units = US_CUSTOMARY_SYSTEM
    empty.add_record(DEVICE_A, "Formula", _id(), when=TODAY, amount=volume(5))
    await setup_loaded(hass, entry)
    state = baby_state(hass, "bottle_volume_today")
    assert state.attributes["unit_of_measurement"] == "fl. oz."
    assert float(state.state) == pytest.approx(5, abs=0.01)


async def test_only_bottles_from_today_are_summed(
    hass: HomeAssistant, entry: MockConfigEntry, empty: FakeServer
) -> None:
    hass.config.units = METRIC_SYSTEM
    empty.add_record(DEVICE_A, "Formula", _id(), when=TODAY, amount=volume(100, english=False))
    empty.add_record(DEVICE_A, "Formula", _id(), when=YESTERDAY, amount=volume(500, english=False))
    await setup_loaded(hass, entry)
    assert float(baby_value(hass, "bottle_volume_today")) == pytest.approx(100)


async def test_pump_sessions_nursing_and_other_feeds_add_no_volume(
    hass: HomeAssistant, entry: MockConfigEntry, empty: FakeServer
) -> None:
    hass.config.units = METRIC_SYSTEM
    empty.add_record(DEVICE_A, "Pump", _id(), when=TODAY, amount=volume(200, english=False))
    empty.add_record(DEVICE_A, "OtherFeed", _id(), when=TODAY, amount=50.0)
    empty.add_record(DEVICE_A, "Nursing", _id(), when=TODAY, leftDuration=5)
    empty.add_record(DEVICE_A, "Formula", _id(), when=TODAY, amount=volume(60, english=False))
    await setup_loaded(hass, entry)
    assert float(baby_value(hass, "bottle_volume_today")) == pytest.approx(60)


async def test_a_bottle_with_no_readable_unit_counts_as_a_feed_but_adds_no_volume(
    hass: HomeAssistant, entry: MockConfigEntry, empty: FakeServer
) -> None:
    hass.config.units = METRIC_SYSTEM
    # A plain number with no scale has no unit, so it can't be added to anything.
    empty.add_record(DEVICE_A, "Formula", _id(), when=TODAY, amount=3)
    empty.add_record(DEVICE_A, "Formula", _id(), when=TODAY, amount=volume(60, english=False))
    await setup_loaded(hass, entry)
    assert baby_value(hass, "feeds_today") == "2"
    assert float(baby_value(hass, "bottle_volume_today")) == pytest.approx(60)


async def test_a_deleted_bottle_is_not_summed(
    hass: HomeAssistant, entry: MockConfigEntry, empty: FakeServer
) -> None:
    hass.config.units = METRIC_SYSTEM
    gone = _id()
    empty.add_record(DEVICE_A, "Formula", gone, when=TODAY, amount=volume(100, english=False))
    empty.add_record(
        DEVICE_A, "Formula", gone, when=TODAY, amount=volume(100, english=False), op=OP_DELETE
    )
    await setup_loaded(hass, entry)
    assert float(baby_value(hass, "bottle_volume_today")) == 0


async def test_a_bottle_edited_to_a_new_amount_changes_the_total(
    hass: HomeAssistant, entry: MockConfigEntry, empty: FakeServer
) -> None:
    hass.config.units = METRIC_SYSTEM
    bottle = _id()
    empty.add_record(DEVICE_A, "Formula", bottle, when=TODAY, amount=volume(100, english=False))
    await setup_loaded(hass, entry)
    empty.add_record(
        DEVICE_A,
        "Formula",
        bottle,
        when=TODAY,
        changed=LOCAL_NOW,
        amount=volume(80, english=False),
        op=OP_UPDATE,
    )
    await tick(hass, minutes=1440)
    assert float(baby_value(hass, "bottle_volume_today")) == pytest.approx(80)


# A baby with nothing logged


async def test_a_baby_with_no_records_has_zero_counts_and_is_awake(
    hass: HomeAssistant, entry: MockConfigEntry, empty: FakeServer
) -> None:
    empty.add_payload(DEVICE_A, BABY_THREE)
    await setup_loaded(hass, entry)
    assert baby_value(hass, "diapers_today", "baby_three") == "0"
    assert baby_value(hass, "feeds_today", "baby_three") == "0"
    assert float(baby_value(hass, "bottle_volume_today", "baby_three")) == 0
    assert baby_value(hass, "asleep", "baby_three") == "off"
    assert _breakdown(baby_state(hass, "diapers_today", "baby_three")) == (0, 0, 0)


async def test_a_baby_whose_records_are_all_old_has_zero_counts(
    hass: HomeAssistant, entry: MockConfigEntry, empty: FakeServer
) -> None:
    _diaper(empty, YESTERDAY)
    empty.add_record(DEVICE_A, "Formula", _id(), when=YESTERDAY, amount=volume(4))
    await setup_loaded(hass, entry)
    assert baby_value(hass, "diapers_today") == "0"
    assert baby_value(hass, "feeds_today") == "0"


# Raw records


async def test_a_raw_record_the_library_cannot_model_is_ignored_by_the_counts(
    hass: HomeAssistant, entry: MockConfigEntry, empty: FakeServer
) -> None:
    hass.config.units = METRIC_SYSTEM
    stamp = TODAY.strftime("%Y-%m-%d %H:%M:%S %z")
    # Known types whose values the models reject are kept raw, like a type the library lacks.
    for kind, fields in (
        ("Diaper", {}),
        ("Formula", {"amount": volume(500, english=False)}),
        ("Sleep", {"duration": 0}),
        ("Hiccups", {}),
    ):
        empty.add_payload(
            DEVICE_A,
            {
                "BCObjectType": kind,
                "objectID": _id(),
                "time": stamp,
                "timestamp": "not a time",
                "baby": BABY_ONE,
                **fields,
            },
        )
    _diaper(empty, TODAY)
    await setup_loaded(hass, entry)
    assert baby_value(hass, "diapers_today") == "1"
    assert baby_value(hass, "feeds_today") == "0"
    assert float(baby_value(hass, "bottle_volume_today")) == 0
    assert baby_value(hass, "asleep") == "off"


# Midnight


async def test_the_counts_reset_at_local_midnight_without_a_sync(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    empty: FakeServer,
    clock: FrozenDateTimeFactory,
) -> None:
    hass.config.units = METRIC_SYSTEM
    _diaper(empty, TODAY)
    empty.add_record(DEVICE_A, "Formula", _id(), when=TODAY, amount=volume(100, english=False))
    await setup_loaded(hass, entry)
    assert baby_value(hass, "diapers_today") == "1"
    assert baby_value(hass, "feeds_today") == "1"
    reads = len(empty.transaction_reads)
    listed = empty.device_list_calls

    await pass_midnight(hass, clock)

    assert baby_value(hass, "diapers_today") == "0"
    assert baby_value(hass, "feeds_today") == "0"
    assert float(baby_value(hass, "bottle_volume_today")) == 0
    assert _breakdown(baby_state(hass, "diapers_today")) == (0, 0, 0)
    # The reset came from the clock, not from asking the service.
    assert len(empty.transaction_reads) == reads
    assert empty.device_list_calls == listed


async def test_the_counts_do_not_reset_before_midnight(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    empty: FakeServer,
    clock: FrozenDateTimeFactory,
) -> None:
    _diaper(empty, TODAY)
    await setup_loaded(hass, entry)
    clock.move_to(local(2026, 10, 26, 23, 59, 0))
    async_fire_time_changed(hass, local(2026, 10, 26, 23, 59, 0))
    await hass.async_block_till_done()
    assert baby_value(hass, "diapers_today") == "1"


async def test_the_counts_reset_again_every_midnight(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    empty: FakeServer,
    clock: FrozenDateTimeFactory,
) -> None:
    _diaper(empty, local(2026, 10, 27, 8))
    await setup_loaded(hass, entry)
    assert baby_value(hass, "diapers_today") == "0"
    await pass_midnight(hass, clock, MIDNIGHT)
    assert baby_value(hass, "diapers_today") == "1"
    await pass_midnight(hass, clock, local(2026, 10, 28))
    assert baby_value(hass, "diapers_today") == "0"


async def test_a_record_exactly_at_midnight_belongs_to_the_new_day(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    empty: FakeServer,
    clock: FrozenDateTimeFactory,
) -> None:
    clock.move_to(local(2026, 10, 26, 12))
    _diaper(empty, local(2026, 10, 26, 0, 0, 0))  # the start of today: counted
    _diaper(empty, local(2026, 10, 25, 23, 59, 59))  # the last second of yesterday: not counted
    _diaper(empty, MIDNIGHT)  # the start of tomorrow: not counted yet
    await setup_loaded(hass, entry)
    assert baby_value(hass, "diapers_today") == "1"

    await pass_midnight(hass, clock)
    assert baby_value(hass, "diapers_today") == "1"  # now the record at midnight, and only it


# Daylight saving time


async def test_a_fall_back_day_still_counts_the_local_calendar_day(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    empty: FakeServer,
    clock: FrozenDateTimeFactory,
) -> None:
    # Sunday, November 1, 2026 is 25 hours long in New York: 00:00 EDT to 00:00 EST.
    clock.move_to(local(2026, 11, 1, 23, 40))
    _diaper(empty, local(2026, 11, 1, 0, 0, 0))  # the first moment of the day
    _diaper(empty, local(2026, 11, 1, 23, 30))  # more than 24 hours after the first
    _diaper(empty, local(2026, 10, 31, 23, 59, 59))  # the day before
    _diaper(empty, local(2026, 11, 2, 0, 0, 0))  # the day after
    await setup_loaded(hass, entry)
    assert baby_value(hass, "diapers_today") == "2"

    await pass_midnight(hass, clock, local(2026, 11, 2))
    assert baby_value(hass, "diapers_today") == "1"


async def test_a_spring_forward_day_still_counts_the_local_calendar_day(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    empty: FakeServer,
    clock: FrozenDateTimeFactory,
) -> None:
    # Sunday, March 8, 2026 is 23 hours long in New York: 00:00 EST to 00:00 EDT.
    clock.move_to(local(2026, 3, 8, 12))
    _diaper(empty, local(2026, 3, 8, 0, 0, 0))
    _diaper(empty, local(2026, 3, 8, 23, 59, 59))
    _diaper(empty, local(2026, 3, 7, 23, 59, 59))
    _diaper(empty, local(2026, 3, 9, 0, 0, 0))
    await setup_loaded(hass, entry)
    assert baby_value(hass, "diapers_today") == "2"

    await pass_midnight(hass, clock, local(2026, 3, 9))
    assert baby_value(hass, "diapers_today") == "1"


# --- the account-level device and sensor ------------------------------------------------------


async def test_last_pump_is_one_sensor_on_an_account_device_named_for_the_entry(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    device = account_device(hass, init_integration)

    assert device.name == "Baby Tracker account"
    assert device.model == "Account"
    assert device.config_entry_id == init_integration.entry_id
    assert state_of(hass, init_integration).attributes["friendly_name"] == (
        "Baby Tracker account Last pump"
    )


async def test_the_account_email_is_nowhere_in_the_entity_id_or_the_device(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    assert pump_entity(hass, init_integration) == "sensor.baby_tracker_account_last_pump"
    device = account_device(hass, init_integration)
    assert EMAIL not in str(device.name) + str(device.model) + str(device.configuration_url)


async def test_the_account_device_holds_only_the_last_pump_sensor(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    device = account_device(hass, init_integration)

    entries = er.async_entries_for_device(er.async_get(hass), device.id)

    assert [e.unique_id for e in entries] == [f"{init_integration.entry_id}_last_pump"]
    assert entries[0].translation_key == "last_pump"


async def test_no_baby_has_a_last_pump_entity(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    unique_ids = {
        e.unique_id
        for e in er.async_entries_for_config_entry(er.async_get(hass), init_integration.entry_id)
    }

    assert unique_ids.isdisjoint({f"{BABY_ONE_ID}_last_pump", f"{BABY_TWO_ID}_last_pump"})
    assert hass.states.get("sensor.baby_one_last_pump") is None
    assert hass.states.get("sensor.baby_two_last_pump") is None


async def test_the_account_device_is_not_a_baby(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    devices = dr.async_entries_for_config_entry(dr.async_get(hass), init_integration.entry_id)

    assert sorted(d.name for d in devices if d.model != "Account") == ["Baby One", "Baby Two"]
    assert len([d for d in devices if d.model == "Account"]) == 1


async def test_last_pump_is_unknown_when_nothing_was_pumped(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    state = state_of(hass, init_integration)

    assert state.state == "unknown"
    assert "amount" not in state.attributes


async def test_last_pump_shows_the_newest_pump_whoever_logged_it(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    server.add_pump(DEVICE_A, _pump_id(1), when=at(30), amount=volume(4), note="older")
    server.add_pump(DEVICE_B, _pump_id(2), when=at(90), amount=volume(5), note="newest")
    server.add_pump(DEVICE_A, _pump_id(3), when=at(60), amount=volume(6), note="middle")
    await tick(hass)

    state = state_of(hass, init_integration)
    assert state.state == at(90).isoformat()
    assert state.attributes["note"] == "newest"
    assert state.attributes["amount"] == 5


async def test_last_pump_shows_amounts_and_durations(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    server.add_pump(
        DEVICE_A,
        _pump_id(4),
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

    state = state_of(hass, init_integration)
    assert state.state == at(45).isoformat()
    assert state.attributes["amount"] == 5
    assert state.attributes["unit"] == "fl. oz."
    assert state.attributes["left_amount"] == 2
    assert state.attributes["right_amount"] == 3
    assert state.attributes["left_duration_minutes"] == 10
    assert state.attributes["right_duration_minutes"] == 12
    assert state.attributes["sides"] == 3
    assert state.attributes["note"] == "morning"


async def test_a_total_only_pump_has_no_side_amounts(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    server.add_pump(
        DEVICE_A, _pump_id(5), when=at(45), amount=volume(4), leftDuration=5, rightDuration=7
    )
    await tick(hass)

    state = state_of(hass, init_integration)
    assert state.attributes["amount"] == 4
    assert "left_amount" not in state.attributes
    assert "right_amount" not in state.attributes


async def test_a_pumped_bottle_is_not_a_pump_session(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    server.add_record(DEVICE_A, "Pumped", _pump_id(6), when=at(45), amount=volume(3))
    await tick(hass)

    assert state_of(hass, init_integration).state == "unknown"


async def test_a_deleted_pump_falls_back_to_the_one_before(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    server.add_pump(DEVICE_A, _pump_id(7), when=at(30), amount=volume(4))
    server.add_pump(DEVICE_A, _pump_id(8), when=at(90), amount=volume(5))
    await tick(hass)
    assert state_of(hass, init_integration).state == at(90).isoformat()

    server.add_record(DEVICE_A, "Pump", _pump_id(8), when=at(90), baby=None, op=2, amount=volume(5))
    await tick(hass)

    assert state_of(hass, init_integration).state == at(30).isoformat()


async def test_last_pump_survives_a_reload_from_saved_state(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    server.add_pump(DEVICE_A, _pump_id(9), when=at(45), amount=volume(5))
    await tick(hass)

    assert await hass.config_entries.async_reload(init_integration.entry_id)
    await hass.async_block_till_done()

    assert state_of(hass, init_integration).state == at(45).isoformat()


async def test_a_pump_creates_no_baby_and_changes_no_babys_counts(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    server.add_pump(DEVICE_A, _pump_id(10), when=at(5), amount=volume(5))
    await tick(hass)

    devices = dr.async_entries_for_config_entry(dr.async_get(hass), init_integration.entry_id)
    assert len(devices) == 3  # two babies and the account
    for baby in ("baby_one", "baby_two"):
        assert hass.states.get(f"sensor.{baby}_feeds_today").state == "0"
        assert float(hass.states.get(f"sensor.{baby}_bottle_volume_today").state) == 0


async def test_an_old_style_pump_that_carries_a_baby_creates_no_baby_and_is_not_that_babys(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    ghost = baby_payload("Ghost Baby", BABY_THREE_ID)
    server.add_record(DEVICE_A, "Pump", _pump_id(11), when=at(70), baby=ghost, amount=volume(5))
    server.add_record(DEVICE_A, "Pump", _pump_id(12), when=at(80), baby=None, amount=volume(2))
    await tick(hass)

    assert get_baby_device(dr.async_get(hass), BABY_THREE_ID) is None
    assert hass.states.get("sensor.ghost_baby_last_diaper") is None
    assert state_of(hass, init_integration).state == at(80).isoformat()
    assert hass.states.get("sensor.baby_one_feeds_today").state == "0"


# --- two accounts: each has its own device and pump ----------------------------------------------


async def test_two_entries_each_have_their_own_account_device_and_sensor(
    hass: HomeAssistant, init_integration: MockConfigEntry, second_entry: MockConfigEntry
) -> None:
    first, second = account_device(hass, init_integration), account_device(hass, second_entry)

    assert first.id != second.id
    assert first.name == second.name == "Baby Tracker account"
    assert pump_entity(hass, init_integration) == "sensor.baby_tracker_account_last_pump"
    assert pump_entity(hass, second_entry) == "sensor.baby_tracker_account_last_pump_2"
    assert first.config_entry_id == init_integration.entry_id
    assert second.config_entry_id == second_entry.entry_id
    assert pump_entity(hass, init_integration) != pump_entity(hass, second_entry)


async def test_each_entrys_last_pump_shows_only_its_own_accounts_pump(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    second_entry: MockConfigEntry,
    server: FakeServer,
    second_server: FakeServer,
) -> None:
    server.add_pump(DEVICE_A, _pump_id(20), when=at(40), amount=volume(4))
    second_server.add_pump(DEVICE_B, _pump_id(21), when=at(80), amount=volume(6))
    await tick(hass)

    assert state_of(hass, init_integration).state == at(40).isoformat()
    assert state_of(hass, second_entry).state == at(80).isoformat()
    assert state_of(hass, init_integration).attributes["amount"] == 4
    assert state_of(hass, second_entry).attributes["amount"] == 6


# Entities


async def test_each_baby_gets_two_timer_sensors_and_two_nursing_duration_sensors(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    devices = dr.async_get(hass)
    entities = er.async_get(hass)
    for baby_id in (BABY_ONE_ID, BABY_TWO_ID):
        device = get_baby_device(devices, baby_id)
        assert device is not None
        keys = {
            e.translation_key
            for e in er.async_entries_for_device(entities, device.id)
            if e.domain == "sensor"
        }
        assert {"sleep_timer", "nursing_timer", "nursing_left_time", "nursing_right_time"} <= keys
    assert state(hass, SLEEP_TIMER).state == "unknown"
    assert state(hass, NURSING_TIMER).state == "unknown"
    assert state(hass, SLEEP_TIMER).attributes["device_class"] == "timestamp"
    assert state(hass, NURSING_TIMER).attributes["device_class"] == "timestamp"


# Sleep: reading a phone's running sleep


async def test_a_running_sleep_synced_from_a_phone_turns_asleep_on_and_shows_its_start(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, freezer: Any
) -> None:
    freezer.move_to(NOW)
    phone_sleep(server, start=minutes(-40))

    await sync(hass, init_integration)

    assert state(hass, ASLEEP).state == "on"
    timer = state(hass, SLEEP_TIMER)
    assert timer.state == minutes(-40).isoformat()
    assert timer.attributes["object_id"] == PHONE_SLEEP


async def test_a_sleep_a_phone_ends_while_home_assistant_shows_it_running_clears_next_sync(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, freezer: Any
) -> None:
    freezer.move_to(NOW)
    phone_sleep(server, start=minutes(-40))
    await sync(hass, init_integration)
    assert state(hass, ASLEEP).state == "on"

    phone_ends_sleep(server, PHONE_SLEEP, start=minutes(-40), duration=40, changed=NOW)
    await tick(hass, minutes=1)

    assert state(hass, ASLEEP).state == "off"
    assert state(hass, SLEEP_TIMER).state == "unknown"
    assert state(hass, LAST_SLEEP).attributes["duration_minutes"] == 40


# Baby two's sleep, from a phone


async def test_a_running_sleep_for_one_baby_leaves_the_other_awake(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, freezer: Any
) -> None:
    freezer.move_to(NOW)
    phone_sleep(server, PHONE_SLEEP_TWO, start=minutes(-15), baby=BABY_TWO)

    await sync(hass, init_integration)

    assert state(hass, TWO_ASLEEP).state == "on"
    assert state(hass, TWO_SLEEP_TIMER).state == minutes(-15).isoformat()
    assert state(hass, ASLEEP).state == "off"
    assert state(hass, SLEEP_TIMER).state == "unknown"


# Coordinator data


async def test_a_refresh_with_nothing_new_writes_no_states(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    reported: list[Event] = []

    @callback
    def record(event: Event) -> None:
        reported.append(event)

    @callback
    def is_ours(data: dict[str, Any]) -> bool:
        return data["entity_id"].split(".")[1].startswith(("baby_", "baby_tracker"))

    hass.bus.async_listen(EVENT_STATE_REPORTED, record, event_filter=is_ours)
    await tick(hass)
    assert reported == []


async def test_a_refresh_with_a_new_record_still_updates_the_sensor(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    server.add_record(
        "BBBBBBBB-0000-4000-8000-00000000000B",
        "Diaper",
        "D2000000-0000-4000-8000-000000000001",
        when=at(60),
    )
    await tick(hass)
    assert hass.states.get(DIAPER).state == at(60).isoformat()


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


async def test_a_pump_alone_reaches_the_account_sensor_on_a_refresh(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    """The last pump lives in the sync data, so a pump-only change isn't dropped."""
    assert hass.states.get(LAST_PUMP).state == "unknown"
    server.add_pump(
        "BBBBBBBB-0000-4000-8000-00000000000B", "F0000000-0000-4000-8000-000000000001", when=at(45)
    )
    await tick(hass)
    assert hass.states.get(LAST_PUMP).state == at(45).isoformat()


# Nursing left and right: the time on each side this session


async def _tick_seconds(hass: HomeAssistant, freezer: Any, seconds: float) -> None:
    """Move the clock and fire the timers due, without any sync being asked for."""
    freezer.tick(timedelta(seconds=seconds))
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()


async def test_the_duration_sensors_are_unknown_with_no_session(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    for entity_id in (NURSING_LEFT_TIME, NURSING_RIGHT_TIME):
        found = state(hass, entity_id)
        assert found.state == "unknown"
        assert found.attributes["device_class"] == "duration"
        assert found.attributes["unit_of_measurement"] == "min"


async def test_the_running_sides_time_counts_up_with_no_sync(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    server: FakeServer,
    freezer: Any,
) -> None:
    await turn_on(hass, NURSE_LEFT)
    assert state(hass, NURSING_LEFT_TIME).state == "0.0"
    assert state(hass, NURSING_RIGHT_TIME).state == "0.0"
    reads = server.device_list_calls

    await _tick_seconds(hass, freezer, 31)  # inside the minute, so no sync is due
    assert server.device_list_calls == reads
    assert state(hass, NURSING_LEFT_TIME).state == "0.5"
    await _tick_seconds(hass, freezer, 29)
    await _tick_seconds(hass, freezer, 60)

    assert state(hass, NURSING_LEFT_TIME).state == "2.0"
    assert state(hass, NURSING_RIGHT_TIME).state == "0.0"
    assert server.posted == []


async def test_the_sensors_update_at_least_every_minute_without_a_sync(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    server: FakeServer,
    freezer: Any,
) -> None:
    await turn_on(hass, NURSE_LEFT)
    changes: list[str] = []

    @callback
    def record(event: Event[Any]) -> None:
        changes.append(event.data["new_state"].state)

    @callback
    def only_left(data: Any) -> bool:
        return bool(data["entity_id"] == NURSING_LEFT_TIME)

    hass.bus.async_listen("state_changed", record, event_filter=only_left)
    for _ in range(4):
        await _tick_seconds(hass, freezer, 30)

    assert len(changes) >= 2  # at least one write per minute
    assert changes[-1] == "2.0"


async def test_the_time_on_each_side_across_a_side_switch(
    hass: HomeAssistant, init_integration: MockConfigEntry, freezer: Any
) -> None:
    await turn_on(hass, NURSE_LEFT)
    freezer.move_to(minutes(5))
    await turn_on(hass, NURSE_RIGHT)
    assert state(hass, NURSING_LEFT_TIME).state == "5.0"
    assert state(hass, NURSING_RIGHT_TIME).state == "0.0"

    await _tick_seconds(hass, freezer, 180)

    assert state(hass, NURSING_LEFT_TIME).state == "5.0"  # the finished side holds still
    assert state(hass, NURSING_RIGHT_TIME).state == "3.0"

    freezer.move_to(minutes(12))
    await turn_on(hass, NURSE_LEFT)  # back to the left: both keep what they have
    assert state(hass, NURSING_LEFT_TIME).state == "5.0"
    assert state(hass, NURSING_RIGHT_TIME).state == "7.0"
    await _tick_seconds(hass, freezer, 60)
    assert state(hass, NURSING_LEFT_TIME).state == "6.0"
    assert state(hass, NURSING_RIGHT_TIME).state == "7.0"


async def test_the_duration_sensors_are_unknown_again_after_a_stop(
    hass: HomeAssistant, init_integration: MockConfigEntry, freezer: Any
) -> None:
    await turn_on(hass, NURSE_LEFT)
    freezer.move_to(minutes(4))
    await turn_on(hass, NURSE_RIGHT)
    freezer.move_to(minutes(6))

    await turn_off(hass, NURSE_RIGHT)

    assert state(hass, NURSING_LEFT_TIME).state == "unknown"
    assert state(hass, NURSING_RIGHT_TIME).state == "unknown"
    await _tick_seconds(hass, freezer, 120)
    assert state(hass, NURSING_LEFT_TIME).state == "unknown"


async def test_the_duration_sensors_are_unknown_after_a_discard(
    hass: HomeAssistant, init_integration: MockConfigEntry, freezer: Any
) -> None:
    await turn_on(hass, NURSE_LEFT)
    freezer.move_to(minutes(4))
    await hass.services.async_call(
        "button", "press", {"entity_id": "button.baby_one_discard_nursing"}, blocking=True
    )

    assert state(hass, NURSING_LEFT_TIME).state == "unknown"


async def test_a_new_session_after_a_stop_starts_from_zero(
    hass: HomeAssistant, init_integration: MockConfigEntry, freezer: Any
) -> None:
    await toggle(hass, NURSE_LEFT)
    freezer.move_to(minutes(9))
    await toggle(hass, NURSE_LEFT)
    freezer.move_to(minutes(20))

    await toggle(hass, NURSE_RIGHT)

    assert state(hass, NURSING_LEFT_TIME).state == "0.0"
    assert state(hass, NURSING_RIGHT_TIME).state == "0.0"


async def test_two_babies_duration_sensors_run_independently(
    hass: HomeAssistant, init_integration: MockConfigEntry, freezer: Any
) -> None:
    await turn_on(hass, NURSE_LEFT)
    freezer.move_to(minutes(2))
    await turn_on(hass, "switch.baby_two_nursing_right")
    await _tick_seconds(hass, freezer, 180)

    assert state(hass, NURSING_LEFT_TIME).state == "5.0"
    assert state(hass, "sensor.baby_two_nursing_left_time").state == "0.0"
    assert state(hass, "sensor.baby_two_nursing_right_time").state == "3.0"


async def test_the_duration_sensors_hold_their_value_when_the_clock_goes_backwards(
    hass: HomeAssistant, init_integration: MockConfigEntry, freezer: Any
) -> None:
    await turn_on(hass, NURSE_LEFT)
    freezer.move_to(NOW - timedelta(minutes=5))
    async_fire_time_changed(hass, NOW - timedelta(minutes=5))
    await hass.async_block_till_done()

    assert state(hass, NURSING_LEFT_TIME).state == "0.0"  # never negative


async def test_a_session_saved_before_a_restart_shows_its_time_after_it(
    hass: HomeAssistant, init_integration: MockConfigEntry, freezer: Any
) -> None:
    await turn_on(hass, NURSE_LEFT)
    freezer.move_to(minutes(3))
    await turn_on(hass, NURSE_RIGHT)
    freezer.move_to(minutes(5))

    await hass.config_entries.async_reload(init_integration.entry_id)
    await hass.async_block_till_done()

    assert state(hass, NURSING_LEFT_TIME).state == "3.0"
    assert state(hass, NURSING_RIGHT_TIME).state == "2.0"
    assert state(hass, NURSE_RIGHT).state == "on"


async def test_the_duration_sensors_follow_a_sync_failure_like_the_other_sensors(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    await turn_on(hass, NURSE_LEFT)
    server.request_error = BabyTrackerConnectionError("down")

    await sync(hass, init_integration)

    assert state(hass, NURSING_LEFT_TIME).state == "unavailable"
