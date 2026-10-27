# ruff: noqa: F811
"""Live sleep and nursing timers: buttons, actions, timer sensors, and polling.

A sleep timer syncs while running (an add with duration 0, then an update at the stop), so Home
Assistant reads and ends sleeps from any device. A nursing timer doesn't sync until it stops, so
Home Assistant keeps its own and writes one finished record at the stop.
"""

import asyncio
from datetime import datetime, timedelta
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.nighp_baby_tracker.const import DOMAIN
from custom_components.nighp_baby_tracker.pybabytracker import (
    OP_ADD,
    OP_DELETE,
    OP_UPDATE,
    BabyTrackerConnectionError,
    Nursing,
    Sleep,
)

from . import BABY_ONE, BABY_TWO
from .factories import BABY_ONE_ID, BABY_TWO_ID, DEVICE_A, record_payload
from .fake_server import FakeServer
from .helpers import NOW, device_id, get_baby_device, posted, press, sync_interval, tick, undo

PHONE_SLEEP = "5EE90000-0000-4000-8000-000000000001"
PHONE_SLEEP_TWO = "5EE90000-0000-4000-8000-000000000002"

SLEEP_BUTTON = "button.baby_one_sleep"
NURSE_LEFT = "button.baby_one_nurse_left"
NURSE_RIGHT = "button.baby_one_nurse_right"
SLEEP_TIMER = "sensor.baby_one_sleep_timer"
NURSING_TIMER = "sensor.baby_one_nursing_timer"
ASLEEP = "binary_sensor.baby_one_asleep"
LAST_SLEEP = "sensor.baby_one_last_sleep"
TWO_SLEEP_TIMER = "sensor.baby_two_sleep_timer"
TWO_NURSING_TIMER = "sensor.baby_two_nursing_timer"
TWO_ASLEEP = "binary_sensor.baby_two_asleep"


# Helpers


@pytest.fixture(autouse=True)
def _start_at_now(freezer: Any) -> None:
    """Freeze the clock at NOW before anything is set up.

    Timers scheduled at setup then share the clock the tests move, so moving it forward and
    ticking reaches them the way it does in core's tests.
    """
    freezer.move_to(NOW)


def minutes(n: float) -> datetime:
    return NOW + timedelta(minutes=n)


def phone_sleep(
    server: FakeServer,
    object_id: str = PHONE_SLEEP,
    *,
    start: datetime,
    duration: int = 0,
    baby: dict[str, Any] | None = None,
) -> None:
    kwargs: dict[str, Any] = {"baby": baby} if baby is not None else {}
    server.add_record(DEVICE_A, "Sleep", object_id, when=start, duration=duration, **kwargs)


def phone_ends_sleep(
    server: FakeServer, object_id: str, *, start: datetime, duration: int, changed: datetime
) -> None:
    payload = record_payload(
        "Sleep", object_id, when=start, baby=BABY_ONE, changed=changed, duration=duration
    )
    payload["newFlage"] = "false"
    server.add_payload(DEVICE_A, payload, op=OP_UPDATE)


