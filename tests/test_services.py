"""The actions: logging diapers, bottles, nursing, sleep, and pumps, and undo."""

import asyncio
import json
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

import probatio
import pytest
import voluptuous as vol
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import issue_registry as ir
from homeassistant.util.unit_system import METRIC_SYSTEM, US_CUSTOMARY_SYSTEM
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.nighp_baby_tracker.const import (
    DOMAIN,
)
from custom_components.nighp_baby_tracker.pybabytracker import (
    OP_ADD,
    OP_DELETE,
    OP_UPDATE,
    BabyTrackerConnectionError,
    Formula,
    Nursing,
    Pump,
    Pumped,
    Sleep,
    SyncEngine,
    Volume,
)

from . import EMAIL, OWN_DEVICE, OWN_DEVICE_TWO
from .factories import (
    BABY_ONE_ID,
    BABY_THREE_ID,
    BABY_TWO_ID,
    DEVICE_A,
    at,
)
from .fake_server import FakeServer
from .helpers import (
    ACTION_IDS,
    ALL_ACTIONS,
    ASLEEP,
    BABY_ACTIONS,
    BOTTLE_TODAY,
    FEEDS_TODAY,
    LAST_DIAPER,
    LAST_FEED,
    LAST_SLEEP,
    NOW,
    NURSE_LEFT,
    NURSING_TIMER,
    OTHER_DEVICES_RECORD,
    PHONE_SLEEP,
    SLEEP_SWITCH,
    SLEEP_TIMER,
    account_device,
    button,
    call,
    device_id,
    fail_to_write,
    gap_issue,
    log_diaper,
    log_pump,
    minutes,
    nurse,
    nursing_posts,
    only_record,
    phone_sleep,
    posted,
    press,
    sensor_states,
    state,
    state_of,
    sync,
    toggle,
    turn_on,
    undo,
)

# The live service's Pump keys: shape only.
PER_SIDE_PUMP_KEYS = {
    "BCObjectType",
    "amount",
    "finishSide",
    "leftDuration",
    "leftAmount",
    "newFlage",
    "note",
    "objectID",
    "pictureLoaded",
    "pictureNote",
    "rightAmount",
    "rightDuration",
    "sides",
    "time",
    "timestamp",
    "timezone",
}


def only_pump(server: FakeServer) -> Pump:
    [(_, _, record)] = posted(server)
    assert isinstance(record, Pump)
    return record


@pytest.fixture(autouse=True)
def _start_at_now(freezer: Any) -> None:
    """Freeze the clock at NOW before anything is set up.

    Timers scheduled at setup then share the clock the tests move, so moving it forward and
    ticking reaches them the way it does in core's tests.
    """
    freezer.move_to(NOW)


CAPTURE = json.loads((Path(__file__).parent / "fixtures" / "captured_payloads.json").read_text())


IGNORED = ("objectID", "timestamp", "note", "baby")


RETRY_ID = "7E57ED00-0000-4000-8000-000000000001"


def captured(prefix: str) -> dict[str, Any]:
    """The captured add payload for a label, minus the keys a write fills in itself."""
    [entry] = [e for e in CAPTURE if e["label"].startswith(prefix) and e["opcode"] == 0]
    return {k: v for k, v in entry["payload"].items() if k not in IGNORED}


VALID: dict[str, tuple[dict[str, Any], str, dict[str, Any]]] = {
    "log_bottle": (
        {"kind": "formula", "amount": 4, "unit": "fl. oz.", "time": "2026-10-26T16:54:35+00:00"},
        "C1 formula add",
        {},
    ),
    "log_bottle_pumped": (
        {"kind": "pumped", "amount": 3, "unit": "fl. oz.", "time": "2026-10-26T16:54:55+00:00"},
        "C2 pumped bottle add",
        {},
    ),
    "log_pump": (
        {
            "left_amount": 2,
            "right_amount": 5,
            "unit": "fl. oz.",
            "left_minutes": 1,
            "right_minutes": 1,
            "finish_side": "left",
            "time": "2026-10-26T16:55:15+00:00",
        },
        "C3 pump add",
        {},
    ),
    "log_nursing": (
        {
            "left_minutes": 1,
            "right_minutes": 1,
            "finish_side": "right",
            "end_time": "2026-10-26T16:57:52+00:00",
        },
        "C4 nursing add",
        {},  # the end time minus its two minutes is the capture's start
    ),
    "log_sleep": (
        {"start": "2026-10-26T16:57:39+00:00", "end": "2026-10-26T16:58:48+00:00"},
        "C5 sleep add",
        {"duration": 1},
    ),
}


ACTION_OF = {"log_bottle_pumped": "log_bottle"}


ACTIONS = list(VALID)


async def run_valid(hass: HomeAssistant, name: str, **extra: Any) -> None:
    data, _, _ = VALID[name]
    await call(hass, ACTION_OF.get(name, name), **{**data, **extra})


def key_of(raised: pytest.ExceptionInfo[ServiceValidationError]) -> str | None:
    return raised.value.translation_key


LIMITS = {
    "nursing left over 240": ("log_nursing", {"left_minutes": 241}, "minutes_too_long"),
    "nursing right over 240": (
        "log_nursing",
        {"left_minutes": 5, "right_minutes": 241},
        "minutes_too_long",
    ),
    "pump left over 240": (
        "log_pump",
        {"left_amount": 2, "left_minutes": 241},
        "minutes_too_long",
    ),
    "pump right over 240": (
        "log_pump",
        {"left_amount": 2, "right_minutes": 241},
        "minutes_too_long",
    ),
    "sleep over 24 hours": (
        "log_sleep",
        {"start": "2026-10-25T01:00:00+00:00", "end": "2026-10-26T01:00:01+00:00"},
        "sleep_too_long",
    ),
    "bottle rounds to zero": (
        "log_bottle",
        {"kind": "formula", "amount": 0.004, "unit": "fl. oz."},
        "amount_too_small",
    ),
    "pump rounds to zero": (
        "log_pump",
        {"left_amount": 0.004, "right_amount": 0.004},
        "amount_too_small",
    ),
}


def _issues(hass: HomeAssistant) -> list[ir.IssueEntry]:
    registry = ir.async_get(hass)
    return [issue for (domain, _), issue in registry.issues.items() if domain == DOMAIN]


def _effect(hass: HomeAssistant, server: Any) -> tuple[Any, ...]:
    """What an action visibly changes: records sent, and the baby's nursing timer."""
    timer = hass.states.get("sensor.baby_one_nursing_timer")
    assert timer is not None
    return (len(server.posted), timer.state, timer.attributes.get("side"))


async def _prepare(hass: HomeAssistant, action: str, freezer: Any) -> None:
    freezer.move_to(NOW)
    prepare = BABY_ACTIONS[action][1]
    if prepare is not None:
        await turn_on(hass, prepare)
        freezer.tick(timedelta(minutes=2))


async def _call(hass: HomeAssistant, action: str, data: dict[str, Any]) -> None:
    await hass.services.async_call(DOMAIN, action, data, blocking=True)


async def _two_diapers(hass: HomeAssistant, freezer: Any, server: Any) -> tuple[str, str]:
    """Write baby one's diaper, then baby two's. Returns their record IDs, oldest first."""
    freezer.move_to(NOW)
    await press(hass, "button.baby_one_wet_diaper")
    freezer.tick(timedelta(minutes=1))
    await press(hass, "button.baby_two_wet_diaper")
    first, second = (record.object_id for _, _, record in posted(server))
    return first, second


def _deleted(server: Any) -> list[str]:
    from custom_components.nighp_baby_tracker.pybabytracker import OP_DELETE

    return [record.object_id for _, op, record in posted(server) if op == OP_DELETE]


def _schema_fields(schema: Any) -> set[str]:
    """Return the field names of an action's schema, which may be wrapped in ``All``."""
    if isinstance(schema, probatio.All):
        return _schema_fields(schema.validators[0])
    return {str(getattr(key, "schema", key)) for key in schema.schema}


async def test_logging_a_pump_for_one_entry_leaves_the_other_untouched(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    second_entry: MockConfigEntry,
    server: FakeServer,
    second_server: FakeServer,
) -> None:
    await log_pump(
        hass,
        config_entry_id=second_entry.entry_id,
        left_amount=2,
        unit="fl. oz.",
        time="2026-10-26T08:00:00+00:00",
    )

    assert server.posted == []
    only_pump(second_server)
    assert state_of(hass, second_entry).state == "2026-10-26T08:00:00+00:00"
    assert state_of(hass, init_integration).state == "unknown"


