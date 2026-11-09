"""Core PR 28: a last activity sensor for each activity type, and the log_activity action.

The app has no screen for some things people log, such as a bath: they add an activity type of
their own in the app and log it as an activity. So every type, built in or added, gets the same
sensor, and nothing matches a type by its name.
"""

from typing import Any
from unittest.mock import patch

import probatio
import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er
from pybabytracker import OtherActivity
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.baby_tracker.const import DOMAIN

from . import BABY_THREE, BABY_TWO, NOW, tick
from .factories import (
    BABY_ONE_ID,
    BABY_THREE_ID,
    BABY_TWO_ID,
    DEVICE_A,
    DEVICE_B,
    OP_ADD,
    OP_DELETE,
    at,
)
from .fake_server import FakeServer
from .helpers import device_id, posted

BATH_ID = "A0000000-0000-4000-8000-0000000000BA"
SWIM_ID = "A0000000-0000-4000-8000-0000000000BB"
TUMMY_TIME_ID = "EasyLogSeed3559dca0-8cbf-11e2-9e96-0800200c9a66"
LAST_BATH = "sensor.baby_one_last_bath"
LAST_TUMMY_TIME = "sensor.baby_one_last_tummy_time"
# An activity type added in the app, as an iPhone embeds it in an activity. Invented values.
BATH = {
    "BCObjectType": "OtherActivityDescription",
    "objectID": BATH_ID,
    "name": "Bath",
    "newFlage": "false",
    "timestamp": "1970-01-01 00:00:00 +0000",
}
# The same type as an Android phone embeds it: no BCObjectType, and its own timestamp.
ANDROID_BATH = {
    "objectID": BATH_ID,
    "name": "Bath",
    "newFlage": False,
    "timestamp": "2026-04-01 00:00:00 +0000",
}
SWIM = {**BATH, "objectID": SWIM_ID, "name": "Swim"}
TUMMY_TIME = {
    "BCObjectType": "OtherActivityDescription",
    "objectID": TUMMY_TIME_ID,
    "name": "Tummy Time",
}


@pytest.fixture(autouse=True)
def _start_at_now(freezer: FrozenDateTimeFactory) -> None:
    """Freeze the clock at NOW before anything is set up."""
    freezer.move_to(NOW)


def _activity(
    server: FakeServer,
    number: int,
    minute: float,
    desc: Any,
    *,
    device: str = DEVICE_A,
    **fields: Any,
) -> None:
    server.add_record(
        device,
        "OtherActivity",
        f"B0000000-0000-4000-8000-{number:012d}",
        when=at(minute),
        desc=desc,
        duration=fields.pop("duration", 0),
        **fields,
    )


def _state(hass: HomeAssistant, entity_id: str) -> Any:
    state = hass.states.get(entity_id)
    assert state is not None, f"{entity_id} does not exist"
    return state


def _activity_entities(hass: HomeAssistant, entry: MockConfigEntry) -> dict[str, str]:
    """Each last activity sensor's unique ID, by entity ID."""
    return {
        e.entity_id: e.unique_id
        for e in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
        if "_last_activity_" in e.unique_id
    }


async def _log(hass: HomeAssistant, activity: Any, **extra: Any) -> None:
    data = {"device_id": device_id(hass, BABY_ONE_ID), "activity": activity, **extra}
    await hass.services.async_call(DOMAIN, "log_activity", data, blocking=True)


# The sensors


