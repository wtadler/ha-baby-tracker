"""Core PR 9: the write path shared by every action, the first action (log_diaper), and how the
actions are registered. Several tests here run over every action, so the cut trims their
parametrization to the actions the PR ships."""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

import probatio
import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.nighp_baby_tracker.const import (
    DOMAIN,
)
from custom_components.nighp_baby_tracker.pybabytracker import (
    OP_ADD,
    BabyTrackerConnectionError,
    SyncEngine,
)

from . import OWN_DEVICE, OWN_DEVICE_TWO
from .factories import (
    BABY_ONE_ID,
    BABY_THREE_ID,
    BABY_TWO_ID,
    DEVICE_A,
    at,
)
from .fake_server import FakeServer
from .helpers import (
    ALL_ACTIONS,
    LAST_DIAPER,
    NOW,
    OTHER_DEVICES_RECORD,
    account_device,
    call,
    device_id,
    log_diaper,
    only_record,
    posted,
)


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


def _schema_fields(schema: Any) -> set[str]:
    """Return the field names of an action's schema, which may be wrapped in ``All``."""
    if isinstance(schema, probatio.All):
        return _schema_fields(schema.validators[0])
    return {str(getattr(key, "schema", key)) for key in schema.schema}


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