async def sync(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Let the clock reach the next scheduled sync."""
    await tick(hass)


async def call(hass: HomeAssistant, action: str, baby_id: str = BABY_ONE_ID, **data: Any) -> None:
    await hass.services.async_call(
        DOMAIN, action, {"device_id": device_id(hass, baby_id), **data}, blocking=True
    )


def state(hass: HomeAssistant, entity_id: str) -> Any:
    found = hass.states.get(entity_id)
    assert found is not None, f"{entity_id} doesn't exist"
    return found


def nursing_posts(server: FakeServer) -> list[Nursing]:
    return [r for _, op, r in posted(server) if op == OP_ADD and isinstance(r, Nursing)]


async def nurse(hass: HomeAssistant, freezer: Any, *steps: tuple[float, str]) -> None:
    """Press nurse buttons at the given minutes after NOW: (minute, "left" | "right")."""
    for minute, side in steps:
        freezer.move_to(minutes(minute))
        await press(hass, f"button.baby_one_nurse_{side}")


# Entities


async def test_each_baby_gets_sleep_and_nurse_buttons_and_two_timer_sensors(
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
            if e.domain in ("button", "sensor")
        }
        assert {"sleep", "nurse_left", "nurse_right", "sleep_timer", "nursing_timer"} <= keys
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


# Sleep: the button


async def test_a_sleep_press_with_no_running_sleep_starts_one_now(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, freezer: Any
) -> None:
    await hass.config.async_set_time_zone("America/New_York")
    freezer.move_to(NOW)

    await press(hass, SLEEP_BUTTON)

    [(number, op, record)] = posted(server)
    assert (number, op) == (1, OP_ADD)
    assert isinstance(record, Sleep)
    assert record.duration == 0
    assert record.time == NOW
    assert record.timezone == -14400
    assert record.baby.object_id == BABY_ONE_ID
    assert state(hass, ASLEEP).state == "on"
    assert state(hass, SLEEP_TIMER).state == NOW.isoformat()


async def test_a_sleep_press_while_a_phone_started_sleep_runs_ends_it_now(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, freezer: Any
) -> None:
    freezer.move_to(NOW)
    phone_sleep(server, start=minutes(-40))
    await sync(hass, init_integration)

    await press(hass, SLEEP_BUTTON)

    [(number, op, record)] = posted(server)
    assert (number, op) == (1, OP_UPDATE)
    assert record.object_id == PHONE_SLEEP
    assert record.duration == 40
    assert record.new_flag == "false"
    assert record.timestamp == NOW
    assert state(hass, ASLEEP).state == "off"
    assert state(hass, SLEEP_TIMER).state == "unknown"
    assert state(hass, LAST_SLEEP).attributes["duration_minutes"] == 40  # one sleep, now ended


async def test_a_second_sleep_press_ends_the_sleep_home_assistant_started(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, freezer: Any
) -> None:
    freezer.move_to(NOW)
    await press(hass, SLEEP_BUTTON)
    freezer.move_to(minutes(25))

    await press(hass, SLEEP_BUTTON)

    [(_, op1, started), (_, op2, ended)] = posted(server)
    assert (op1, op2) == (OP_ADD, OP_UPDATE)
    assert ended.object_id == started.object_id
    assert ended.duration == 25
    assert state(hass, ASLEEP).state == "off"


async def test_a_sleep_press_after_a_phone_already_ended_the_sleep_starts_nothing(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, freezer: Any
) -> None:
    """The press meant "stop": the sync before the write finds it stopped, so it's done."""
    freezer.move_to(NOW)
    phone_sleep(server, start=minutes(-40))
    await sync(hass, init_integration)
    phone_ends_sleep(server, PHONE_SLEEP, start=minutes(-40), duration=38, changed=minutes(-2))

    with pytest.raises(ServiceValidationError) as err:
        await press(hass, SLEEP_BUTTON)

    assert err.value.translation_key == "sleep_already_ended"
    assert posted(server) == []
    assert state(hass, ASLEEP).state == "off"


async def test_a_sleep_press_after_a_phone_already_started_a_sleep_starts_no_second_one(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, freezer: Any
) -> None:
    """The press meant "start": the sync before the write finds one running, so it's done."""
    freezer.move_to(NOW)
    phone_sleep(server, start=minutes(-1))  # not synced yet

    with pytest.raises(ServiceValidationError) as err:
        await press(hass, SLEEP_BUTTON)

    assert err.value.translation_key == "sleep_already_started"
    assert posted(server) == []
    assert state(hass, ASLEEP).state == "on"
    assert state(hass, SLEEP_TIMER).attributes["object_id"] == PHONE_SLEEP


async def test_a_failed_end_keeps_the_sleep_running_and_the_next_press_lands_once(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, freezer: Any
) -> None:
    freezer.move_to(NOW)
    phone_sleep(server, start=minutes(-40))
    await sync(hass, init_integration)
    server.post_errors.append((BabyTrackerConnectionError("down"), False))

    with pytest.raises(HomeAssistantError):
        await press(hass, SLEEP_BUTTON)
    assert state(hass, ASLEEP).state == "on"
    await press(hass, SLEEP_BUTTON)

    assert [(n, op) for n, op, _ in posted(server)] == [(1, OP_UPDATE)]
    assert state(hass, ASLEEP).state == "off"


async def test_a_phone_and_home_assistant_ending_one_sleep_leave_one_finished_sleep(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, freezer: Any
) -> None:
    freezer.move_to(NOW)
    phone_sleep(server, start=minutes(-40))
    await sync(hass, init_integration)
    # The phone's end lands after Home Assistant's pre-write sync, just before its post.
    server.before_post.append(
        lambda: phone_ends_sleep(
            server, PHONE_SLEEP, start=minutes(-40), duration=39, changed=minutes(1)
        )
    )

    await press(hass, SLEEP_BUTTON)
    await sync(hass, init_integration)

    assert state(hass, LAST_SLEEP).attributes["duration_minutes"] == 39  # the phone's end wins
    assert [(n, op) for n, op, _ in posted(server)] == [(1, OP_UPDATE)]
    assert state(hass, ASLEEP).state == "off"
    assert state(hass, SLEEP_TIMER).state == "unknown"


async def test_undo_of_a_sleep_home_assistant_started_deletes_it(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, freezer: Any
) -> None:
    freezer.move_to(NOW)
    await press(hass, SLEEP_BUTTON)

    await undo(hass, device_id=device_id(hass, BABY_ONE_ID))

    [(_, op1, started), (_, op2, deleted)] = posted(server)
    assert (op1, op2) == (OP_ADD, OP_DELETE)
    assert deleted.object_id == started.object_id
    assert state(hass, ASLEEP).state == "off"
    assert state(hass, SLEEP_TIMER).state == "unknown"


# Sleep: the actions


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


async def test_two_babies_sleep_timers_run_independently(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, freezer: Any
) -> None:
    freezer.move_to(NOW)
    await press(hass, SLEEP_BUTTON)
    assert state(hass, TWO_ASLEEP).state == "off"
    freezer.move_to(minutes(5))
    await press(hass, "button.baby_two_sleep")
    freezer.move_to(minutes(30))

    await press(hass, SLEEP_BUTTON)

    assert state(hass, ASLEEP).state == "off"
    assert state(hass, TWO_ASLEEP).state == "on"
    assert state(hass, TWO_SLEEP_TIMER).state == minutes(5).isoformat()
    [(_, _, one), (_, _, two), (_, op, ended)] = posted(server)
    assert ended.object_id == one.object_id
    assert op == OP_UPDATE
    assert two.baby.object_id == BABY_TWO_ID


# Nursing: buttons


async def test_nurse_left_then_right_then_right_writes_both_sides_and_finish_side_right(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, freezer: Any
) -> None:
    await nurse(hass, freezer, (0, "left"), (5, "right"), (12, "right"))

    [record] = nursing_posts(server)
    assert record.left_duration == 5
    assert record.right_duration == 7
    assert record.finish_side == 1
    assert record.time == NOW
    assert record.state == 5
    assert record.baby.object_id == BABY_ONE_ID
    assert state(hass, NURSING_TIMER).state == "unknown"


async def test_a_nursing_timer_posts_nothing_until_it_stops(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, freezer: Any
) -> None:
    await nurse(hass, freezer, (0, "left"), (5, "right"), (7, "left"))

    assert posted(server) == []
    assert state(hass, NURSING_TIMER).state == NOW.isoformat()


async def test_pressing_the_running_side_again_stops_with_only_that_side(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, freezer: Any
) -> None:
    await nurse(hass, freezer, (0, "right"), (9, "right"))

    [record] = nursing_posts(server)
    assert (record.left_duration, record.right_duration, record.finish_side) == (0, 9, 1)


async def test_switching_sides_several_times_adds_up_each_side(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, freezer: Any
) -> None:
    await nurse(hass, freezer, (0, "left"), (2, "right"), (5, "left"), (9, "right"), (10, "right"))

    [record] = nursing_posts(server)
    assert (record.left_duration, record.right_duration) == (6, 4)
    assert record.finish_side == 1
    assert record.time == NOW


async def test_ending_on_the_left_sets_finish_side_left(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, freezer: Any
) -> None:
    await nurse(hass, freezer, (0, "right"), (4, "left"), (6, "left"))

    [record] = nursing_posts(server)
    assert (record.left_duration, record.right_duration, record.finish_side) == (2, 4, 0)


async def test_the_nursing_timer_sensor_shows_start_side_and_seconds(
    hass: HomeAssistant, init_integration: MockConfigEntry, freezer: Any
) -> None:
    await nurse(hass, freezer, (0, "left"))
    timer = state(hass, NURSING_TIMER)
    assert timer.state == NOW.isoformat()
    assert timer.attributes["side"] == "left"
    assert timer.attributes["left_seconds"] == 0
    assert timer.attributes["right_seconds"] == 0
    assert timer.attributes["side_started"] == NOW.isoformat()

    await nurse(hass, freezer, (5, "right"))

    timer = state(hass, NURSING_TIMER)
    assert timer.state == NOW.isoformat()
    assert timer.attributes["side"] == "right"
    assert timer.attributes["left_seconds"] == 300
    assert timer.attributes["right_seconds"] == 0
    assert timer.attributes["side_started"] == minutes(5).isoformat()


async def test_stopping_under_30_seconds_writes_one_minute_on_that_side(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, freezer: Any
) -> None:
    await nurse(hass, freezer, (0, "left"), (0.25, "left"))

    [record] = nursing_posts(server)
    assert (record.left_duration, record.right_duration, record.finish_side) == (1, 0, 0)


async def test_each_side_rounds_to_whole_minutes_half_up(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, freezer: Any
) -> None:
    # Left 89 seconds (1 minute), right 90 seconds (2 minutes).
    await nurse(hass, freezer, (0, "left"), (89 / 60, "right"), (179 / 60, "right"))

    [record] = nursing_posts(server)
    assert (record.left_duration, record.right_duration) == (1, 2)


async def test_a_short_first_side_rounds_to_zero_but_still_counts_the_other(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, freezer: Any
) -> None:
    await nurse(hass, freezer, (0, "left"), (0.25, "right"), (10.25, "right"))

    [record] = nursing_posts(server)
    assert (record.left_duration, record.right_duration, record.finish_side) == (0, 10, 1)


async def test_a_forgotten_nursing_timer_clamps_each_side_to_240_minutes(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, freezer: Any
) -> None:
    await nurse(hass, freezer, (0, "left"), (300, "right"), (600, "right"))

    [record] = nursing_posts(server)
    assert (record.left_duration, record.right_duration) == (240, 240)


async def test_a_nurse_press_while_the_baby_is_asleep_is_allowed(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, freezer: Any
) -> None:
    freezer.move_to(NOW)
    phone_sleep(server, start=minutes(-40))
    await sync(hass, init_integration)

    await nurse(hass, freezer, (0, "left"), (8, "left"))

    [record] = nursing_posts(server)
    assert record.left_duration == 8
    assert [op for _, op, _ in posted(server)] == [OP_ADD]
    assert state(hass, ASLEEP).state == "on"


async def test_two_babies_nursing_timers_run_independently(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, freezer: Any
) -> None:
    await nurse(hass, freezer, (0, "left"))
    freezer.move_to(minutes(1))
    await press(hass, "button.baby_two_nurse_right")
    await nurse(hass, freezer, (6, "left"))

    [record] = nursing_posts(server)
    assert record.baby.object_id == BABY_ONE_ID
    assert record.left_duration == 6
    assert state(hass, NURSING_TIMER).state == "unknown"
    two = state(hass, TWO_NURSING_TIMER)
    assert two.state == minutes(1).isoformat()
    assert two.attributes["side"] == "right"


async def test_a_failed_nursing_write_keeps_the_timer_and_the_retry_lands_once(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, freezer: Any
) -> None:
    await nurse(hass, freezer, (0, "left"))
    server.post_errors.append((BabyTrackerConnectionError("down"), False))

    freezer.move_to(minutes(5))
    with pytest.raises(HomeAssistantError):
        await press(hass, NURSE_LEFT)
    assert state(hass, NURSING_TIMER).state == NOW.isoformat()
    await press(hass, NURSE_LEFT)

    assert server.calls.count("post") == 2  # the failed post, then the retry
    [(number, _, record)] = posted(server)
    assert number == 1
    assert isinstance(record, Nursing)
    assert record.left_duration == 5
    assert state(hass, NURSING_TIMER).state == "unknown"


async def test_a_nursing_write_that_landed_despite_an_error_is_not_written_twice(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, freezer: Any
) -> None:
    await nurse(hass, freezer, (0, "left"))
    # The post lands but the answer is lost, and the landing check fails too.
    server.post_errors.append((BabyTrackerConnectionError("lost"), True))

    freezer.move_to(minutes(5))
    try:
        await press(hass, NURSE_LEFT)
    except HomeAssistantError:
        await press(hass, NURSE_LEFT)

    assert len(nursing_posts(server)) == 1
    assert state(hass, NURSING_TIMER).state == "unknown"


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


# Nursing: the actions


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


# Nursing: surviving a restart


async def test_a_nursing_timer_survives_a_restart(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, freezer: Any
) -> None:
    await nurse(hass, freezer, (0, "left"), (4, "right"))

    await hass.config_entries.async_reload(init_integration.entry_id)
    await hass.async_block_till_done()
    assert init_integration.state is ConfigEntryState.LOADED
    timer = state(hass, NURSING_TIMER)
    assert timer.state == NOW.isoformat()
    assert timer.attributes["side"] == "right"
    assert timer.attributes["left_seconds"] == 240

    await nurse(hass, freezer, (10, "right"))  # the time across the restart counts

    [record] = nursing_posts(server)
    assert (record.left_duration, record.right_duration, record.finish_side) == (4, 6, 1)


async def test_a_corrupt_saved_nursing_timer_is_ignored(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    store: Store = Store(hass, 1, f"{DOMAIN}.{mock_config_entry.entry_id}.nursing")
    await store.async_save({BABY_ONE_ID: {"side": "middle"}})
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.LOADED
    assert state(hass, NURSING_TIMER).state == "unknown"


async def test_removing_the_entry_deletes_the_saved_nursing_timers(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    freezer: Any,
    hass_storage: dict[str, Any],
) -> None:
    await nurse(hass, freezer, (0, "left"))
    key = f"{DOMAIN}.{init_integration.entry_id}.nursing"
    await hass.async_block_till_done()
    assert key in hass_storage

    await hass.config_entries.async_remove(init_integration.entry_id)
    await hass.async_block_till_done()

    assert key not in hass_storage


# Polling


async def test_a_running_synced_sleep_polls_every_minute_and_then_returns_to_the_interval(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    assert await sync_interval(hass, server) == timedelta(minutes=5)
    start = dt_util.utcnow().replace(microsecond=0) - timedelta(minutes=10)
    phone_sleep(server, start=start)
    await tick(hass, minutes=5)  # the five-minute sync finds the running sleep
    assert await sync_interval(hass, server) == timedelta(minutes=1)

    phone_ends_sleep(server, PHONE_SLEEP, start=start, duration=11, changed=start)
    await tick(hass, minutes=1)
    assert state(hass, ASLEEP).state == "off"
    assert await sync_interval(hass, server) == timedelta(minutes=5)


async def test_a_nursing_timer_polls_every_minute_and_then_returns_to_the_interval(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, freezer: Any
) -> None:
    await nurse(hass, freezer, (0, "left"))
    assert await sync_interval(hass, server) == timedelta(minutes=1)

    await nurse(hass, freezer, (2, "left"))

    assert await sync_interval(hass, server) == timedelta(minutes=5)


async def test_a_sleep_home_assistant_starts_switches_polling_at_once(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, freezer: Any
) -> None:
    freezer.move_to(NOW)
    await press(hass, SLEEP_BUTTON)
    assert await sync_interval(hass, server) == timedelta(minutes=1)
    freezer.move_to(minutes(3))
    await press(hass, SLEEP_BUTTON)
    assert await sync_interval(hass, server) == timedelta(minutes=5)


async def test_a_second_account_polls_on_its_own_schedule(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    second_entry: MockConfigEntry,
    server: FakeServer,
    second_server: FakeServer,
    freezer: Any,
) -> None:
    await nurse(hass, freezer, (0, "left"))

    assert await sync_interval(hass, server) == timedelta(minutes=1)
    assert await sync_interval(hass, second_server) == timedelta(minutes=5)


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


# Round 1: nursing timers under the lock, orphaned timers, and a failed save


async def test_a_nursing_stop_waiting_for_the_lock_counts_the_time_until_it_gets_it(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, freezer: Any
) -> None:
    await nurse(hass, freezer, (0, "left"))
    freezer.move_to(minutes(5))
    gate = asyncio.Event()
    server.post_gate = gate  # another write holds its turn at the post
    busy = hass.async_create_task(press(hass, "button.baby_two_wet_diaper"))
    for _ in range(20):
        await asyncio.sleep(0)
    assert "post" in server.calls
    stop = hass.async_create_task(press(hass, NURSE_LEFT))
    for _ in range(5):
        await asyncio.sleep(0)
    freezer.move_to(minutes(8))
    gate.set()
    await busy
    await stop

    [record] = nursing_posts(server)
    assert record.left_duration == 8


async def test_a_switch_during_a_stops_write_waits_and_is_not_erased(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, freezer: Any
) -> None:
    await nurse(hass, freezer, (0, "left"))
    gate = asyncio.Event()
    server.post_gate = gate
    freezer.move_to(minutes(6))
    stop = hass.async_create_task(press(hass, NURSE_LEFT))
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


async def test_a_saved_nursing_timer_for_a_baby_no_longer_in_the_account_is_dropped(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    server: FakeServer,
    hass_storage: dict[str, Any],
) -> None:
    key = f"{DOMAIN}.{mock_config_entry.entry_id}.nursing"
    gone = "99999999-0000-4000-8000-000000000099"
    timer = {
        "object_id": "AB000000-0000-4000-8000-000000000001",
        "started": NOW.isoformat(),
        "side": "left",
        "side_started": NOW.isoformat(),
        "left_seconds": 0,
        "right_seconds": 0,
    }
    hass_storage[key] = {"version": 1, "minor_version": 1, "key": key, "data": {gone: timer}}
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.LOADED
    assert state(hass, NURSING_TIMER).state == "unknown"  # no timer runs for anyone
    assert gone not in hass_storage[key]["data"]
    assert await sync_interval(hass, server) == timedelta(minutes=5)


async def test_a_nursing_timer_that_cannot_be_saved_raises_but_still_runs(
    hass: HomeAssistant, init_integration: MockConfigEntry, freezer: Any
) -> None:
    freezer.move_to(NOW)
    real_save = Store.async_save

    async def save(self: Store[Any], data: Any) -> None:
        if self.key.endswith(".nursing"):
            raise OSError("disk full")
        await real_save(self, data)

    with (
        patch.object(Store, "async_save", save),
        pytest.raises(HomeAssistantError) as err,
    ):
        await press(hass, NURSE_LEFT)

    assert err.value.translation_key == "nursing_not_saved"
    assert state(hass, NURSING_TIMER).state == NOW.isoformat()