async def test_a_baby_without_activities_has_no_last_activity_sensor(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    assert _activity_entities(hass, init_integration) == {}


async def test_the_first_activity_of_a_type_adds_its_sensor_without_a_reload(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    _activity(server, 1, 30, BATH, duration=10, note="Bubbles")
    await tick(hass)

    state = _state(hass, LAST_BATH)
    assert state.state == at(30).isoformat()
    assert state.attributes["device_class"] == "timestamp"
    assert state.attributes["duration_minutes"] == 10
    assert state.attributes["note"] == "Bubbles"
    assert state.attributes["friendly_name"] == "Baby One Last Bath"
    assert _activity_entities(hass, init_integration) == {
        LAST_BATH: f"{BABY_ONE_ID}_last_activity_{BATH_ID}"
    }


async def test_each_type_has_its_own_sensor_showing_its_newest_activity(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    _activity(server, 1, 30, BATH, note="First")
    _activity(server, 2, 60, ANDROID_BATH, duration=12, note="Second", device=DEVICE_B)
    _activity(server, 3, 45, BATH, note="Between")
    _activity(server, 4, 90, TUMMY_TIME, duration=15)
    await tick(hass)

    bath = _state(hass, LAST_BATH)
    assert bath.state == at(60).isoformat()
    assert (bath.attributes["duration_minutes"], bath.attributes["note"]) == (12, "Second")
    tummy = _state(hass, LAST_TUMMY_TIME)
    assert tummy.state == at(90).isoformat()
    assert tummy.attributes["duration_minutes"] == 15
    assert tummy.attributes["note"] is None
    assert tummy.attributes["friendly_name"] == "Baby One Last Tummy Time"


async def test_a_newer_activity_moves_the_sensor(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    _activity(server, 1, 30, BATH)
    await tick(hass)
    _activity(server, 2, 120, BATH, note="Later")
    await tick(hass)

    assert _state(hass, LAST_BATH).state == at(120).isoformat()
    assert len(_activity_entities(hass, init_integration)) == 1


async def test_an_older_activity_synced_later_does_not_move_the_sensor(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    _activity(server, 1, 90, BATH, note="Newest")
    await tick(hass)
    _activity(server, 2, 30, BATH, note="Logged late")
    await tick(hass)

    state = _state(hass, LAST_BATH)
    assert (state.state, state.attributes["note"]) == (at(90).isoformat(), "Newest")


async def test_activities_at_the_same_time_resolve_the_same_way_every_time(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    """Test the one saved later wins, whatever order the records arrive in."""
    _activity(server, 2, 30, BATH, note="Saved first", changed=at(40))
    _activity(server, 1, 30, BATH, note="Saved later", changed=at(50))
    await tick(hass)

    assert _state(hass, LAST_BATH).attributes["note"] == "Saved later"


async def test_another_babys_activity_is_its_own(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    _activity(server, 1, 30, BATH)
    _activity(server, 2, 60, BATH, baby=BABY_TWO)
    _activity(server, 3, 90, SWIM, baby=BABY_TWO)
    await tick(hass)

    assert _state(hass, LAST_BATH).state == at(30).isoformat()
    assert _state(hass, "sensor.baby_two_last_bath").state == at(60).isoformat()
    assert _state(hass, "sensor.baby_two_last_swim").state == at(90).isoformat()
    assert hass.states.get("sensor.baby_one_last_swim") is None
    assert set(_activity_entities(hass, init_integration).values()) == {
        f"{BABY_ONE_ID}_last_activity_{BATH_ID}",
        f"{BABY_TWO_ID}_last_activity_{BATH_ID}",
        f"{BABY_TWO_ID}_last_activity_{SWIM_ID}",
    }


async def test_two_types_with_one_name_get_two_sensors(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    _activity(server, 1, 30, BATH)
    _activity(server, 2, 60, {**SWIM, "name": "Bath"})
    await tick(hass)

    assert set(_activity_entities(hass, init_integration).values()) == {
        f"{BABY_ONE_ID}_last_activity_{BATH_ID}",
        f"{BABY_ONE_ID}_last_activity_{SWIM_ID}",
    }
    states = {
        hass.states.get(entity_id).state  # type: ignore[union-attr]
        for entity_id in _activity_entities(hass, init_integration)
    }
    assert states == {at(30).isoformat(), at(60).isoformat()}


async def test_renaming_a_type_keeps_its_sensor_and_names_it_anew_on_reload(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    _activity(server, 1, 30, BATH)
    await tick(hass)
    _activity(server, 2, 60, {**BATH, "name": "Bathtime"})
    await tick(hass)

    assert _activity_entities(hass, init_integration) == {
        LAST_BATH: f"{BABY_ONE_ID}_last_activity_{BATH_ID}"
    }
    assert _state(hass, LAST_BATH).state == at(60).isoformat()

    assert await hass.config_entries.async_reload(init_integration.entry_id)
    await hass.async_block_till_done()

    assert _activity_entities(hass, init_integration) == {
        LAST_BATH: f"{BABY_ONE_ID}_last_activity_{BATH_ID}"
    }
    assert _state(hass, LAST_BATH).attributes["friendly_name"] == "Baby One Last Bathtime"


async def test_a_synced_type_record_names_the_sensor(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    """Test the type as the app syncs it on its own wins over an older copy in an activity."""
    _activity(server, 1, 30, BATH)
    renamed = {**BATH, "name": "Bubble bath", "timestamp": "2026-10-25 00:00:00 +0000"}
    server.add_payload(DEVICE_A, renamed, op=OP_ADD)
    await tick(hass)

    assert _activity_entities(hass, init_integration) == {
        "sensor.baby_one_last_bubble_bath": f"{BABY_ONE_ID}_last_activity_{BATH_ID}"
    }
    state = _state(hass, "sensor.baby_one_last_bubble_bath")
    assert state.attributes["friendly_name"] == "Baby One Last Bubble bath"


async def test_the_sensors_are_back_after_a_reload_without_duplicates(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    _activity(server, 1, 30, BATH)
    _activity(server, 2, 40, TUMMY_TIME)
    await tick(hass)
    before = _activity_entities(hass, init_integration)

    assert await hass.config_entries.async_reload(init_integration.entry_id)
    await hass.async_block_till_done()
    await tick(hass)

    assert _activity_entities(hass, init_integration) == before
    assert _state(hass, LAST_BATH).state == at(30).isoformat()
    assert _state(hass, LAST_TUMMY_TIME).state == at(40).isoformat()


async def test_a_baby_that_appears_later_gets_its_activity_sensors(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    _activity(server, 1, 30, BATH, baby=BABY_THREE, device=DEVICE_B)
    await tick(hass)

    assert _state(hass, "sensor.baby_three_last_bath").state == at(30).isoformat()
    assert f"{BABY_THREE_ID}_last_activity_{BATH_ID}" in set(
        _activity_entities(hass, init_integration).values()
    )


async def test_a_baby_added_again_after_its_delete_gets_its_activity_sensor_again(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    _activity(server, 1, 30, BATH, baby=BABY_TWO)
    await tick(hass)
    server.add_payload(DEVICE_B, BABY_TWO, op=OP_DELETE)
    await tick(hass)
    assert hass.states.get("sensor.baby_two_last_bath") is None

    server.add_payload(DEVICE_B, {**BABY_TWO, "timestamp": "2026-10-26 12:00:00 +0000"}, op=OP_ADD)
    await tick(hass)

    assert _state(hass, "sensor.baby_two_last_bath").state == at(30).isoformat()


async def test_deleting_a_types_only_activity_leaves_its_sensor_unknown(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    _activity(server, 1, 30, BATH)
    await tick(hass)
    _activity(server, 1, 30, BATH, op=OP_DELETE)
    await tick(hass)

    state = _state(hass, LAST_BATH)
    assert state.state == "unknown"
    assert state.attributes["duration_minutes"] is None
    assert state.attributes["note"] is None

    _activity(server, 2, 90, BATH)
    await tick(hass)
    assert _state(hass, LAST_BATH).state == at(90).isoformat()
    assert len(_activity_entities(hass, init_integration)) == 1


@pytest.mark.parametrize(
    "desc",
    [
        None,
        "Bath",
        ["Bath"],
        5,
        {},
        {"name": "Bath"},
        {"name": "Bath", "objectID": None},
        {"name": "Bath", "objectID": ""},
        {"name": "Bath", "objectID": "   "},
        {"name": "Bath", "objectID": 7},
    ],
    ids=repr,
)
async def test_an_activity_without_a_usable_type_id_gets_no_sensor(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, desc: Any
) -> None:
    _activity(server, 1, 30, desc)
    _activity(server, 2, 40, BATH)
    await tick(hass)

    assert _activity_entities(hass, init_integration) == {
        LAST_BATH: f"{BABY_ONE_ID}_last_activity_{BATH_ID}"
    }
    assert _state(hass, LAST_BATH).state == at(40).isoformat()


@pytest.mark.parametrize("name", [None, "", "  ", 5, ["Bath"]], ids=repr)
async def test_a_type_without_a_usable_name_is_named_by_its_id(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, name: Any
) -> None:
    _activity(server, 1, 30, {"objectID": SWIM_ID, "name": name})
    await tick(hass)

    [entity_id] = _activity_entities(hass, init_integration)
    assert _state(hass, entity_id).state == at(30).isoformat()
    assert _state(hass, entity_id).attributes["friendly_name"] == f"Baby One Last {SWIM_ID}"


@pytest.mark.parametrize("duration", ["10", None, True, 1.5, ["x"], {"a": 1}], ids=repr)
async def test_a_duration_that_is_not_whole_minutes_reads_as_none(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, duration: Any
) -> None:
    _activity(server, 1, 30, BATH, duration=duration)
    await tick(hass)

    assert _state(hass, LAST_BATH).attributes["duration_minutes"] is None


@pytest.mark.parametrize("note", [5, None, ["x"], ""], ids=repr)
async def test_a_note_that_is_not_text_reads_as_none(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, note: Any
) -> None:
    _activity(server, 1, 30, BATH, note=note)
    await tick(hass)

    assert _state(hass, LAST_BATH).attributes["note"] is None


# The action


async def test_log_activity_logs_a_built_in_type_with_minutes_and_a_note(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    await _log(hass, "tummy time", minutes=15, note="On the play mat")

    [(_, _, record)] = posted(server)
    assert isinstance(record, OtherActivity)
    assert record.activity_name == "Tummy Time"
    assert record.desc is not None
    assert record.desc["objectID"] == TUMMY_TIME_ID
    assert (record.duration, record.note, record.time) == (15, "On the play mat", NOW)
    state = _state(hass, LAST_TUMMY_TIME)
    assert state.state == NOW.isoformat()
    assert state.attributes["duration_minutes"] == 15


async def test_log_activity_logs_a_type_added_in_the_app(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    _activity(server, 1, 30, BATH)
    await tick(hass)

    await _log(hass, "Bath")

    [(_, _, record)] = posted(server)
    assert record.desc == BATH
    assert (record.duration, record.note) == (0, "Added from Home Assistant")
    assert _state(hass, LAST_BATH).state == NOW.isoformat()


async def test_log_activity_by_the_types_id(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    _activity(server, 1, 30, BATH)
    await tick(hass)

    await _log(hass, BATH_ID)

    [(_, _, record)] = posted(server)
    assert record.desc == BATH


async def test_a_type_first_used_on_a_phone_since_the_last_sync_can_be_logged(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    """Test the write's own sync finds it, so the stale state doesn't refuse it."""
    _activity(server, 1, 30, BATH)

    await _log(hass, "Bath")

    [(_, _, record)] = posted(server)
    assert record.desc == BATH


async def test_log_activity_at_a_given_time(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    await _log(hass, "Crawling", time="2026-10-26T12:00:00+00:00")

    [(_, _, record)] = posted(server)
    assert record.time is not None
    assert record.time.isoformat() == "2026-10-26T12:00:00+00:00"


async def test_log_activity_with_a_record_id_uses_it(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    await _log(hass, "Crawling", object_id="6f1c2a3e-0000-4000-8000-000000000003")

    [(_, _, record)] = posted(server)
    assert record.object_id == "6F1C2A3E-0000-4000-8000-000000000003"


@pytest.mark.parametrize("note", ["", " ", "\t\n"], ids=repr)
async def test_log_activity_with_a_blank_note_says_it_came_from_home_assistant(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, note: str
) -> None:
    await _log(hass, "Crawling", note=note)

    [(_, _, record)] = posted(server)
    assert record.note == "Added from Home Assistant"


@pytest.mark.parametrize("minutes", [0, 1, 1440, 30.0], ids=repr)
async def test_log_activity_takes_whole_minutes_up_to_a_day(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, minutes: Any
) -> None:
    await _log(hass, "Crawling", minutes=minutes)

    [(_, _, record)] = posted(server)
    assert record.duration == int(minutes)


async def test_log_activity_refuses_an_unknown_type_with_nothing_posted(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    with pytest.raises(ServiceValidationError) as raised:
        await _log(hass, "Swimming")

    assert raised.value.translation_key == "unknown_activity"
    assert raised.value.translation_placeholders == {"activity": "Swimming"}
    assert posted(server) == []


async def test_log_activity_names_the_ids_when_two_types_share_a_name(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    _activity(server, 1, 30, BATH)
    _activity(server, 2, 40, {**SWIM, "name": "Bath"})
    await tick(hass)

    with pytest.raises(ServiceValidationError) as raised:
        await _log(hass, "Bath")

    assert raised.value.translation_key == "ambiguous_selection"
    placeholders = raised.value.translation_placeholders or {}
    assert placeholders["medicine"] == "Bath"
    assert set(placeholders["ids"].split(", ")) == {BATH_ID, SWIM_ID}
    assert posted(server) == []


@pytest.mark.parametrize("name", ["", " ", "\t"], ids=repr)
async def test_a_blank_activity_name_is_a_translated_error(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, name: str
) -> None:
    with pytest.raises(ServiceValidationError) as raised:
        await _log(hass, name)

    assert raised.value.translation_key == "invalid_selection_name"
    assert posted(server) == []


@pytest.mark.parametrize("minutes", [-1, 1.5, 1441, "ten", True, None], ids=repr)
async def test_log_activity_refuses_bad_minutes(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, minutes: Any
) -> None:
    with pytest.raises(probatio.Invalid):
        await _log(hass, "Tummy Time", minutes=minutes)
    assert posted(server) == []


@pytest.mark.parametrize("name", [None, ["Bath"], {"a": 1}], ids=repr)
async def test_an_activity_that_is_not_text_is_a_schema_error(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, name: Any
) -> None:
    with pytest.raises(probatio.Invalid):
        await _log(hass, name)
    assert posted(server) == []


async def test_a_missing_activity_is_a_schema_error(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    data = {"device_id": device_id(hass, BABY_ONE_ID)}
    with pytest.raises(probatio.Invalid):
        await hass.services.async_call(DOMAIN, "log_activity", data, blocking=True)
    assert posted(server) == []


async def test_a_value_error_the_library_raises_is_a_write_error(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    with (
        patch.object(
            init_integration.runtime_data.engine.__class__,
            "add_activity",
            side_effect=ValueError("no device ID"),
        ),
        pytest.raises(HomeAssistantError) as raised,
    ):
        await _log(hass, "Crawling")

    assert not isinstance(raised.value, ServiceValidationError)
    assert raised.value.translation_key == "write_failed"
    assert posted(server) == []


async def test_a_record_id_taken_by_another_record_is_refused(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    taken = "6F1C2A3E-0000-4000-8000-0000000000AA"
    await hass.services.async_call(
        DOMAIN,
        "log_diaper",
        {"device_id": device_id(hass, BABY_ONE_ID), "type": "wet", "object_id": taken},
        blocking=True,
    )
    server.posted.clear()

    with pytest.raises(ServiceValidationError) as raised:
        await _log(hass, "Crawling", object_id=taken)

    assert raised.value.translation_key == "object_id_in_use"
    assert server.posted == []


async def test_retrying_with_the_same_record_id_adds_nothing_again(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    record_id = "6F1C2A3E-0000-4000-8000-0000000000AB"
    await _log(hass, "Crawling", object_id=record_id)
    await _log(hass, "Crawling", object_id=record_id)

    assert len(posted(server)) == 1


async def test_a_record_id_that_is_not_a_uuid_is_a_schema_error(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    with pytest.raises(probatio.Invalid):
        await _log(hass, "Crawling", object_id="not-a-uuid")
    assert posted(server) == []
