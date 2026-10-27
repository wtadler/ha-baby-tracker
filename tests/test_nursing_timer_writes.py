"""Core PR 20: nursing timers driven through the side switches, and the record they write."""

import asyncio
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.storage import Store
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.nighp_baby_tracker.pybabytracker import (
    OP_ADD,
    OP_DELETE,
    BabyTrackerConnectionError,
    Nursing,
)

from .factories import (
    BABY_ONE_ID,
)
from .fake_server import FakeServer
from .helpers import (
    ASLEEP,
    NOW,
    NURSE_LEFT,
    NURSING_TIMER,
    device_id,
    minutes,
    nurse,
    nursing_posts,
    phone_sleep,
    posted,
    press,
    state,
    sync,
    toggle,
    undo,
)

TWO_NURSING_TIMER = "sensor.baby_two_nursing_timer"


@pytest.fixture(autouse=True)
def _start_at_now(freezer: Any) -> None:
    """Freeze the clock at NOW before anything is set up.

    Timers scheduled at setup then share the clock the tests move, so moving it forward and
    ticking reaches them the way it does in core's tests.
    """
    freezer.move_to(NOW)


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
    await toggle(hass, "switch.baby_two_nursing_right")
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
        await toggle(hass, NURSE_LEFT)
    assert state(hass, NURSING_TIMER).state == NOW.isoformat()
    await toggle(hass, NURSE_LEFT)

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
        await toggle(hass, NURSE_LEFT)
    except HomeAssistantError:
        await toggle(hass, NURSE_LEFT)

    assert len(nursing_posts(server)) == 1
    assert state(hass, NURSING_TIMER).state == "unknown"


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
    stop = hass.async_create_task(toggle(hass, NURSE_LEFT))
    for _ in range(5):
        await asyncio.sleep(0)
    freezer.move_to(minutes(8))
    gate.set()
    await busy
    await stop

    [record] = nursing_posts(server)
    assert record.left_duration == 8


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
        await toggle(hass, NURSE_LEFT)

    assert err.value.translation_key == "nursing_not_saved"
    assert state(hass, NURSING_TIMER).state == NOW.isoformat()


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
