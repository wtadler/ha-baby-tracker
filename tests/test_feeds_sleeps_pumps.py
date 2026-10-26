# ruff: noqa: F811
"""Bottle, nursing, sleep, and pump actions, plus the Bottle button and its options."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import pytest
import voluptuous as vol
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType, InvalidData
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.util.unit_system import METRIC_SYSTEM, US_CUSTOMARY_SYSTEM
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.nighp_baby_tracker.const import (
    CONF_BOTTLE_SIZE,
    CONF_BOTTLE_TYPE,
    CONF_BOTTLE_UNIT,
    CONF_SCAN_INTERVAL,
    DOMAIN,
)
from custom_components.nighp_baby_tracker.pybabytracker import (
    BabyTrackerConnectionError,
    Formula,
    Nursing,
    Pump,
    Pumped,
    Sleep,
    Volume,
)

from .conftest import FakeServer
from .factories import BABY_ONE_ID, BABY_TWO_ID
from .test_writes import (  # noqa: F401  (fixtures: a fake that can post, and a second account)
    NOW,
    OTHER_DEVICES_RECORD,
    button,
    device_id,
    fake_client,
    posted,
    press,
    second_entry,
    second_server,
    server,
    undo,
)

CAPTURE = json.loads((Path(__file__).parent / "fixtures" / "captured_payloads.json").read_text())
IGNORED = ("objectID", "timestamp", "note", "baby")
RETRY_ID = "7E57ED00-0000-4000-8000-000000000001"

BOTTLE_BUTTON = "button.baby_one_bottle"
LAST_FEED = "sensor.baby_one_last_feed"
LAST_PUMP = "sensor.baby_one_last_pump"
LAST_SLEEP = "sensor.baby_one_last_sleep"
FEEDS_TODAY = "sensor.baby_one_feeds_today"
BOTTLE_TODAY = "sensor.baby_one_bottle_volume_today"
ASLEEP = "binary_sensor.baby_one_asleep"


def captured(prefix: str) -> dict[str, Any]:
    """The captured add payload for a label, minus the keys a write fills in itself."""
    [entry] = [e for e in CAPTURE if e["label"].startswith(prefix) and e["opcode"] == 0]
    return {k: v for k, v in entry["payload"].items() if k not in IGNORED}


async def call(hass: HomeAssistant, action: str, baby_id: str = BABY_ONE_ID, **data: Any) -> None:
    await hass.services.async_call(
        DOMAIN, action, {"baby": device_id(hass, baby_id), **data}, blocking=True
    )


async def set_options(hass: HomeAssistant, entry: MockConfigEntry, **options: Any) -> None:
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], options)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()


def only_record(server: FakeServer) -> Any:
    [(_, _, record)] = posted(server)
    return record


# One valid call per action, with values that reproduce the captured record (the zone makes
# the capture's -18000 offset).
CHICAGO = "America/Chicago"
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


# --- AC1: every action posts the captured format ------------------------------------------


@pytest.mark.parametrize("name", ACTIONS)
async def test_each_action_posts_a_record_matching_the_captured_format(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer, name: str
) -> None:
    await hass.config.async_set_time_zone(CHICAGO)
    _, label, tweaks = VALID[name]

    await run_valid(hass, name, note="a note")

    record = only_record(server)
    expected = {**captured(label), **tweaks}
    payload = record.to_payload()
    assert {k: v for k, v in payload.items() if k not in IGNORED} == expected
    assert payload["note"] == "a note"
    assert payload["baby"]["objectID"] == BABY_ONE_ID
    assert payload["newFlage"] == "true"


@pytest.mark.parametrize("name", ACTIONS)
async def test_each_action_posts_an_add_under_the_next_number_with_a_new_uppercase_id(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer, name: str
) -> None:
    await run_valid(hass, name)
    await run_valid(hass, name)

    [(n1, op1, first), (n2, op2, second)] = posted(server)
    assert (n1, op1, n2, op2) == (1, 0, 2, 0)
    assert first.object_id != second.object_id
    assert first.object_id == first.object_id.upper()
    assert len(first.object_id) == 36


@pytest.mark.parametrize("name", ACTIONS)
async def test_each_action_goes_to_the_baby_named(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer, name: str
) -> None:
    data, _, _ = VALID[name]
    await call(hass, ACTION_OF.get(name, name), BABY_TWO_ID, **data)

    assert only_record(server).baby.object_id == BABY_TWO_ID


# --- log_bottle ------------------------------------------------------------------------------


async def test_log_bottle_writes_formula_or_pumped_by_kind(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer
) -> None:
    await call(hass, "log_bottle", kind="formula", amount=4, unit="fl. oz.")
    await call(hass, "log_bottle", kind="pumped", amount=3, unit="fl. oz.")

    [(_, _, first), (_, _, second)] = posted(server)
    assert isinstance(first, Formula)
    assert isinstance(second, Pumped)
    assert first.volume == Volume(4, "oz")
    assert second.volume == Volume(3, "oz")


@pytest.mark.parametrize(
    ("action", "data"),
    [
        ("log_bottle", {"kind": "formula", "amount": 120}),
        ("log_bottle", {"kind": "pumped", "amount": 90}),
        ("log_pump", {"left_amount": 60, "right_amount": 75}),
        ("log_pump", {"left_amount": 60}),
    ],
    ids=["formula", "pumped", "pump", "pump-one-side"],
)
async def test_a_milliliter_write_is_refused_with_a_translated_error_and_no_request(
    hass: HomeAssistant,
    setup_entry: MockConfigEntry,
    server: FakeServer,
    action: str,
    data: dict[str, Any],
) -> None:
    sent = len(server.calls)  # type: ignore[attr-defined]
    with pytest.raises(ServiceValidationError) as raised:
        await call(hass, action, unit="mL", **data)
    assert raised.value.translation_key == "metric_not_supported"
    assert server.posted == []  # type: ignore[attr-defined]
    assert len(server.calls) == sent  # type: ignore[attr-defined]  # not even a sync


async def test_log_bottle_in_ounces_writes_english_measure_true(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer
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
    setup_entry: MockConfigEntry,
    server: FakeServer,
    units: Any,
    unit: str,
    expected: Volume,
) -> None:
    hass.config.units = units

    await call(hass, "log_bottle", kind="formula", amount=5)

    assert only_record(server).volume == expected


async def test_log_bottle_time_defaults_to_now_and_reads_a_naive_time_in_the_ha_zone(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer, freezer: Any
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
    assert (now_record.timezone, naive_record.timezone) == (-14400, -14400)


# --- log_nursing -----------------------------------------------------------------------------


async def test_log_nursing_with_only_one_side_writes_zero_for_the_other(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer
) -> None:
    await call(hass, "log_nursing", left_minutes=12, end_time="2026-10-26T12:30:00+00:00")

    record = only_record(server)
    assert isinstance(record, Nursing)
    assert (record.left_duration, record.right_duration, record.finish_side) == (12, 0, 0)
    assert record.time.isoformat() == "2026-10-26T12:18:00+00:00"  # the start


async def test_log_nursing_without_an_end_time_ends_now(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer, freezer: Any
) -> None:
    freezer.move_to(NOW)

    await call(hass, "log_nursing", right_minutes=10)

    record = only_record(server)
    assert record.time.isoformat() == "2026-10-26T14:50:00+00:00"
    assert record.finish_side == 1


async def test_log_nursing_reads_a_naive_end_time_in_the_ha_zone(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer
) -> None:
    await hass.config.async_set_time_zone("America/New_York")

    await call(hass, "log_nursing", left_minutes=10, end_time="2026-10-26T09:10:00")

    record = only_record(server)
    assert record.time.isoformat() == "2026-10-26T13:00:00+00:00"
    assert record.timezone == -14400


# --- log_sleep -------------------------------------------------------------------------------


async def test_log_sleep_writes_the_start_and_a_whole_minute_duration(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer
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
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer
) -> None:
    await hass.config.async_set_time_zone("America/New_York")

    await call(hass, "log_sleep", start="2026-10-25T20:00:00", end="2026-10-26T06:00:00")

    record = only_record(server)
    assert record.time.isoformat() == "2026-10-26T00:00:00+00:00"
    assert record.duration == 600
    assert record.timezone == -14400


# --- log_pump --------------------------------------------------------------------------------


async def test_log_pump_totals_the_two_sides_and_decodes_back(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer
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
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer
) -> None:
    await call(hass, "log_pump", left_amount=3, unit="fl. oz.", left_minutes=15)

    record = only_record(server)
    assert record.right_volume == Volume(0, "oz")
    assert record.volume == Volume(3, "oz")


async def test_log_pump_without_a_unit_uses_the_unit_systems_bottle_unit(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer
) -> None:
    hass.config.units = US_CUSTOMARY_SYSTEM

    await call(hass, "log_pump", left_amount=3, right_amount=2)

    assert only_record(server).volume == Volume(5, "oz")


# --- validation: refused before any request ---------------------------------------------------


def key_of(raised: pytest.ExceptionInfo[ServiceValidationError]) -> str | None:
    return raised.value.translation_key


@pytest.mark.parametrize("amount", [-1, 0, -0.5, math.inf, math.nan])
async def test_log_bottle_with_a_non_positive_or_non_finite_amount_is_a_validation_error(
    hass: HomeAssistant,
    setup_entry: MockConfigEntry,
    server: FakeServer,
    amount: float,
) -> None:
    sent = len(server.calls)  # type: ignore[attr-defined]
    with pytest.raises(ServiceValidationError) as raised:
        await call(hass, "log_bottle", kind="formula", amount=amount, unit="fl. oz.")
    assert key_of(raised) == "invalid_amount"
    assert server.posted == []  # type: ignore[attr-defined]
    assert len(server.calls) == sent  # type: ignore[attr-defined]  # not even a sync


async def test_log_bottle_with_a_negative_amount_gives_the_translated_amount_error(
    hass: HomeAssistant, setup_entry: MockConfigEntry
) -> None:
    with pytest.raises(ServiceValidationError) as raised:
        await call(hass, "log_bottle", kind="formula", amount=-1, unit="fl. oz.")
    assert key_of(raised) == "invalid_amount"


@pytest.mark.parametrize("kind", ["milk", "", "FORMULA", "nursing"])
async def test_log_bottle_with_an_unknown_kind_is_rejected(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer, kind: str
) -> None:
    with pytest.raises(vol.Invalid):
        await call(hass, "log_bottle", kind=kind, amount=4, unit="fl. oz.")
    assert server.posted == []  # type: ignore[attr-defined]


@pytest.mark.parametrize("unit", ["cups", "oz", "ml", "", "L"])
async def test_log_bottle_with_an_unknown_unit_is_rejected(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer, unit: str
) -> None:
    with pytest.raises(vol.Invalid):
        await call(hass, "log_bottle", kind="formula", amount=4, unit=unit)
    assert server.posted == []  # type: ignore[attr-defined]


async def test_log_bottle_needs_a_kind_and_an_amount(
    hass: HomeAssistant, setup_entry: MockConfigEntry
) -> None:
    with pytest.raises(vol.Invalid):
        await call(hass, "log_bottle", amount=4)
    with pytest.raises(vol.Invalid):
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
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer, start: str, end: str
) -> None:
    sent = len(server.calls)  # type: ignore[attr-defined]
    with pytest.raises(ServiceValidationError) as raised:
        await call(hass, "log_sleep", start=start, end=end)
    assert key_of(raised) == "end_before_start"
    assert server.posted == []  # type: ignore[attr-defined]
    assert len(server.calls) == sent  # type: ignore[attr-defined]


async def test_log_sleep_with_a_naive_end_before_a_zoned_start_is_a_validation_error(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer
) -> None:
    await hass.config.async_set_time_zone("America/New_York")
    with pytest.raises(ServiceValidationError) as raised:
        # 02:00 in New York is 06:00 UTC: before the 07:00 UTC start.
        await call(hass, "log_sleep", start="2026-10-26T07:00:00+00:00", end="2026-10-26T02:00:00")
    assert key_of(raised) == "end_before_start"


@pytest.mark.parametrize("missing", ["start", "end"])
async def test_log_sleep_needs_both_times(
    hass: HomeAssistant, setup_entry: MockConfigEntry, missing: str
) -> None:
    data = {"start": "2026-10-26T01:00:00+00:00", "end": "2026-10-26T02:00:00+00:00"}
    del data[missing]
    with pytest.raises(vol.Invalid):
        await call(hass, "log_sleep", **data)


async def test_log_nursing_with_no_minutes_is_a_validation_error(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer
) -> None:
    with pytest.raises(ServiceValidationError) as raised:
        await call(hass, "log_nursing")
    assert key_of(raised) == "no_duration"
    with pytest.raises(ServiceValidationError):
        await call(hass, "log_nursing", left_minutes=0, right_minutes=0)
    assert server.posted == []  # type: ignore[attr-defined]


@pytest.mark.parametrize("minutes", [-1, 2.5, "ten"])
async def test_log_nursing_rejects_negative_or_fractional_minutes(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer, minutes: Any
) -> None:
    with pytest.raises(vol.Invalid):
        await call(hass, "log_nursing", left_minutes=minutes)
    assert server.posted == []  # type: ignore[attr-defined]


async def test_log_nursing_rejects_an_unknown_finish_side(
    hass: HomeAssistant, setup_entry: MockConfigEntry
) -> None:
    with pytest.raises(vol.Invalid):
        await call(hass, "log_nursing", left_minutes=5, finish_side="both")


async def test_log_pump_with_no_amount_is_a_validation_error(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer
) -> None:
    with pytest.raises(ServiceValidationError) as raised:
        await call(hass, "log_pump", left_minutes=10)
    assert key_of(raised) == "no_amount"
    with pytest.raises(ServiceValidationError) as raised:
        await call(hass, "log_pump", left_amount=0, right_amount=0)
    assert key_of(raised) == "invalid_amount"
    assert server.posted == []  # type: ignore[attr-defined]


async def test_log_pump_with_a_negative_side_is_a_validation_error(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer
) -> None:
    with pytest.raises(ServiceValidationError) as raised:
        await call(hass, "log_pump", left_amount=-2, right_amount=5)
    assert key_of(raised) == "invalid_amount"
    assert server.posted == []  # type: ignore[attr-defined]


@pytest.mark.parametrize("name", ACTIONS)
async def test_each_action_for_an_unknown_baby_device_is_a_validation_error(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer, name: str
) -> None:
    data, _, _ = VALID[name]
    with pytest.raises(ServiceValidationError) as raised:
        await hass.services.async_call(
            DOMAIN, ACTION_OF.get(name, name), {"baby": "nope", **data}, blocking=True
        )
    assert key_of(raised) == "baby_not_found"
    assert server.posted == []  # type: ignore[attr-defined]


# --- retries and failures --------------------------------------------------------------------


@pytest.mark.parametrize("name", ACTIONS)
async def test_a_retry_with_the_same_object_id_does_not_post_twice(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer, name: str
) -> None:
    await run_valid(hass, name, object_id=RETRY_ID)
    await run_valid(hass, name, object_id=RETRY_ID.lower())

    [(number, _, record)] = posted(server)
    assert (number, record.object_id) == (1, RETRY_ID)


@pytest.mark.parametrize("name", ACTIONS)
async def test_each_action_rejects_an_object_id_that_is_not_a_uuid(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer, name: str
) -> None:
    with pytest.raises(vol.Invalid):
        await run_valid(hass, name, object_id="retry-1")
    assert server.posted == []  # type: ignore[attr-defined]


@pytest.mark.parametrize("name", ACTIONS)
async def test_an_object_id_that_belongs_to_another_record_is_a_validation_error(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer, name: str
) -> None:
    with pytest.raises(ServiceValidationError) as raised:
        await run_valid(hass, name, object_id=OTHER_DEVICES_RECORD)
    assert key_of(raised) == "object_id_in_use"
    assert server.posted == []  # type: ignore[attr-defined]


@pytest.mark.parametrize("name", ACTIONS)
async def test_a_failed_write_raises_a_translated_error_and_changes_no_sensor(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer, name: str
) -> None:
    before = {s.entity_id: (s.state, dict(s.attributes)) for s in hass.states.async_all()}
    server.post_errors.append((BabyTrackerConnectionError("the service answered 500"), False))  # type: ignore[attr-defined]

    with pytest.raises(HomeAssistantError) as raised:
        await run_valid(hass, name)

    assert raised.value.translation_key == "write_failed"
    assert {s.entity_id: (s.state, dict(s.attributes)) for s in hass.states.async_all()} == before
    assert server.posted == []  # type: ignore[attr-defined]


# --- sensors update at once -------------------------------------------------------------------


async def test_a_bottle_updates_the_last_feed_sensor_without_a_poll(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer
) -> None:
    assert hass.states.get(LAST_FEED).state == "unknown"

    await call(
        hass,
        "log_bottle",
        kind="pumped",
        amount=3,
        unit="fl. oz.",
        time="2026-10-26T09:15:00+00:00",
        note="Left breast",
    )

    state = hass.states.get(LAST_FEED)
    assert state.state == "2026-10-26T09:15:00+00:00"
    assert state.attributes["kind"] == "pumped"
    assert state.attributes["amount"] == 3
    assert state.attributes["unit"] == "fl. oz."
    assert state.attributes["note"] == "Left breast"


async def test_nursing_updates_the_last_feed_sensor_without_a_poll(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer
) -> None:
    await call(
        hass,
        "log_nursing",
        left_minutes=8,
        right_minutes=6,
        finish_side="right",
        end_time="2026-10-26T10:00:00+00:00",
    )

    state = hass.states.get(LAST_FEED)
    assert state.state == "2026-10-26T09:46:00+00:00"
    assert state.attributes["kind"] == "nursing"
    assert state.attributes["left_duration_minutes"] == 8
    assert state.attributes["right_duration_minutes"] == 6
    assert state.attributes["finish_side"] == "right"


async def test_a_pump_updates_the_last_pump_sensor_without_a_poll(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer
) -> None:
    await call(
        hass,
        "log_pump",
        left_amount=2,
        right_amount=3,
        unit="fl. oz.",
        left_minutes=10,
        right_minutes=12,
        time="2026-10-26T08:00:00+00:00",
    )

    state = hass.states.get(LAST_PUMP)
    assert state.state == "2026-10-26T08:00:00+00:00"
    assert state.attributes["amount"] == 5
    assert state.attributes["left_amount"] == 2
    assert state.attributes["right_amount"] == 3
    assert state.attributes["left_duration_minutes"] == 10


async def test_a_sleep_updates_the_last_sleep_sensor_and_leaves_the_baby_awake(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer
) -> None:
    assert hass.states.get(ASLEEP).state == "off"

    await call(
        hass, "log_sleep", start="2026-10-26T01:00:00+00:00", end="2026-10-26T02:30:00+00:00"
    )

    state = hass.states.get(LAST_SLEEP)
    assert state.state == "2026-10-26T01:00:00+00:00"
    assert state.attributes["duration_minutes"] == 90
    assert hass.states.get(ASLEEP).state == "off"


async def test_a_bottle_bumps_todays_feeds_and_bottle_volume_with_no_poll(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer, freezer: Any
) -> None:
    # Metric display: the sensor still shows today's volume in mL, converted from the ounces.
    hass.config.units = METRIC_SYSTEM
    await hass.config.async_set_time_zone("America/New_York")
    freezer.move_to(NOW)
    assert hass.states.get(FEEDS_TODAY).state == "0"
    assert float(hass.states.get(BOTTLE_TODAY).state) == 0

    await call(hass, "log_bottle", kind="formula", amount=4, unit="fl. oz.")
    assert hass.states.get(FEEDS_TODAY).state == "1"
    state = hass.states.get(BOTTLE_TODAY)
    assert state.attributes["unit_of_measurement"] == "mL"
    assert float(state.state) == pytest.approx(4 * 29.5735, abs=0.1)

    await call(hass, "log_bottle", kind="pumped", amount=2, unit="fl. oz.")
    assert hass.states.get(FEEDS_TODAY).state == "2"
    assert float(hass.states.get(BOTTLE_TODAY).state) == pytest.approx(6 * 29.5735, abs=0.1)
    assert hass.states.get("sensor.baby_two_feeds_today").state == "0"

    await undo(hass, baby=device_id(hass, BABY_ONE_ID))
    assert hass.states.get(FEEDS_TODAY).state == "1"
    assert float(hass.states.get(BOTTLE_TODAY).state) == pytest.approx(4 * 29.5735, abs=0.1)


async def test_nursing_counts_as_a_feed_today_and_adds_no_bottle_volume(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer, freezer: Any
) -> None:
    await hass.config.async_set_time_zone("America/New_York")
    freezer.move_to(NOW)

    await call(hass, "log_nursing", left_minutes=10)

    assert hass.states.get(FEEDS_TODAY).state == "1"
    assert float(hass.states.get(BOTTLE_TODAY).state) == 0


async def test_a_pump_and_a_sleep_are_not_feeds_today(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer, freezer: Any
) -> None:
    await hass.config.async_set_time_zone("America/New_York")
    freezer.move_to(NOW)

    await call(hass, "log_pump", left_amount=3, unit="fl. oz.", time="2026-10-26T14:00:00+00:00")
    await call(
        hass, "log_sleep", start="2026-10-26T12:00:00+00:00", end="2026-10-26T13:00:00+00:00"
    )

    assert hass.states.get(FEEDS_TODAY).state == "0"
    assert float(hass.states.get(BOTTLE_TODAY).state) == 0


# --- undo ------------------------------------------------------------------------------------


async def test_undo_removes_a_bottle_and_the_sensor_goes_back(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer
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

    await undo(hass, baby=device_id(hass, BABY_ONE_ID))

    assert hass.states.get(LAST_FEED).state == "unknown"
    [(_, op_add, added), (number, op_delete, deleted)] = posted(server)
    assert (op_add, op_delete, number) == (0, 2, 2)
    assert deleted.object_id == added.object_id


@pytest.mark.parametrize("name", ACTIONS)
async def test_undo_removes_a_record_of_every_new_type(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer, name: str
) -> None:
    await run_valid(hass, name)
    added = only_record(server)

    await undo(hass, config_entry_id=setup_entry.entry_id)

    [_, (_, op, deleted)] = posted(server)
    assert op == 2
    assert deleted.object_id == added.object_id
    assert deleted.record_type == added.record_type


async def test_undo_by_record_id_removes_a_bottle_among_other_writes(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer
) -> None:
    await run_valid(hass, "log_bottle", object_id=RETRY_ID)
    await run_valid(hass, "log_sleep")

    await undo(hass, baby=device_id(hass, BABY_ONE_ID), object_id=RETRY_ID)

    [_, _, (_, op, deleted)] = posted(server)
    assert (op, deleted.object_id) == (2, RETRY_ID)


# --- the Bottle button ------------------------------------------------------------------------


async def test_each_baby_gets_a_bottle_button(
    hass: HomeAssistant, setup_entry: MockConfigEntry
) -> None:
    devices = dr.async_get(hass)
    entities = er.async_get(hass)
    for baby_id in (BABY_ONE_ID, BABY_TWO_ID):
        device = devices.async_get_device(identifiers={(DOMAIN, baby_id)})
        assert device is not None
        bottle = [
            e
            for e in er.async_entries_for_device(entities, device.id)
            if e.domain == "button" and e.translation_key == "bottle"
        ]
        assert len(bottle) == 1
        assert bottle[0].unique_id == f"{baby_id}_bottle"
    assert hass.states.get(BOTTLE_BUTTON).attributes["friendly_name"] == "Baby One Bottle"
    assert hass.states.get("button.baby_two_bottle") is not None


async def test_a_bottle_press_with_a_4_oz_formula_option_logs_a_4_oz_formula_bottle(
    hass: HomeAssistant,
    setup_entry: MockConfigEntry,
    server: FakeServer,
    freezer: Any,
) -> None:
    freezer.move_to(NOW)
    hass.config_entries.async_update_entry(
        setup_entry,
        options={CONF_BOTTLE_SIZE: 4, CONF_BOTTLE_UNIT: "fl. oz.", CONF_BOTTLE_TYPE: "formula"},
    )

    await press(hass, BOTTLE_BUTTON)

    [(number, op, record)] = posted(server)
    assert (number, op) == (1, 0)
    assert isinstance(record, Formula)
    assert record.volume == Volume(4, "oz")
    assert record.time == NOW
    assert record.baby.object_id == BABY_ONE_ID
    assert record.to_payload()["amount"]["englishMeasure"] == "true"


@pytest.mark.parametrize(
    ("units", "volume"),
    [(US_CUSTOMARY_SYSTEM, Volume(4, "oz")), (METRIC_SYSTEM, Volume(4, "oz"))],
    ids=["us-customary", "metric"],
)
async def test_a_bottle_press_with_no_options_logs_a_4_oz_formula_bottle_in_any_unit_system(
    hass: HomeAssistant,
    setup_entry: MockConfigEntry,
    server: FakeServer,
    units: Any,
    volume: Volume,
) -> None:
    hass.config.units = units

    await press(hass, BOTTLE_BUTTON)

    record = only_record(server)
    assert isinstance(record, Formula)
    assert record.volume == volume


async def test_a_bottle_press_uses_the_options_as_they_are_after_a_change(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer
) -> None:
    await press(hass, BOTTLE_BUTTON)
    await set_options(
        hass,
        setup_entry,
        **{CONF_BOTTLE_SIZE: 5, CONF_BOTTLE_UNIT: "fl. oz.", CONF_BOTTLE_TYPE: "pumped"},
    )
    await press(hass, BOTTLE_BUTTON)
    await set_options(
        hass,
        setup_entry,
        **{CONF_BOTTLE_SIZE: 6, CONF_BOTTLE_UNIT: "fl. oz.", CONF_BOTTLE_TYPE: "formula"},
    )
    await press(hass, BOTTLE_BUTTON)

    [(_, _, first), (_, _, second), (_, _, third)] = posted(server)
    assert isinstance(first, Formula)
    assert (type(second), second.volume) == (Pumped, Volume(5, "oz"))
    assert (type(third), third.volume) == (Formula, Volume(6, "oz"))


async def test_changing_the_bottle_options_does_not_reload_the_entry(
    hass: HomeAssistant, setup_entry: MockConfigEntry
) -> None:
    coordinator = setup_entry.runtime_data

    await set_options(
        hass,
        setup_entry,
        **{CONF_BOTTLE_SIZE: 5, CONF_BOTTLE_UNIT: "fl. oz.", CONF_BOTTLE_TYPE: "pumped"},
    )

    assert setup_entry.runtime_data is coordinator
    assert setup_entry.options[CONF_BOTTLE_SIZE] == 5


async def test_a_bottle_press_is_for_its_own_baby_and_counts_in_todays_totals(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer, freezer: Any
) -> None:
    await hass.config.async_set_time_zone("America/New_York")
    hass.config.units = METRIC_SYSTEM
    freezer.move_to(NOW)

    await press(hass, "button.baby_two_bottle")

    assert only_record(server).baby.object_id == BABY_TWO_ID
    assert hass.states.get("sensor.baby_two_feeds_today").state == "1"
    assert hass.states.get(FEEDS_TODAY).state == "0"
    assert float(hass.states.get("sensor.baby_two_bottle_volume_today").state) == pytest.approx(
        4 * 29.5735, abs=0.1
    )


async def test_a_bottle_press_with_bad_saved_options_falls_back_to_the_defaults(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer
) -> None:
    hass.config.units = METRIC_SYSTEM
    hass.config_entries.async_update_entry(
        setup_entry,
        options={CONF_BOTTLE_SIZE: -3, CONF_BOTTLE_UNIT: "cups", CONF_BOTTLE_TYPE: "milk"},
    )

    await press(hass, BOTTLE_BUTTON)

    record = only_record(server)
    assert isinstance(record, Formula)
    assert record.volume == Volume(4, "oz")


async def test_a_bottle_press_with_milliliter_options_is_refused_with_a_translated_error(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer
) -> None:
    await set_options(
        hass,
        setup_entry,
        **{CONF_BOTTLE_SIZE: 120, CONF_BOTTLE_UNIT: "mL", CONF_BOTTLE_TYPE: "formula"},
    )
    sent = len(server.calls)  # type: ignore[attr-defined]

    with pytest.raises(ServiceValidationError) as raised:
        await press(hass, BOTTLE_BUTTON)

    assert raised.value.translation_key == "metric_not_supported"
    assert server.posted == []  # type: ignore[attr-defined]
    assert len(server.calls) == sent  # type: ignore[attr-defined]


async def test_a_failed_bottle_press_raises_a_translated_error_and_changes_no_sensor(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer
) -> None:
    before = hass.states.get(LAST_FEED)
    server.post_errors.append((BabyTrackerConnectionError("the service answered 500"), False))  # type: ignore[attr-defined]

    with pytest.raises(HomeAssistantError) as raised:
        await press(hass, BOTTLE_BUTTON)

    assert raised.value.translation_key == "write_failed"
    assert hass.states.get(LAST_FEED) == before


async def test_two_bottle_presses_make_two_records(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer
) -> None:
    await press(hass, BOTTLE_BUTTON)
    await press(hass, BOTTLE_BUTTON)

    records = [record for _, _, record in posted(server)]
    assert len({record.object_id for record in records}) == 2
    assert [n for n, _, _ in posted(server)] == [1, 2]


# --- the options screen ------------------------------------------------------------------------


def schema_defaults(result: Any) -> dict[str, Any]:
    return {str(key): key.default() for key in result["data_schema"].schema}


@pytest.mark.parametrize(
    ("units", "expected"),
    [
        (US_CUSTOMARY_SYSTEM, (4, "fl. oz.", "formula")),
        (METRIC_SYSTEM, (4, "fl. oz.", "formula")),
    ],
    ids=["us-customary", "metric"],
)
async def test_options_default_the_bottle_to_4_oz_of_formula_in_any_unit_system(
    hass: HomeAssistant, setup_entry: MockConfigEntry, units: Any, expected: tuple[Any, ...]
) -> None:
    hass.config.units = units

    result = await hass.config_entries.options.async_init(setup_entry.entry_id)

    defaults = schema_defaults(result)
    assert (
        defaults[CONF_BOTTLE_SIZE],
        defaults[CONF_BOTTLE_UNIT],
        defaults[CONF_BOTTLE_TYPE],
    ) == expected
    assert defaults[CONF_SCAN_INTERVAL] == 5


async def test_options_show_the_saved_bottle(
    hass: HomeAssistant, setup_entry: MockConfigEntry
) -> None:
    hass.config_entries.async_update_entry(
        setup_entry,
        options={CONF_BOTTLE_SIZE: 90, CONF_BOTTLE_UNIT: "mL", CONF_BOTTLE_TYPE: "pumped"},
    )

    result = await hass.config_entries.options.async_init(setup_entry.entry_id)

    defaults = schema_defaults(result)
    assert (defaults[CONF_BOTTLE_SIZE], defaults[CONF_BOTTLE_UNIT]) == (90, "mL")
    assert defaults[CONF_BOTTLE_TYPE] == "pumped"


async def test_options_save_the_bottle_next_to_the_interval(
    hass: HomeAssistant, setup_entry: MockConfigEntry
) -> None:
    await set_options(
        hass,
        setup_entry,
        **{
            CONF_SCAN_INTERVAL: 10,
            CONF_BOTTLE_SIZE: 5.5,
            CONF_BOTTLE_UNIT: "fl. oz.",
            CONF_BOTTLE_TYPE: "pumped",
        },
    )

    assert setup_entry.options == {
        CONF_SCAN_INTERVAL: 10,
        CONF_BOTTLE_SIZE: 5.5,
        CONF_BOTTLE_UNIT: "fl. oz.",
        CONF_BOTTLE_TYPE: "pumped",
    }


async def test_saving_only_the_interval_still_saves_the_default_bottle(
    hass: HomeAssistant, setup_entry: MockConfigEntry
) -> None:
    hass.config.units = METRIC_SYSTEM

    await set_options(hass, setup_entry, **{CONF_SCAN_INTERVAL: 7})

    assert setup_entry.options[CONF_SCAN_INTERVAL] == 7
    assert setup_entry.options[CONF_BOTTLE_SIZE] == 4
    assert setup_entry.options[CONF_BOTTLE_UNIT] == "fl. oz."
    assert setup_entry.options[CONF_BOTTLE_TYPE] == "formula"


@pytest.mark.parametrize(
    "bad",
    [
        {CONF_BOTTLE_SIZE: 0},
        {CONF_BOTTLE_SIZE: -4},
        {CONF_BOTTLE_UNIT: "cups"},
        {CONF_BOTTLE_TYPE: "milk"},
    ],
    ids=["zero-size", "negative-size", "unknown-unit", "unknown-type"],
)
async def test_options_reject_a_bad_bottle(
    hass: HomeAssistant, setup_entry: MockConfigEntry, bad: dict[str, Any]
) -> None:
    good = {
        CONF_SCAN_INTERVAL: 5,
        CONF_BOTTLE_SIZE: 4,
        CONF_BOTTLE_UNIT: "fl. oz.",
        CONF_BOTTLE_TYPE: "formula",
    }
    result = await hass.config_entries.options.async_init(setup_entry.entry_id)
    with pytest.raises(InvalidData):
        await hass.config_entries.options.async_configure(result["flow_id"], {**good, **bad})


# --- two accounts ------------------------------------------------------------------------------


async def test_the_bottle_button_of_each_account_uses_that_accounts_options(
    hass: HomeAssistant,
    setup_entry: MockConfigEntry,
    second_entry: MockConfigEntry,
    server: FakeServer,
    second_server: FakeServer,
) -> None:
    await set_options(
        hass,
        setup_entry,
        **{CONF_BOTTLE_SIZE: 7, CONF_BOTTLE_UNIT: "fl. oz.", CONF_BOTTLE_TYPE: "pumped"},
    )

    await press(hass, BOTTLE_BUTTON)
    await press(hass, "button.baby_three_bottle")

    first = only_record(server)
    second = only_record(second_server)
    assert (type(first), first.volume) == (Pumped, Volume(7, "oz"))
    assert isinstance(second, Formula)
    assert second.volume != Volume(7, "oz")


# --- metadata ----------------------------------------------------------------------------------

INTEGRATION = Path(__file__).resolve().parent.parent / "custom_components" / "nighp_baby_tracker"


def test_services_yaml_describes_the_new_actions_and_their_fields() -> None:
    import yaml

    services = yaml.safe_load((INTEGRATION / "services.yaml").read_text())
    assert set(services) == {
        "log_diaper",
        "undo",
        "log_bottle",
        "log_nursing",
        "log_sleep",
        "log_pump",
    }
    fields = {name: set(spec["fields"]) for name, spec in services.items()}
    assert fields["log_bottle"] == {"baby", "kind", "amount", "unit", "time", "note", "object_id"}
    assert fields["log_nursing"] == {
        "baby",
        "left_minutes",
        "right_minutes",
        "finish_side",
        "end_time",
        "note",
        "object_id",
    }
    assert fields["log_sleep"] == {"baby", "start", "end", "note", "object_id"}
    assert fields["log_pump"] == {
        "baby",
        "left_amount",
        "right_amount",
        "unit",
        "left_minutes",
        "right_minutes",
        "finish_side",
        "time",
        "note",
        "object_id",
    }
    bottle = services["log_bottle"]["fields"]
    assert bottle["kind"]["required"] is True
    assert bottle["amount"]["required"] is True
    assert bottle["unit"]["selector"]["select"]["options"] == ["fl. oz.", "mL"]
    for action in ("log_bottle", "log_nursing", "log_sleep", "log_pump"):
        assert services[action]["fields"]["baby"]["selector"]["device"]["filter"] == [
            {"integration": DOMAIN}
        ]


def test_the_new_actions_errors_options_and_button_are_translated() -> None:
    strings = json.loads((INTEGRATION / "strings.json").read_text())
    assert {
        "invalid_amount",
        "end_before_start",
        "no_duration",
        "no_amount",
        "metric_not_supported",
    } <= set(strings["exceptions"])
    assert "bottle" in strings["entity"]["button"]
    options = strings["options"]["step"]["init"]
    for key in (CONF_BOTTLE_SIZE, CONF_BOTTLE_UNIT, CONF_BOTTLE_TYPE):
        assert options["data"][key] and options["data_description"][key]
    assert "metric isn't supported yet" in options["data_description"][CONF_BOTTLE_UNIT].lower()
    assert {"formula", "pumped"} <= set(strings["selector"]["bottle_type"]["options"])
    assert strings == json.loads((INTEGRATION / "translations" / "en.json").read_text())