async def test_log_pump_by_config_entry_id_writes_a_pump_with_no_baby_and_the_real_keys(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    await log_pump(
        hass,
        config_entry_id=init_integration.entry_id,
        left_amount=2,
        right_amount=5,
        unit="fl. oz.",
        left_minutes=1,
        right_minutes=1,
        finish_side="left",
    )

    record = only_pump(server)
    payload = record.to_payload()
    assert "baby" not in payload
    assert set(payload) == PER_SIDE_PUMP_KEYS
    assert record.volume == Volume(7, "oz")
    assert record.baby is None


async def test_log_pump_accepts_the_account_device(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    await log_pump(
        hass, device=account_device(hass, init_integration).id, left_amount=3, unit="fl. oz."
    )

    assert "baby" not in only_pump(server).to_payload()


async def test_log_pump_accepts_a_baby_device_and_resolves_it_to_its_account(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    await log_pump(hass, device=device_id(hass, BABY_TWO_ID), left_amount=3, unit="fl. oz.")

    record = only_pump(server)
    assert "baby" not in record.to_payload()  # the baby that was named is not recorded
    assert state_of(hass, init_integration).state != "unknown"


async def test_log_pump_through_a_second_accounts_baby_device_posts_to_that_account(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    second_entry: MockConfigEntry,
    server: FakeServer,
    second_server: FakeServer,
) -> None:
    await log_pump(hass, device=device_id(hass, BABY_THREE_ID), left_amount=3, unit="fl. oz.")

    assert server.posted == []
    only_pump(second_server)


async def test_log_pump_through_a_second_accounts_device_posts_to_that_account(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    second_entry: MockConfigEntry,
    server: FakeServer,
    second_server: FakeServer,
) -> None:
    await log_pump(
        hass, device=account_device(hass, second_entry).id, left_amount=3, unit="fl. oz."
    )

    assert server.posted == []
    only_pump(second_server)


async def test_log_pump_needs_an_account_or_a_device(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    with pytest.raises(vol.Invalid, match="config_entry_id"):
        await log_pump(hass, left_amount=3)
    assert server.posted == []


async def test_log_pump_takes_one_of_config_entry_id_and_device_not_both(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    with pytest.raises(vol.Invalid, match="at most one"):
        await log_pump(
            hass,
            config_entry_id=init_integration.entry_id,
            device=account_device(hass, init_integration).id,
            left_amount=3,
        )
    assert server.posted == []


async def test_log_pump_still_accepts_baby_as_an_alias_of_device(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    await log_pump(hass, baby=device_id(hass, BABY_ONE_ID), left_amount=3, unit="fl. oz.")

    assert "baby" not in only_pump(server).to_payload()


async def test_the_baby_alias_resolves_a_second_accounts_baby_to_that_account(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    second_entry: MockConfigEntry,
    server: FakeServer,
    second_server: FakeServer,
) -> None:
    await log_pump(hass, baby=device_id(hass, BABY_THREE_ID), left_amount=3, unit="fl. oz.")

    assert server.posted == []
    only_pump(second_server)


async def test_the_baby_alias_for_an_unknown_device_is_a_validation_error(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    with pytest.raises(ServiceValidationError) as raised:
        await log_pump(hass, baby="nope", left_amount=3)
    assert raised.value.translation_key == "baby_not_found"


@pytest.mark.parametrize("other", ["device", "config_entry_id"])
async def test_baby_with_device_or_an_account_is_refused_as_two_targets(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, other: str
) -> None:
    value = (
        account_device(hass, init_integration).id
        if other == "device"
        else init_integration.entry_id
    )
    with pytest.raises(vol.Invalid, match="at most one"):
        await log_pump(hass, baby=device_id(hass, BABY_ONE_ID), left_amount=3, **{other: value})
    assert server.posted == []


async def test_log_pump_for_an_unknown_account_is_a_validation_error(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    with pytest.raises(ServiceValidationError) as raised:
        await log_pump(hass, config_entry_id="no-such-entry", left_amount=3)
    assert raised.value.translation_key == "service_config_entry_not_found"
    assert server.posted == []


async def test_log_pump_for_an_account_that_is_not_loaded_is_a_validation_error(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    device = account_device(hass, init_integration)
    await hass.config_entries.async_unload(init_integration.entry_id)
    assert init_integration.state is ConfigEntryState.NOT_LOADED

    with pytest.raises(ServiceValidationError) as raised:
        await log_pump(hass, device=device.id, left_amount=3)
    assert raised.value.translation_key == "service_config_entry_not_loaded"
    with pytest.raises(ServiceValidationError) as raised:
        await log_pump(hass, config_entry_id=init_integration.entry_id, left_amount=3)
    assert raised.value.translation_key == "service_config_entry_not_loaded"


@pytest.mark.parametrize("device", ["nope", "00000000000000000000000000000000"])
async def test_log_pump_for_a_device_that_is_not_ours_is_a_validation_error(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, device: str
) -> None:
    with pytest.raises(ServiceValidationError) as raised:
        await log_pump(hass, device=device, left_amount=3)
    assert raised.value.translation_key == "baby_not_found"
    assert server.posted == []


async def test_log_pump_for_another_integrations_device_is_a_validation_error(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    other = MockConfigEntry(domain="other_integration")
    other.add_to_hass(hass)
    foreign = dr.async_get(hass).async_get_or_create(
        config_entry_id=other.entry_id, identifiers={("other_integration", "thing")}
    )

    with pytest.raises(ServiceValidationError) as raised:
        await log_pump(hass, device=foreign.id, left_amount=3)
    assert raised.value.translation_key == "baby_not_found"
    assert server.posted == []


async def test_log_pump_still_requires_an_amount_and_a_unit_it_knows(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    with pytest.raises(ServiceValidationError) as raised:
        await log_pump(hass, config_entry_id=init_integration.entry_id, left_minutes=5)
    assert raised.value.translation_key == "no_amount"
    with pytest.raises(probatio.MultipleInvalid):
        await log_pump(hass, config_entry_id=init_integration.entry_id, left_amount=60, unit="cups")
    assert server.posted == []


async def test_a_baby_service_given_the_account_device_is_a_validation_error(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    with pytest.raises(ServiceValidationError) as raised:
        await hass.services.async_call(
            DOMAIN,
            "log_diaper",
            {"device_id": account_device(hass, init_integration).id, "type": "wet"},
            blocking=True,
        )
    assert raised.value.translation_key == "baby_not_found"
    assert server.posted == []


async def test_undo_removes_a_pump_by_account(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    await log_pump(hass, config_entry_id=init_integration.entry_id, left_amount=3, unit="fl. oz.")

    await undo(hass, config_entry_id=init_integration.entry_id)

    assert [op for _, op, _ in posted(server)] == [0, 2]
    assert state_of(hass, init_integration).state == "unknown"


async def test_undo_for_a_baby_never_picks_a_pump(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    await log_pump(hass, config_entry_id=init_integration.entry_id, left_amount=3, unit="fl. oz.")
    pump_id = posted(server)[0][2].object_id

    with pytest.raises(ServiceValidationError) as raised:
        await undo(hass, device_id=device_id(hass, BABY_ONE_ID))
    assert raised.value.translation_key == "nothing_to_undo"
    with pytest.raises(ServiceValidationError) as raised:
        await undo(hass, device_id=device_id(hass, BABY_ONE_ID), object_id=pump_id)
    assert raised.value.translation_key == "record_not_for_baby"
    assert len(posted(server)) == 1


async def test_undo_of_a_sleep_home_assistant_started_deletes_it(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, freezer: Any
) -> None:
    freezer.move_to(NOW)
    await toggle(hass, SLEEP_SWITCH)

    await undo(hass, device_id=device_id(hass, BABY_ONE_ID))

    [(_, op1, started), (_, op2, deleted)] = posted(server)
    assert (op1, op2) == (OP_ADD, OP_DELETE)
    assert deleted.object_id == started.object_id
    assert state(hass, ASLEEP).state == "off"
    assert state(hass, SLEEP_TIMER).state == "unknown"


async def test_start_sleep_with_a_past_start_and_a_note(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, freezer: Any
) -> None:
    freezer.move_to(NOW)

    await call(hass, "start_sleep", start=minutes(-10).isoformat(), note="Crib")

    [(_, op, record)] = posted(server)
    assert op == OP_ADD
    assert record.duration == 0
    assert record.time == minutes(-10)
    assert record.note == "Crib"
    assert state(hass, SLEEP_TIMER).state == minutes(-10).isoformat()


async def test_start_sleep_with_an_object_id_retries_safely(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, freezer: Any
) -> None:
    freezer.move_to(NOW)
    object_id = "5EE90000-0000-4000-8000-0000000000AA"

    await call(hass, "start_sleep", object_id=object_id.lower())
    await call(hass, "start_sleep", object_id=object_id)

    [(_, _, record)] = posted(server)
    assert record.object_id == object_id


async def test_start_sleep_while_a_sleep_is_running_is_refused(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, freezer: Any
) -> None:
    freezer.move_to(NOW)
    phone_sleep(server, start=minutes(-40))
    await sync(hass, init_integration)

    with pytest.raises(ServiceValidationError) as err:
        await call(hass, "start_sleep")

    assert err.value.translation_key == "sleep_already_running"
    assert posted(server) == []


async def test_start_sleep_more_than_24_hours_ago_is_refused(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, freezer: Any
) -> None:
    freezer.move_to(NOW)

    with pytest.raises(ServiceValidationError) as err:
        await call(hass, "start_sleep", start=minutes(-60 * 25).isoformat())

    assert err.value.translation_key == "sleep_start_out_of_range"
    assert posted(server) == []


async def test_stop_sleep_ends_a_phone_started_sleep_at_the_given_end(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, freezer: Any
) -> None:
    freezer.move_to(NOW)
    phone_sleep(server, start=minutes(-40))
    await sync(hass, init_integration)

    await call(hass, "stop_sleep", end=minutes(-5).isoformat())

    [(_, op, record)] = posted(server)
    assert (op, record.object_id, record.duration) == (OP_UPDATE, PHONE_SLEEP, 35)
    assert state(hass, ASLEEP).state == "off"


async def test_stop_sleep_defaults_to_now(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, freezer: Any
) -> None:
    freezer.move_to(NOW)
    await call(hass, "start_sleep")
    freezer.move_to(minutes(61))

    await call(hass, "stop_sleep")

    [_, (_, op, record)] = posted(server)
    assert (op, record.duration) == (OP_UPDATE, 61)


async def test_stop_sleep_with_no_running_sleep_is_a_validation_error(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, freezer: Any
) -> None:
    freezer.move_to(NOW)
    phone_sleep(server, start=minutes(-90), duration=30)  # finished
    await sync(hass, init_integration)

    with pytest.raises(ServiceValidationError) as err:
        await call(hass, "stop_sleep")

    assert err.value.translation_key == "no_running_sleep"
    assert posted(server) == []


async def test_stop_sleep_with_an_end_before_the_start_is_refused(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, freezer: Any
) -> None:
    freezer.move_to(NOW)
    phone_sleep(server, start=minutes(-40))
    await sync(hass, init_integration)

    with pytest.raises(ServiceValidationError) as err:
        await call(hass, "stop_sleep", end=minutes(-50).isoformat())

    assert err.value.translation_key == "end_before_start"
    assert posted(server) == []


async def test_stop_sleep_past_24_hours_writes_24_hours(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, freezer: Any
) -> None:
    freezer.move_to(NOW)
    phone_sleep(server, start=minutes(-60 * 23))
    await sync(hass, init_integration)
    freezer.move_to(minutes(60 * 3))

    await call(hass, "stop_sleep")

    [(_, _, record)] = posted(server)
    assert record.duration == 1440


async def test_undo_of_a_nursing_record_home_assistant_wrote_deletes_it(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, freezer: Any
) -> None:
    await nurse(hass, freezer, (0, "left"), (5, "left"))

    await undo(hass, device_id=device_id(hass, BABY_ONE_ID))

    [(_, op1, added), (_, op2, deleted)] = posted(server)
    assert (op1, op2) == (OP_ADD, OP_DELETE)
    assert deleted.object_id == added.object_id
    assert state(hass, "sensor.baby_one_last_feed").state == "unknown"  # the nursing is gone
    assert state(hass, NURSING_TIMER).state == "unknown"


async def test_start_switch_and_stop_nursing_actions(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, freezer: Any
) -> None:
    freezer.move_to(NOW)
    await call(hass, "start_nursing", side="right")
    freezer.move_to(minutes(3))
    await call(hass, "switch_nursing_side")
    assert state(hass, NURSING_TIMER).attributes["side"] == "left"
    freezer.move_to(minutes(10))

    await call(hass, "stop_nursing", note="Sleepy")

    [record] = nursing_posts(server)
    assert (record.left_duration, record.right_duration, record.finish_side) == (7, 3, 0)
    assert record.note == "Sleepy"
    assert record.time == NOW


async def test_start_nursing_on_the_running_side_changes_nothing_and_on_the_other_switches(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, freezer: Any
) -> None:
    freezer.move_to(NOW)
    await call(hass, "start_nursing", side="left")
    freezer.move_to(minutes(2))
    await call(hass, "start_nursing", side="left")
    assert state(hass, NURSING_TIMER).attributes["side_started"] == NOW.isoformat()
    freezer.move_to(minutes(4))
    await call(hass, "start_nursing", side="right")
    freezer.move_to(minutes(5))

    await call(hass, "stop_nursing")

    [record] = nursing_posts(server)
    assert (record.left_duration, record.right_duration) == (4, 1)


@pytest.mark.parametrize("action", ["switch_nursing_side", "stop_nursing"])
async def test_switch_or_stop_with_no_nursing_timer_is_a_validation_error(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, action: str
) -> None:
    with pytest.raises(ServiceValidationError) as err:
        await call(hass, action)

    assert err.value.translation_key == "no_nursing_timer"
    assert posted(server) == []


async def test_start_nursing_needs_a_side(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    import voluptuous as vol

    with pytest.raises(vol.Invalid):
        await call(hass, "start_nursing")
    with pytest.raises(vol.Invalid):
        await call(hass, "start_nursing", side="both")


async def test_a_switch_during_a_stops_write_waits_and_is_not_erased(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, freezer: Any
) -> None:
    await nurse(hass, freezer, (0, "left"))
    gate = asyncio.Event()
    server.post_gate = gate
    freezer.move_to(minutes(6))
    stop = hass.async_create_task(toggle(hass, NURSE_LEFT))
    for _ in range(20):
        await asyncio.sleep(0)
    assert "post" in server.calls  # the stop's post is waiting at the gate
    switch = hass.async_create_task(call(hass, "switch_nursing_side"))
    for _ in range(5):
        await asyncio.sleep(0)
    gate.set()
    await stop

    with pytest.raises(ServiceValidationError) as err:
        await switch

    assert err.value.translation_key == "no_nursing_timer"
    [record] = nursing_posts(server)
    assert (record.left_duration, record.right_duration) == (6, 0)
    assert state(hass, NURSING_TIMER).state == "unknown"


async def test_log_diaper_with_a_past_time_and_a_note(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    await log_diaper(
        hass, BABY_ONE_ID, "dirty", time="2026-10-26T10:30:00+00:00", note="Rash cream applied"
    )

    [(_, op, record)] = posted(server)
    assert op == OP_ADD
    assert record.status == 1
    assert record.time == datetime(2026, 10, 26, 10, 30, tzinfo=UTC)
    assert record.note == "Rash cream applied"
    assert record.baby.object_id == BABY_ONE_ID


async def test_log_diaper_reads_a_time_without_an_offset_in_the_home_assistant_time_zone(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    await hass.config.async_set_time_zone("America/New_York")

    await log_diaper(hass, BABY_ONE_ID, "wet", time="2026-10-26 10:30:00")

    [(_, _, record)] = posted(server)
    assert record.time == datetime(2026, 10, 26, 14, 30, tzinfo=UTC)
    assert record.timezone == -18000


async def test_log_diaper_without_a_time_uses_now_in_the_home_assistant_time_zone(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, freezer: Any
) -> None:
    await hass.config.async_set_time_zone("Asia/Kolkata")
    freezer.move_to(NOW)

    await log_diaper(hass, BABY_ONE_ID, "mixed")

    [(_, _, record)] = posted(server)
    assert record.time == NOW
    assert record.timezone == 19800
    assert record.status == 2


async def test_log_diaper_for_an_unknown_baby_device_is_a_validation_error(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    with pytest.raises(ServiceValidationError) as raised:
        await hass.services.async_call(
            DOMAIN, "log_diaper", {"device_id": "0" * 32, "type": "wet"}, blocking=True
        )
    assert raised.value.translation_domain == DOMAIN
    assert raised.value.translation_key == "baby_not_found"
    assert server.posted == []


async def test_log_diaper_for_another_integrations_device_is_a_validation_error(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    other = MockConfigEntry(domain="other_integration")
    other.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=other.entry_id, identifiers={("other_integration", "lamp")}
    )
    with pytest.raises(ServiceValidationError) as raised:
        await hass.services.async_call(
            DOMAIN, "log_diaper", {"device_id": device.id, "type": "wet"}, blocking=True
        )
    assert raised.value.translation_key == "baby_not_found"
    assert server.posted == []


async def test_a_log_diaper_retry_with_the_same_object_id_does_not_post_twice(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    retry_id = "7E57ED00-0000-4000-8000-000000000001"
    await log_diaper(hass, BABY_ONE_ID, "wet", object_id=retry_id)
    # The caller never saw the answer, so it sends the same logical write again (any case).
    await log_diaper(hass, BABY_ONE_ID, "wet", object_id=retry_id.lower())

    [(_, _, record)] = posted(server)
    assert record.object_id == retry_id
    assert [n for n, _, _ in posted(server)] == [1]


@pytest.mark.parametrize("bad", ["retry-1", "", "1234", "7E57ED00-0000-4000-8000-00000000000Z"])
async def test_log_diaper_rejects_an_object_id_that_is_not_a_uuid(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, bad: str
) -> None:
    with pytest.raises(probatio.Invalid):
        await log_diaper(hass, BABY_ONE_ID, "wet", object_id=bad)
    assert server.posted == []


async def test_log_diaper_accepts_a_lowercase_uuid_and_posts_it_in_uppercase(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    await log_diaper(hass, BABY_ONE_ID, "wet", object_id="7e57ed00-0000-4000-8000-00000000000a")

    [(_, _, record)] = posted(server)
    assert record.object_id == "7E57ED00-0000-4000-8000-0000000000" + "0A"


async def test_other_value_errors_are_not_reported_as_an_id_in_use(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    with (
        patch.object(SyncEngine, "add_diaper", side_effect=ValueError("some other refusal")),
        pytest.raises(ValueError, match="some other refusal"),
    ):
        await log_diaper(hass, BABY_ONE_ID, "wet")


async def test_a_plain_value_error_that_mentions_belongs_to_is_not_reported_as_an_id_in_use(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    lookalike = "object_id 7E57ED00-0000-4000-8000-00000000000A belongs to another record"
    with (
        patch.object(SyncEngine, "add_diaper", side_effect=ValueError(lookalike)),
        pytest.raises(ValueError, match="belongs to another record"),
    ):
        await log_diaper(hass, BABY_ONE_ID, "wet", object_id="7e57ed00-0000-4000-8000-00000000000a")


async def test_a_log_diaper_object_id_belonging_to_another_record_is_an_error(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    with pytest.raises(ServiceValidationError) as raised:
        await log_diaper(hass, BABY_ONE_ID, "wet", object_id=OTHER_DEVICES_RECORD)
    assert raised.value.translation_key == "object_id_in_use"
    assert server.posted == []


async def test_log_diaper_rejects_an_unknown_type(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    with pytest.raises(probatio.Invalid):
        await log_diaper(hass, BABY_ONE_ID, "purple")
    assert server.posted == []


async def test_log_diaper_for_a_baby_whose_entry_is_not_loaded_is_a_validation_error(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    baby_device = device_id(hass, BABY_ONE_ID)
    await hass.config_entries.async_unload(init_integration.entry_id)
    with pytest.raises(ServiceValidationError) as raised:
        await hass.services.async_call(
            DOMAIN, "log_diaper", {"device_id": baby_device, "type": "wet"}, blocking=True
        )
    assert raised.value.translation_key == "service_config_entry_not_loaded"


async def test_the_actions_exist_even_before_any_entry_is_set_up(hass: HomeAssistant) -> None:
    from homeassistant.setup import async_setup_component

    assert await async_setup_component(hass, DOMAIN, {})
    assert hass.services.has_service(DOMAIN, "log_diaper")
    assert hass.services.has_service(DOMAIN, "undo")


async def test_a_successful_write_updates_the_sensor_without_waiting_for_the_next_sync(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    assert hass.states.get(LAST_DIAPER).state == at(0).isoformat()

    await log_diaper(hass, BABY_ONE_ID, "dirty", time="2026-10-26T09:15:00+00:00", note="Big one")
    # No clock tick and no refresh: the new record is already on the sensor.
    state = hass.states.get(LAST_DIAPER)
    assert state.state == "2026-10-26T09:15:00+00:00"
    assert state.attributes["type"] == "dirty"
    assert state.attributes["note"] == "Big one"


async def test_a_write_syncs_first_so_it_sees_records_other_devices_just_added(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    # A phone logs a diaper; HA hasn't polled yet. The write's own sync picks it up.
    server.add_record(
        DEVICE_A, "Diaper", "D0000000-0000-4000-8000-000000000003", when=at(30), status=1
    )
    server.calls.clear()

    await log_diaper(hass, BABY_ONE_ID, "wet", time="2026-10-24T00:00:00+00:00")

    assert server.calls[0] == "devices"
    assert server.transaction_reads  # the phone's new entry was read
    assert hass.states.get(LAST_DIAPER).state == at(30).isoformat()  # the newest by time


async def test_a_failed_log_diaper_action_raises_the_same_error(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    server.post_errors.append((BabyTrackerConnectionError("HTTP 500"), False))
    with pytest.raises(HomeAssistantError) as raised:
        await log_diaper(hass, BABY_ONE_ID, "wet")
    assert raised.value.translation_key == "write_failed"


async def test_a_gap_in_this_devices_own_log_raises_but_creates_no_repair_at_first(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    # The service says this device's log is at 5, but nothing can be read back.
    server.last_sync_override[OWN_DEVICE] = 5

    await fail_to_write(hass)

    assert server.posted == []
    assert gap_issue(hass, init_integration) is None


async def test_a_gap_that_keeps_failing_for_an_hour_creates_a_repair_issue(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, freezer: Any
) -> None:
    freezer.move_to(NOW)
    server.last_sync_override[OWN_DEVICE] = 5
    await fail_to_write(hass)

    freezer.tick(timedelta(hours=1, minutes=1))
    await fail_to_write(hass)

    issue = gap_issue(hass, init_integration)
    assert issue is not None
    assert issue.translation_key == "own_log_gap"
    assert issue.severity is ir.IssueSeverity.ERROR
    assert issue.is_fixable is False
    assert issue.translation_placeholders == {"account": EMAIL}


async def test_a_gap_still_failing_just_under_an_hour_later_creates_no_repair(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, freezer: Any
) -> None:
    freezer.move_to(NOW)
    server.last_sync_override[OWN_DEVICE] = 5
    await fail_to_write(hass)

    freezer.tick(timedelta(minutes=59))
    await fail_to_write(hass)

    assert gap_issue(hass, init_integration) is None


async def test_many_quick_failures_do_not_add_up_to_a_repair(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, freezer: Any
) -> None:
    freezer.move_to(NOW)
    server.last_sync_override[OWN_DEVICE] = 5

    for _ in range(10):
        await fail_to_write(hass)
        freezer.tick(timedelta(minutes=5))

    assert gap_issue(hass, init_integration) is None  # 45 minutes in


async def test_undo_deletes_the_record_a_button_press_created(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, freezer: Any
) -> None:
    freezer.move_to(NOW)
    await press(hass, button("baby_one", "dirty"))
    [(_, _, created)] = posted(server)
    assert hass.states.get(LAST_DIAPER).state == NOW.isoformat()

    await undo(hass, device_id=device_id(hass, BABY_ONE_ID))

    deleted = posted(server)[1]
    assert deleted[1] == OP_DELETE
    assert deleted[0] == 2
    assert deleted[2].object_id == created.object_id
    # The sensor falls back to the phone's earlier diaper, again without waiting for a sync.
    assert hass.states.get(LAST_DIAPER).state == at(0).isoformat()


async def test_undo_by_config_entry_id(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    await press(hass, button("baby_one", "wet"))

    await undo(hass, config_entry_id=init_integration.entry_id)

    assert [op for _, op, _ in posted(server)] == [OP_ADD, OP_DELETE]


async def test_undo_without_an_object_id_removes_the_most_recent_record_ha_created(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, freezer: Any
) -> None:
    freezer.move_to(NOW)
    await press(hass, button("baby_one", "wet"))
    freezer.move_to(datetime(2026, 10, 26, 15, 0, 30, tzinfo=UTC))
    await press(hass, button("baby_one", "dirty"))
    newest = posted(server)[1][2].object_id

    await undo(hass, device_id=device_id(hass, BABY_ONE_ID))

    assert posted(server)[2][2].object_id == newest


async def test_undo_with_a_baby_only_considers_that_babys_records(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, freezer: Any
) -> None:
    freezer.move_to(NOW)
    await press(hass, button("baby_one", "wet"))
    first = posted(server)[0][2].object_id
    freezer.move_to(datetime(2026, 10, 26, 15, 0, 30, tzinfo=UTC))
    await press(hass, button("baby_two", "wet"))

    await undo(hass, device_id=device_id(hass, BABY_ONE_ID))
    assert posted(server)[2][2].object_id == first

    await undo(hass, config_entry_id=init_integration.entry_id)  # any baby: baby two's is left
    assert posted(server)[3][2].baby.object_id == BABY_TWO_ID


async def test_undo_removes_the_last_write_even_when_all_presses_share_a_second(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, freezer: Any
) -> None:
    freezer.move_to(NOW)  # the clock never moves: every record has the same timestamp
    for _ in range(6):
        await press(hass, button("baby_one", "wet"))
    ids = [record.object_id for _, _, record in posted(server)]

    await undo(hass, device_id=device_id(hass, BABY_ONE_ID))
    await undo(hass, config_entry_id=init_integration.entry_id)

    deleted = [record.object_id for _, op, record in posted(server) if op == OP_DELETE]
    assert deleted == [ids[5], ids[4]]


@pytest.mark.parametrize("scope", ["baby", "account"])
async def test_undo_deletes_the_newest_ha_write_even_after_a_phone_edited_it(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    server: FakeServer,
    freezer: Any,
    scope: str,
) -> None:
    freezer.move_to(NOW)
    await press(hass, button("baby_one", "wet"))  # A
    freezer.move_to(datetime(2026, 10, 26, 15, 5, 0, tzinfo=UTC))
    await press(hass, button("baby_one", "wet"))  # B
    first, second = (record.object_id for _, _, record in posted(server))
    # A phone edits B (wet to dirty), so B's current version came from the phone.
    server.add_record(
        DEVICE_A,
        "Diaper",
        second,
        when=datetime(2026, 10, 26, 15, 5, 0, tzinfo=UTC),
        changed=datetime(2026, 10, 26, 15, 6, 0, tzinfo=UTC),
        op=OP_UPDATE,
        status=1,
    )

    target = (
        {"device_id": device_id(hass, BABY_ONE_ID)}
        if scope == "baby"
        else {"config_entry_id": init_integration.entry_id}
    )
    await undo(hass, **target)

    deleted = [record.object_id for _, op, record in posted(server) if op == OP_DELETE]
    assert deleted == [second]
    assert first != second


async def test_undo_follows_write_order_not_the_time_of_the_diaper(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, freezer: Any
) -> None:
    freezer.move_to(NOW)
    await press(hass, button("baby_one", "wet"))
    # Written last, but its diaper happened days ago.
    await log_diaper(hass, BABY_ONE_ID, "dirty", time="2026-10-22T08:00:00+00:00")
    written_last = posted(server)[1][2].object_id

    await undo(hass, device_id=device_id(hass, BABY_ONE_ID))

    assert posted(server)[2][2].object_id == written_last


async def test_undo_accepts_an_object_id_in_any_case(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    await press(hass, button("baby_one", "wet"))
    created = posted(server)[0][2].object_id

    await undo(hass, device_id=device_id(hass, BABY_ONE_ID), object_id=created.lower())

    assert posted(server)[1][1] == OP_DELETE
    assert posted(server)[1][2].object_id == created


async def test_undo_with_a_baby_and_another_babys_record_id_is_a_validation_error(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    await press(hass, button("baby_two", "wet"))
    babys_two = posted(server)[0][2].object_id

    with pytest.raises(ServiceValidationError) as raised:
        await undo(hass, device_id=device_id(hass, BABY_ONE_ID), object_id=babys_two)

    assert raised.value.translation_key == "record_not_for_baby"
    assert len(posted(server)) == 1  # nothing deleted


async def test_a_baby_device_of_an_unloaded_entry_is_not_loaded_even_when_another_entry_is(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    second_entry: MockConfigEntry,
    server: FakeServer,
    second_server: FakeServer,
) -> None:
    # A device belongs to exactly one config entry (Home Assistant 2026.9 and later), so it is
    # never routed to the other, loaded account.
    baby_device = device_id(hass, BABY_ONE_ID)
    await hass.config_entries.async_unload(init_integration.entry_id)

    with pytest.raises(ServiceValidationError) as raised:
        await hass.services.async_call(
            DOMAIN, "log_diaper", {"device_id": baby_device, "type": "wet"}, blocking=True
        )
    assert raised.value.translation_key == "service_config_entry_not_loaded"


async def test_undo_with_an_object_id_removes_that_record(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, freezer: Any
) -> None:
    freezer.move_to(NOW)
    await press(hass, button("baby_one", "wet"))
    first = posted(server)[0][2].object_id
    freezer.move_to(datetime(2026, 10, 26, 15, 0, 30, tzinfo=UTC))
    await press(hass, button("baby_one", "dirty"))

    await undo(hass, device_id=device_id(hass, BABY_ONE_ID), object_id=first)

    assert posted(server)[2][2].object_id == first


async def test_undo_with_nothing_to_undo_is_a_validation_error(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    with pytest.raises(ServiceValidationError) as raised:
        await undo(hass, device_id=device_id(hass, BABY_ONE_ID))

    assert raised.value.translation_domain == DOMAIN
    assert raised.value.translation_key == "nothing_to_undo"
    assert server.posted == []


async def test_undo_of_a_record_another_device_created_is_refused(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    before = sensor_states(hass)

    with pytest.raises(ServiceValidationError) as raised:
        await undo(hass, device_id=device_id(hass, BABY_ONE_ID), object_id=OTHER_DEVICES_RECORD)

    assert raised.value.translation_key == "not_own_record"
    assert server.posted == []
    assert sensor_states(hass) == before


async def test_undo_never_picks_another_devices_record_when_ha_created_none(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    # The phone's diaper is the newest record, but it isn't HA's to delete.
    with pytest.raises(ServiceValidationError) as raised:
        await undo(hass, config_entry_id=init_integration.entry_id)
    assert raised.value.translation_key == "nothing_to_undo"


async def test_undo_twice_deletes_once_and_then_refuses(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    await press(hass, button("baby_one", "wet"))
    created = posted(server)[0][2].object_id
    await undo(hass, device_id=device_id(hass, BABY_ONE_ID))

    with pytest.raises(ServiceValidationError) as no_id:
        await undo(hass, device_id=device_id(hass, BABY_ONE_ID))
    with pytest.raises(ServiceValidationError) as by_id:
        await undo(hass, device_id=device_id(hass, BABY_ONE_ID), object_id=created)

    assert no_id.value.translation_key == "nothing_to_undo"
    assert by_id.value.translation_key == "record_not_found"
    assert [op for _, op, _ in posted(server)] == [OP_ADD, OP_DELETE]


async def test_undo_needs_a_baby_or_a_config_entry(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    with pytest.raises(probatio.Invalid):
        await undo(hass)


async def test_undo_with_an_unknown_config_entry_is_a_validation_error(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    with pytest.raises(ServiceValidationError) as raised:
        await undo(hass, config_entry_id="no-such-entry")
    assert raised.value.translation_key == "service_config_entry_not_found"


async def test_a_failed_undo_raises_a_translated_error_and_keeps_the_record(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    await press(hass, button("baby_one", "wet"))
    before = sensor_states(hass)
    server.post_errors.append((BabyTrackerConnectionError("HTTP 500"), False))

    with pytest.raises(HomeAssistantError) as raised:
        await undo(hass, device_id=device_id(hass, BABY_ONE_ID))

    assert raised.value.translation_key == "write_failed"
    assert sensor_states(hass) == before


async def test_both_accounts_work_with_two_config_entries(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    second_entry: MockConfigEntry,
    server: FakeServer,
    second_server: FakeServer,
) -> None:
    assert hass.services.has_service(DOMAIN, "log_diaper")
    assert hass.services.has_service(DOMAIN, "undo")

    await log_diaper(hass, BABY_ONE_ID, "wet", note="first account")
    await log_diaper(hass, BABY_THREE_ID, "mixed", note="second account")

    [(_, _, one)] = posted(server)
    [(_, _, three)] = posted(second_server)
    assert (one.baby.object_id, one.note) == (BABY_ONE_ID, "first account")
    assert (three.baby.object_id, three.note) == (BABY_THREE_ID, "second account")
    assert OWN_DEVICE in server.logs and OWN_DEVICE_TWO not in server.logs
    assert OWN_DEVICE_TWO in second_server.logs and OWN_DEVICE not in second_server.logs


async def test_undo_with_two_config_entries_only_touches_the_named_one(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    second_entry: MockConfigEntry,
    server: FakeServer,
    second_server: FakeServer,
) -> None:
    await log_diaper(hass, BABY_ONE_ID, "wet")
    await log_diaper(hass, BABY_THREE_ID, "wet")

    await undo(hass, config_entry_id=second_entry.entry_id)

    assert [op for _, op, _ in posted(server)] == [OP_ADD]
    assert [op for _, op, _ in posted(second_server)] == [OP_ADD, OP_DELETE]
    await undo(hass, device_id=device_id(hass, BABY_ONE_ID))
    assert [op for _, op, _ in posted(server)] == [OP_ADD, OP_DELETE]


@pytest.mark.parametrize("name", ACTIONS)
async def test_each_action_posts_a_record_matching_the_captured_format(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, name: str
) -> None:
    await hass.config.async_set_time_zone("America/New_York")
    _, label, tweaks = VALID[name]

    await run_valid(hass, name, note="a note")

    record = only_record(server)
    expected = {**captured(label), **tweaks}
    payload = record.to_payload()
    assert {k: v for k, v in payload.items() if k not in IGNORED} == expected
    assert payload["note"] == "a note"
    if name == "log_pump":
        assert "baby" not in payload  # a pump belongs to the account, not a baby
    else:
        assert payload["baby"]["objectID"] == BABY_ONE_ID
    assert payload["newFlage"] == "true"


@pytest.mark.parametrize("name", ACTIONS)
async def test_each_action_posts_an_add_under_the_next_number_with_a_new_uppercase_id(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, name: str
) -> None:
    await run_valid(hass, name)
    await run_valid(hass, name)

    [(n1, op1, first), (n2, op2, second)] = posted(server)
    assert (n1, op1, n2, op2) == (1, 0, 2, 0)
    assert first.object_id != second.object_id
    assert first.object_id == first.object_id.upper()
    assert len(first.object_id) == 36


@pytest.mark.parametrize("name", [n for n in ACTIONS if n != "log_pump"])
async def test_each_action_goes_to_the_baby_named(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, name: str
) -> None:
    data, _, _ = VALID[name]
    await call(hass, ACTION_OF.get(name, name), BABY_TWO_ID, **data)

    assert only_record(server).baby.object_id == BABY_TWO_ID


async def test_log_bottle_writes_formula_or_pumped_by_kind(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    await call(hass, "log_bottle", kind="formula", amount=4, unit="fl. oz.")
    await call(hass, "log_bottle", kind="pumped", amount=3, unit="fl. oz.")

    [(_, _, first), (_, _, second)] = posted(server)
    assert isinstance(first, Formula)
    assert isinstance(second, Pumped)
    assert first.volume == Volume(4, "oz")
    assert second.volume == Volume(3, "oz")


@pytest.mark.parametrize(
    ("action", "data", "key"),
    [
        ("log_bottle", {"kind": "formula", "amount": 120}, "amount"),
        ("log_bottle", {"kind": "pumped", "amount": 90}, "amount"),
        ("log_pump", {"left_amount": 60, "right_amount": 75}, "leftAmount"),
        ("log_pump", {"left_amount": 60}, "leftAmount"),
    ],
    ids=["formula", "pumped", "pump", "pump-one-side"],
)
async def test_a_milliliter_write_is_accepted_and_sent_as_metric(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    server: FakeServer,
    action: str,
    data: dict[str, Any],
    key: str,
) -> None:
    await call(hass, action, unit="mL", **data)

    payload = only_record(server).to_payload()
    first = data.get("amount", data.get("left_amount"))
    assert payload[key] == {
        "BCObjectType": "VolumeMeasure",
        "englishMeasure": "false",
        "value": first,
    }


async def test_log_bottle_in_milliliters_writes_the_value_as_given(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    await call(hass, "log_bottle", kind="formula", amount=118.5, unit="mL")

    record = only_record(server)
    assert record.volume == Volume(118.5, "ml")
    assert record.to_payload()["amount"]["englishMeasure"] == "false"
    assert record.to_payload()["amount"]["value"] == 118.5


async def test_log_bottle_in_ounces_writes_english_measure_true(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    await call(hass, "log_bottle", kind="formula", amount=2.5, unit="fl. oz.")

    assert only_record(server).to_payload()["amount"] == {
        "BCObjectType": "VolumeMeasure",
        "englishMeasure": "true",
        "value": 2.5,
    }


@pytest.mark.parametrize(
    ("units", "unit", "expected"),
    [
        (US_CUSTOMARY_SYSTEM, "fl. oz.", Volume(5, "oz")),
        (METRIC_SYSTEM, "fl. oz.", Volume(5, "oz")),
    ],
    ids=["us-customary", "metric"],
)
async def test_log_bottle_without_a_unit_uses_fluid_ounces_in_any_unit_system(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    server: FakeServer,
    units: Any,
    unit: str,
    expected: Volume,
) -> None:
    hass.config.units = units

    await call(hass, "log_bottle", kind="formula", amount=5)

    assert only_record(server).volume == expected


async def test_log_bottle_time_defaults_to_now_and_reads_a_naive_time_in_the_ha_zone(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, freezer: Any
) -> None:
    await hass.config.async_set_time_zone("America/New_York")
    freezer.move_to(NOW)

    await call(hass, "log_bottle", kind="formula", amount=4, unit="fl. oz.")
    await call(
        hass, "log_bottle", kind="formula", amount=4, unit="fl. oz.", time="2026-10-26T09:00:00"
    )

    [(_, _, now_record), (_, _, naive_record)] = posted(server)
    assert now_record.time == NOW
    assert naive_record.time.isoformat() == "2026-10-26T13:00:00+00:00"
    assert (now_record.timezone, naive_record.timezone) == (-18000, -18000)


async def test_log_nursing_with_only_one_side_writes_zero_for_the_other(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    await call(hass, "log_nursing", left_minutes=12, end_time="2026-10-26T12:30:00+00:00")

    record = only_record(server)
    assert isinstance(record, Nursing)
    assert (record.left_duration, record.right_duration, record.finish_side) == (12, 0, 0)
    assert record.time.isoformat() == "2026-10-26T12:18:00+00:00"  # the start


async def test_log_nursing_without_an_end_time_ends_now(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, freezer: Any
) -> None:
    freezer.move_to(NOW)

    await call(hass, "log_nursing", right_minutes=10)

    record = only_record(server)
    assert record.time.isoformat() == "2026-10-26T14:50:00+00:00"
    assert record.finish_side == 1


async def test_log_nursing_reads_a_naive_end_time_in_the_ha_zone(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    await hass.config.async_set_time_zone("America/New_York")

    await call(hass, "log_nursing", left_minutes=10, end_time="2026-10-26T09:10:00")

    record = only_record(server)
    assert record.time.isoformat() == "2026-10-26T13:00:00+00:00"
    assert record.timezone == -18000


async def test_log_sleep_writes_the_start_and_a_whole_minute_duration(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    await call(
        hass, "log_sleep", start="2026-10-26T01:00:00+00:00", end="2026-10-26T03:30:30+00:00"
    )

    record = only_record(server)
    assert isinstance(record, Sleep)
    assert record.time.isoformat() == "2026-10-26T01:00:00+00:00"
    assert record.duration == 151  # 150.5 minutes, rounded half up
    assert record.is_running is False


async def test_log_sleep_reads_naive_times_in_the_ha_zone(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    await hass.config.async_set_time_zone("America/New_York")

    await call(hass, "log_sleep", start="2026-10-25T20:00:00", end="2026-10-26T06:00:00")

    record = only_record(server)
    assert record.time.isoformat() == "2026-10-26T00:00:00+00:00"
    assert record.duration == 600
    assert record.timezone == -18000


async def test_log_pump_totals_the_two_sides_and_decodes_back(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    await call(
        hass,
        "log_pump",
        left_amount=6,
        right_amount=7.5,
        unit="fl. oz.",
        left_minutes=10,
        right_minutes=12,
    )

    record = only_record(server)
    assert isinstance(record, Pump)
    assert record.volume == Volume(13.5, "oz")
    assert record.left_volume == Volume(6, "oz")
    assert record.right_volume == Volume(7.5, "oz")
    assert (record.left_duration, record.right_duration) == (10, 12)


async def test_log_pump_with_one_side_only_writes_zero_for_the_other(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    await call(hass, "log_pump", left_amount=3, unit="fl. oz.", left_minutes=15)

    record = only_record(server)
    assert record.right_volume == Volume(0, "oz")
    assert record.volume == Volume(3, "oz")


async def test_log_pump_without_a_unit_uses_the_unit_systems_bottle_unit(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    hass.config.units = US_CUSTOMARY_SYSTEM

    await call(hass, "log_pump", left_amount=3, right_amount=2)

    assert only_record(server).volume == Volume(5, "oz")


@pytest.mark.parametrize("amount", [-1, 0, -0.5, math.inf, math.nan])
async def test_log_bottle_with_a_non_positive_or_non_finite_amount_is_a_validation_error(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    server: FakeServer,
    amount: float,
) -> None:
    sent = len(server.calls)
    with pytest.raises(ServiceValidationError) as raised:
        await call(hass, "log_bottle", kind="formula", amount=amount, unit="fl. oz.")
    assert key_of(raised) == "invalid_amount"
    assert server.posted == []
    assert len(server.calls) == sent  # not even a sync


async def test_log_bottle_with_a_negative_amount_gives_the_translated_amount_error(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    with pytest.raises(ServiceValidationError) as raised:
        await call(hass, "log_bottle", kind="formula", amount=-1, unit="fl. oz.")
    assert key_of(raised) == "invalid_amount"


@pytest.mark.parametrize("kind", ["milk", "", "FORMULA", "nursing"])
async def test_log_bottle_with_an_unknown_kind_is_rejected(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, kind: str
) -> None:
    with pytest.raises(probatio.Invalid):
        await call(hass, "log_bottle", kind=kind, amount=4, unit="fl. oz.")
    assert server.posted == []


@pytest.mark.parametrize("unit", ["cups", "oz", "ml", "", "L"])
async def test_log_bottle_with_an_unknown_unit_is_rejected(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, unit: str
) -> None:
    with pytest.raises(probatio.Invalid):
        await call(hass, "log_bottle", kind="formula", amount=4, unit=unit)
    assert server.posted == []


async def test_log_bottle_needs_a_kind_and_an_amount(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    with pytest.raises(probatio.Invalid):
        await call(hass, "log_bottle", amount=4)
    with pytest.raises(probatio.Invalid):
        await call(hass, "log_bottle", kind="formula")


@pytest.mark.parametrize(
    ("start", "end"),
    [
        ("2026-10-26T03:00:00+00:00", "2026-10-26T02:00:00+00:00"),
        ("2026-10-26T03:00:00+00:00", "2026-10-26T03:00:00+00:00"),
        ("2026-10-26T03:00:00+00:00", "2026-10-26T02:59:59+00:00"),
    ],
    ids=["end-before-start", "end-equals-start", "one-second-early"],
)
async def test_log_sleep_with_the_end_before_the_start_is_a_validation_error(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, start: str, end: str
) -> None:
    sent = len(server.calls)
    with pytest.raises(ServiceValidationError) as raised:
        await call(hass, "log_sleep", start=start, end=end)
    assert key_of(raised) == "end_before_start"
    assert server.posted == []
    assert len(server.calls) == sent


async def test_log_sleep_with_a_naive_end_before_a_zoned_start_is_a_validation_error(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    await hass.config.async_set_time_zone("America/New_York")
    with pytest.raises(ServiceValidationError) as raised:
        # 02:00 in New York is 06:00 UTC: before the 07:00 UTC start.
        await call(hass, "log_sleep", start="2026-10-26T07:00:00+00:00", end="2026-10-26T02:00:00")
    assert key_of(raised) == "end_before_start"


@pytest.mark.parametrize("missing", ["start", "end"])
async def test_log_sleep_needs_both_times(
    hass: HomeAssistant, init_integration: MockConfigEntry, missing: str
) -> None:
    data = {"start": "2026-10-26T01:00:00+00:00", "end": "2026-10-26T02:00:00+00:00"}
    del data[missing]
    with pytest.raises(probatio.Invalid):
        await call(hass, "log_sleep", **data)


async def test_log_nursing_with_no_minutes_is_a_validation_error(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    with pytest.raises(ServiceValidationError) as raised:
        await call(hass, "log_nursing")
    assert key_of(raised) == "no_duration"
    with pytest.raises(ServiceValidationError):
        await call(hass, "log_nursing", left_minutes=0, right_minutes=0)
    assert server.posted == []


@pytest.mark.parametrize("with_id", [False, True], ids=["no-id", "with-id"])
@pytest.mark.parametrize("case", list(LIMITS))
async def test_a_value_past_a_limit_is_a_translated_validation_error_with_no_sync(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    server: FakeServer,
    case: str,
    with_id: bool,
) -> None:
    action, data, key = LIMITS[case]
    extra = {"object_id": RETRY_ID} if with_id else {}
    sent = len(server.calls)

    with pytest.raises(ServiceValidationError) as raised:
        await call(hass, action, **data, **extra)

    assert raised.value.translation_key == key
    assert server.posted == []
    assert len(server.calls) == sent


@pytest.mark.parametrize(
    ("action", "data"),
    [
        ("log_nursing", {"left_minutes": 240, "right_minutes": 240}),
        ("log_pump", {"left_amount": 2, "left_minutes": 240}),
        ("log_sleep", {"start": "2026-10-25T01:00:00+00:00", "end": "2026-10-26T01:00:00+00:00"}),
        ("log_bottle", {"kind": "formula", "amount": 0.01, "unit": "fl. oz."}),
    ],
    ids=["nursing", "pump", "sleep", "bottle"],
)
async def test_the_values_at_the_limits_are_accepted(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    server: FakeServer,
    action: str,
    data: dict[str, Any],
) -> None:
    await call(hass, action, **data)

    assert len(posted(server)) == 1


@pytest.mark.parametrize("action", ["log_bottle", "log_nursing", "log_sleep", "log_pump"])
async def test_a_value_error_that_is_not_an_id_clash_is_not_reported_as_one(
    hass: HomeAssistant, init_integration: MockConfigEntry, action: str
) -> None:
    engine_method = {
        "log_bottle": "add_formula",
        "log_nursing": "add_nursing",
        "log_sleep": "add_sleep",
        "log_pump": "add_pump",
    }[action]
    data, _, _ = VALID[{"log_bottle": "log_bottle"}.get(action, action)]
    with (
        patch.object(SyncEngine, engine_method, side_effect=ValueError("some other refusal")),
        pytest.raises(ValueError, match="some other refusal"),
    ):
        await call(hass, action, object_id=RETRY_ID, **data)


@pytest.mark.parametrize("minutes", [-1, 2.5, "ten"])
async def test_log_nursing_rejects_negative_or_fractional_minutes(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, minutes: Any
) -> None:
    with pytest.raises(probatio.Invalid):
        await call(hass, "log_nursing", left_minutes=minutes)
    assert server.posted == []


async def test_log_nursing_rejects_an_unknown_finish_side(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    with pytest.raises(probatio.Invalid):
        await call(hass, "log_nursing", left_minutes=5, finish_side="both")


async def test_log_pump_with_no_amount_is_a_validation_error(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    with pytest.raises(ServiceValidationError) as raised:
        await call(hass, "log_pump", left_minutes=10)
    assert key_of(raised) == "no_amount"
    with pytest.raises(ServiceValidationError) as raised:
        await call(hass, "log_pump", left_amount=0, right_amount=0)
    assert key_of(raised) == "invalid_amount"
    assert server.posted == []


async def test_log_pump_with_a_negative_side_is_a_validation_error(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    with pytest.raises(ServiceValidationError) as raised:
        await call(hass, "log_pump", left_amount=-2, right_amount=5)
    assert key_of(raised) == "invalid_amount"
    assert server.posted == []


@pytest.mark.parametrize("name", [n for n in ACTIONS if n != "log_pump"])
async def test_each_action_for_an_unknown_baby_device_is_a_validation_error(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, name: str
) -> None:
    data, _, _ = VALID[name]
    with pytest.raises(ServiceValidationError) as raised:
        await hass.services.async_call(
            DOMAIN, ACTION_OF.get(name, name), {"device_id": "nope", **data}, blocking=True
        )
    assert key_of(raised) == "baby_not_found"
    assert server.posted == []


@pytest.mark.parametrize("name", ACTIONS)
async def test_a_retry_with_the_same_object_id_does_not_post_twice(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, name: str
) -> None:
    await run_valid(hass, name, object_id=RETRY_ID)
    await run_valid(hass, name, object_id=RETRY_ID.lower())

    [(number, _, record)] = posted(server)
    assert (number, record.object_id) == (1, RETRY_ID)


@pytest.mark.parametrize("name", ACTIONS)
async def test_each_action_rejects_an_object_id_that_is_not_a_uuid(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, name: str
) -> None:
    with pytest.raises(probatio.Invalid):
        await run_valid(hass, name, object_id="retry-1")
    assert server.posted == []


@pytest.mark.parametrize("name", ACTIONS)
async def test_an_object_id_that_belongs_to_another_record_is_a_validation_error(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, name: str
) -> None:
    with pytest.raises(ServiceValidationError) as raised:
        await run_valid(hass, name, object_id=OTHER_DEVICES_RECORD)
    assert key_of(raised) == "object_id_in_use"
    assert server.posted == []


@pytest.mark.parametrize("name", ACTIONS)
async def test_a_failed_write_raises_a_translated_error_and_changes_no_sensor(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, name: str
) -> None:
    before = {s.entity_id: (s.state, dict(s.attributes)) for s in hass.states.async_all()}
    server.post_errors.append((BabyTrackerConnectionError("the service answered 500"), False))

    with pytest.raises(HomeAssistantError) as raised:
        await run_valid(hass, name)

    assert raised.value.translation_key == "write_failed"
    assert {s.entity_id: (s.state, dict(s.attributes)) for s in hass.states.async_all()} == before
    assert server.posted == []


async def test_a_sleep_updates_the_last_sleep_sensor_and_leaves_the_baby_awake(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    assert hass.states.get(ASLEEP).state == "off"

    await call(
        hass, "log_sleep", start="2026-10-26T01:00:00+00:00", end="2026-10-26T02:30:00+00:00"
    )

    state = hass.states.get(LAST_SLEEP)
    assert state.state == "2026-10-26T01:00:00+00:00"
    assert state.attributes["duration_minutes"] == 90
    assert hass.states.get(ASLEEP).state == "off"


async def test_nursing_counts_as_a_feed_today_and_adds_no_bottle_volume(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, freezer: Any
) -> None:
    await hass.config.async_set_time_zone("America/New_York")
    freezer.move_to(NOW)

    await call(hass, "log_nursing", left_minutes=10)

    assert hass.states.get(FEEDS_TODAY).state == "1"
    assert float(hass.states.get(BOTTLE_TODAY).state) == 0


async def test_a_pump_and_a_sleep_are_not_feeds_today(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, freezer: Any
) -> None:
    await hass.config.async_set_time_zone("America/New_York")
    freezer.move_to(NOW)

    await call(hass, "log_pump", left_amount=3, unit="fl. oz.", time="2026-10-26T14:00:00+00:00")
    await call(
        hass, "log_sleep", start="2026-10-26T12:00:00+00:00", end="2026-10-26T13:00:00+00:00"
    )

    assert hass.states.get(FEEDS_TODAY).state == "0"
    assert float(hass.states.get(BOTTLE_TODAY).state) == 0


async def test_undo_removes_a_bottle_and_the_sensor_goes_back(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    await call(
        hass,
        "log_bottle",
        kind="formula",
        amount=4,
        unit="fl. oz.",
        time="2026-10-26T09:15:00+00:00",
    )
    assert hass.states.get(LAST_FEED).state == "2026-10-26T09:15:00+00:00"

    await undo(hass, device_id=device_id(hass, BABY_ONE_ID))

    assert hass.states.get(LAST_FEED).state == "unknown"
    [(_, op_add, added), (number, op_delete, deleted)] = posted(server)
    assert (op_add, op_delete, number) == (0, 2, 2)
    assert deleted.object_id == added.object_id


@pytest.mark.parametrize("name", ACTIONS)
async def test_undo_removes_a_record_of_every_new_type(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, name: str
) -> None:
    await run_valid(hass, name)
    added = only_record(server)

    await undo(hass, config_entry_id=init_integration.entry_id)

    [_, (_, op, deleted)] = posted(server)
    assert op == 2
    assert deleted.object_id == added.object_id
    assert deleted.record_type == added.record_type


async def test_undo_by_record_id_removes_a_bottle_among_other_writes(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    await run_valid(hass, "log_bottle", object_id=RETRY_ID)
    await run_valid(hass, "log_sleep")

    await undo(hass, device_id=device_id(hass, BABY_ONE_ID), object_id=RETRY_ID)

    [_, _, (_, op, deleted)] = posted(server)
    assert (op, deleted.object_id) == (2, RETRY_ID)


@pytest.mark.parametrize("action", ACTION_IDS)
async def test_a_baby_action_works_with_device_id_and_raises_no_repair(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: Any, freezer: Any, action: str
) -> None:
    await _prepare(hass, action, freezer)
    before = _effect(hass, server)

    await _call(
        hass, action, {"device_id": device_id(hass, BABY_ONE_ID), **BABY_ACTIONS[action][0]}
    )

    assert _effect(hass, server) != before
    assert _issues(hass) == []


@pytest.mark.parametrize("action", ACTION_IDS)
async def test_the_deprecated_baby_field_still_works_and_raises_a_repair(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: Any, freezer: Any, action: str
) -> None:
    await _prepare(hass, action, freezer)
    before = _effect(hass, server)

    await _call(hass, action, {"baby": device_id(hass, BABY_ONE_ID), **BABY_ACTIONS[action][0]})

    assert _effect(hass, server) != before
    issue = ir.async_get(hass).async_get_issue(DOMAIN, f"deprecated_{action}_baby")
    assert issue is not None
    assert issue.translation_key == "deprecated_action_field"
    assert issue.translation_placeholders == {
        "action": action,
        "field": "baby",
        "replacement": "device_id",
    }
    assert issue.severity is ir.IssueSeverity.WARNING
    assert issue.is_fixable is False


async def test_using_the_alias_again_keeps_a_single_repair(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    for _ in range(3):
        await _call(hass, "log_diaper", {"baby": device_id(hass, BABY_ONE_ID), "type": "wet"})

    assert [issue.issue_id for issue in _issues(hass)] == ["deprecated_log_diaper_baby"]


async def test_each_action_gets_its_own_repair(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    baby = device_id(hass, BABY_ONE_ID)
    await _call(hass, "log_diaper", {"baby": baby, "type": "wet"})
    await _call(hass, "log_bottle", {"baby": baby, "kind": "formula", "amount": 4})

    assert {issue.issue_id for issue in _issues(hass)} == {
        "deprecated_log_diaper_baby",
        "deprecated_log_bottle_baby",
    }


@pytest.mark.parametrize("action", ACTION_IDS)
async def test_a_baby_action_with_both_fields_is_refused(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: Any, action: str
) -> None:
    baby = device_id(hass, BABY_ONE_ID)
    with pytest.raises(vol.Invalid, match="at most one"):
        await _call(hass, action, {"device_id": baby, "baby": baby, **BABY_ACTIONS[action][0]})
    assert server.posted == []
    assert _issues(hass) == []


@pytest.mark.parametrize("action", ACTION_IDS)
async def test_a_baby_action_with_no_target_is_refused(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: Any, action: str
) -> None:
    with pytest.raises(vol.Invalid, match="at least one"):
        await _call(hass, action, dict(BABY_ACTIONS[action][0]))
    assert server.posted == []


@pytest.mark.parametrize("field", ["device_id", "baby"])
async def test_an_unknown_baby_device_is_a_validation_error(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: Any, field: str
) -> None:
    with pytest.raises(ServiceValidationError) as raised:
        await _call(hass, "log_diaper", {field: "0" * 32, "type": "wet"})
    assert raised.value.translation_key == "baby_not_found"
    assert server.posted == []


@pytest.mark.parametrize("field", ["device_id", "baby"])
async def test_the_account_device_is_not_a_baby(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: Any, field: str
) -> None:
    with pytest.raises(ServiceValidationError) as raised:
        await _call(
            hass,
            "log_diaper",
            {field: account_device(hass, init_integration).id, "type": "wet"},
        )
    assert raised.value.translation_key == "baby_not_found"
    assert server.posted == []


async def test_a_baby_of_an_unloaded_account_says_the_account_is_not_loaded(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: Any
) -> None:
    baby = device_id(hass, BABY_ONE_ID)
    await hass.config_entries.async_unload(init_integration.entry_id)
    assert init_integration.state is ConfigEntryState.NOT_LOADED

    with pytest.raises(ServiceValidationError) as raised:
        await _call(hass, "log_diaper", {"device_id": baby, "type": "wet"})
    assert raised.value.translation_domain == "homeassistant"
    assert raised.value.translation_key == "service_config_entry_not_loaded"


async def test_the_device_id_of_a_second_account_writes_to_that_account(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    second_entry: MockConfigEntry,
    server: Any,
    second_server: Any,
) -> None:
    baby_three = device_id(hass, BABY_THREE_ID)

    await _call(hass, "log_diaper", {"device_id": baby_three, "type": "wet"})

    assert server.posted == []
    assert len(second_server.posted) == 1


async def test_log_pump_takes_an_account_with_config_entry_id(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: Any
) -> None:
    await _call(hass, "log_pump", {"config_entry_id": init_integration.entry_id, "left_amount": 3})

    assert len(server.posted) == 1
    assert _issues(hass) == []


@pytest.mark.parametrize("alias", ["device", "baby"])
async def test_log_pump_still_takes_a_device_and_raises_a_repair(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: Any, alias: str
) -> None:
    await _call(
        hass, "log_pump", {alias: account_device(hass, init_integration).id, "left_amount": 3}
    )

    assert len(server.posted) == 1
    issue = ir.async_get(hass).async_get_issue(DOMAIN, f"deprecated_log_pump_{alias}")
    assert issue is not None
    assert issue.translation_key == "deprecated_action_field"
    assert issue.translation_placeholders == {
        "action": "log_pump",
        "field": alias,
        "replacement": "config_entry_id",
    }
    assert issue.severity is ir.IssueSeverity.WARNING
    assert issue.is_fixable is False


@pytest.mark.parametrize("alias", ["device", "baby"])
async def test_log_pump_with_a_babys_device_still_reaches_the_account(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: Any, alias: str
) -> None:
    await _call(hass, "log_pump", {alias: device_id(hass, BABY_ONE_ID), "left_amount": 3})

    assert len(server.posted) == 1


@pytest.mark.parametrize(
    "fields",
    [("config_entry_id", "device"), ("config_entry_id", "baby"), ("device", "baby")],
)
async def test_log_pump_with_two_targets_is_refused(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: Any, fields: tuple[str, str]
) -> None:
    values = {
        "config_entry_id": init_integration.entry_id,
        "device": account_device(hass, init_integration).id,
        "baby": device_id(hass, BABY_ONE_ID),
    }
    with pytest.raises(vol.Invalid, match="at most one"):
        await _call(
            hass, "log_pump", {**{field: values[field] for field in fields}, "left_amount": 3}
        )
    assert server.posted == []


async def test_log_pump_with_no_target_is_refused(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: Any
) -> None:
    with pytest.raises(vol.Invalid, match="at least one"):
        await _call(hass, "log_pump", {"left_amount": 3})
    assert server.posted == []


async def test_undo_with_device_id_removes_that_babys_latest_write(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: Any, freezer: Any
) -> None:
    first, _ = await _two_diapers(hass, freezer, server)

    await _call(hass, "undo", {"device_id": device_id(hass, BABY_ONE_ID)})

    assert _deleted(server) == [first]
    assert _issues(hass) == []


async def test_undo_with_config_entry_id_removes_the_accounts_latest_write(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: Any, freezer: Any
) -> None:
    _, second = await _two_diapers(hass, freezer, server)

    await _call(hass, "undo", {"config_entry_id": init_integration.entry_id})

    assert _deleted(server) == [second]
    assert _issues(hass) == []


async def test_undo_still_takes_baby_and_raises_a_repair(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: Any, freezer: Any
) -> None:
    first, _ = await _two_diapers(hass, freezer, server)

    await _call(hass, "undo", {"baby": device_id(hass, BABY_ONE_ID)})

    assert _deleted(server) == [first]
    issue = ir.async_get(hass).async_get_issue(DOMAIN, "deprecated_undo_baby")
    assert issue is not None
    assert issue.translation_placeholders == {
        "action": "undo",
        "field": "baby",
        "replacement": "device_id",
    }
    assert issue.severity is ir.IssueSeverity.WARNING


async def test_undo_with_no_target_is_refused(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    with pytest.raises(vol.Invalid, match="at least one"):
        await _call(hass, "undo", {})


@pytest.mark.parametrize("action", ["log_pump", "undo"])
async def test_an_unknown_account_is_a_validation_error(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: Any, action: str
) -> None:
    data = {
        "config_entry_id": "no-such-entry",
        **({"left_amount": 3} if action == "log_pump" else {}),
    }
    with pytest.raises(ServiceValidationError) as raised:
        await _call(hass, action, data)
    assert raised.value.translation_domain == "homeassistant"
    assert raised.value.translation_key == "service_config_entry_not_found"
    assert server.posted == []


@pytest.mark.parametrize("action", ["log_pump", "undo"])
async def test_an_account_that_is_not_loaded_is_a_validation_error(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: Any, action: str
) -> None:
    await hass.config_entries.async_unload(init_integration.entry_id)
    data = {
        "config_entry_id": init_integration.entry_id,
        **({"left_amount": 3} if action == "log_pump" else {}),
    }
    with pytest.raises(ServiceValidationError) as raised:
        await _call(hass, action, data)
    assert raised.value.translation_domain == "homeassistant"
    assert raised.value.translation_key == "service_config_entry_not_loaded"


@pytest.mark.parametrize("action", ["log_pump", "undo"])
async def test_another_integrations_entry_is_a_validation_error(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: Any, action: str
) -> None:
    other = MockConfigEntry(domain="other_integration")
    other.add_to_hass(hass)
    data = {
        "config_entry_id": other.entry_id,
        **({"left_amount": 3} if action == "log_pump" else {}),
    }

    with pytest.raises(ServiceValidationError) as raised:
        await _call(hass, action, data)

    assert raised.value.translation_key == "service_config_entry_wrong_domain"
    assert server.posted == []


async def test_setting_up_the_actions_registers_every_one_of_them(hass: HomeAssistant) -> None:
    from custom_components.nighp_baby_tracker.services import async_setup_services

    async_setup_services(hass)

    assert set(hass.services.async_services_for_domain(DOMAIN)) == set(ALL_ACTIONS)


async def test_every_registered_action_name_is_a_service_member(hass: HomeAssistant) -> None:
    from custom_components.nighp_baby_tracker.const import BabyTrackerService
    from custom_components.nighp_baby_tracker.services import async_setup_services

    async_setup_services(hass)

    registered = set(hass.services.async_services_for_domain(DOMAIN))
    assert registered - {member.value for member in BabyTrackerService} == set()
    assert {member.value for member in BabyTrackerService} - registered == set()


def test_the_coordinator_has_no_timer_forwarding_methods() -> None:
    from custom_components.nighp_baby_tracker.coordinator import BabyTrackerCoordinator

    forwarding = {"nursing", "pump", "async_set_nursing", "async_set_pump"}

    assert {name for name in forwarding if hasattr(BabyTrackerCoordinator, name)} == set()


async def test_every_schema_field_is_an_argument_member(hass: HomeAssistant) -> None:
    from custom_components.nighp_baby_tracker.services import (
        BabyTrackerServiceArgument,
        async_setup_services,
    )

    async_setup_services(hass)

    members = {member.value for member in BabyTrackerServiceArgument}
    fields: set[str] = set()
    for action, handler in hass.services.async_services_for_domain(DOMAIN).items():
        action_fields = _schema_fields(handler.schema)
        assert action_fields - members == set(), action
        fields |= action_fields
    assert members - fields == set()


@pytest.mark.parametrize(
    ("zone", "time", "expected"),
    [
        ("America/New_York", "2026-07-15T12:00:00", -18000),
        ("America/New_York", "2026-01-15T12:00:00", -18000),
        ("America/New_York", "2026-07-15T16:00:00+00:00", -18000),
        ("America/Chicago", "2026-07-15T12:00:00", -21600),
        ("Asia/Kolkata", "2026-07-15T12:00:00", 19800),
        ("UTC", "2026-07-15T12:00:00", 0),
        ("Australia/Sydney", "2026-01-15T12:00:00", 36000),
    ],
    ids=["july", "january", "aware-july", "chicago", "no-dst", "utc", "southern-dst"],
)
async def test_a_write_sends_the_standard_offset_of_the_records_own_moment(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    server: FakeServer,
    zone: str,
    time: str,
    expected: int,
) -> None:
    await hass.config.async_set_time_zone(zone)

    await log_diaper(hass, BABY_ONE_ID, "wet", time=time)

    assert only_record(server).timezone == expected


async def test_a_backdated_write_uses_the_standard_offset_not_the_offset_now(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, freezer: Any
) -> None:
    await hass.config.async_set_time_zone("America/New_York")
    freezer.move_to(datetime(2026, 1, 20, 12, 0, tzinfo=UTC))

    await log_diaper(hass, BABY_ONE_ID, "wet", time="2026-01-19T08:00:00")
    await call(hass, "log_sleep", start="2026-01-19T20:00:00", end="2026-01-19T22:00:00")

    assert [r.timezone for _, _, r in posted(server)] == [-18000, -18000]


@pytest.mark.parametrize(
    "time",
    [
        "2026-03-08T06:59:59+00:00",
        "2026-03-08T07:00:00+00:00",
        "2026-11-01T05:59:59+00:00",
        "2026-11-01T06:00:00+00:00",
    ],
    ids=["spring-before", "spring-after", "fall-before", "fall-after"],
)
async def test_a_write_at_the_exact_dst_transition_sends_the_same_offset_on_both_sides(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, time: str
) -> None:
    await hass.config.async_set_time_zone("America/New_York")

    await log_diaper(hass, BABY_ONE_ID, "wet", time=time)

    assert only_record(server).timezone == -18000
