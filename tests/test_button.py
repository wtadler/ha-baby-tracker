"""The buttons: what each baby has, and what pressing them logs."""

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import patch

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.storage import Store
from homeassistant.util.unit_system import METRIC_SYSTEM, US_CUSTOMARY_SYSTEM
from pytest_homeassistant_custom_component.common import MockConfigEntry, snapshot_platform
from syrupy.assertion import SnapshotAssertion

from custom_components.nighp_baby_tracker.const import (
    CONF_BOTTLE_SIZE,
    CONF_BOTTLE_TYPE,
    CONF_BOTTLE_UNIT,
    DOMAIN,
)
from custom_components.nighp_baby_tracker.pybabytracker import (
    OP_ADD,
    OP_UPDATE,
    AuthenticationError,
    BabyTrackerConnectionError,
    Diaper,
    Formula,
    NoResponseError,
    Nursing,
    Pumped,
    Sleep,
    Volume,
)

from . import OWN_DEVICE, setup_integration
from .factories import (
    BABY_ONE_ID,
    BABY_TWO_ID,
    DEVICE_B,
    at,
)
from .fake_server import FakeServer
from .helpers import (
    ASLEEP,
    FEEDS_TODAY,
    LAST_DIAPER,
    LAST_FEED,
    LAST_SLEEP,
    NOW,
    NURSE_LEFT,
    NURSING_TIMER,
    PHONE_SLEEP,
    SLEEP_BUTTON,
    SLEEP_TIMER,
    TWO_ASLEEP,
    TWO_SLEEP_TIMER,
    button,
    fail_to_write,
    gap_issue,
    get_baby_device,
    minutes,
    nurse,
    nursing_posts,
    only_record,
    phone_ends_sleep,
    phone_sleep,
    posted,
    press,
    reauth_flows,
    sensor_states,
    set_options,
    state,
    sync,
)

TWO_NURSING_TIMER = "sensor.baby_two_nursing_timer"


@pytest.fixture(autouse=True)
def _start_at_now(freezer: Any) -> None:
    """Freeze the clock at NOW before anything is set up.

    Timers scheduled at setup then share the clock the tests move, so moving it forward and
    ticking reaches them the way it does in core's tests.
    """
    freezer.move_to(NOW)


BOTTLE_BUTTON = "button.baby_one_bottle"


async def test_buttons(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    snapshot: SnapshotAssertion,
    freezer: FrozenDateTimeFactory,
    mock_config_entry: MockConfigEntry,
    populated_server: FakeServer,
) -> None:
    freezer.move_to(at(180))
    with patch("custom_components.nighp_baby_tracker.PLATFORMS", [Platform.BUTTON]):
        await setup_integration(hass, mock_config_entry)

    await snapshot_platform(hass, entity_registry, snapshot, mock_config_entry.entry_id)


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
    assert record.timezone == -18000
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


# Sleep: the actions


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


# Nursing timers under the lock, orphaned timers, and a failed save


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


# Buttons


async def test_each_baby_gets_wet_dirty_mixed_and_bottle_buttons(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    devices = dr.async_get(hass)
    entities = er.async_get(hass)
    for baby_id in (BABY_ONE_ID, BABY_TWO_ID):
        device = get_baby_device(devices, baby_id)
        assert device is not None
        buttons = [
            e for e in er.async_entries_for_device(entities, device.id) if e.domain == "button"
        ]
        assert {b.translation_key for b in buttons} == {
            "wet_diaper",
            "dirty_diaper",
            "mixed_diaper",
            "bottle",
            "sleep",
            "nurse_left",
            "nurse_right",
        }
        assert len({b.unique_id for b in buttons}) == 7
    assert hass.states.get(button("baby_one", "wet")) is not None
    assert hass.states.get(button("baby_two", "mixed")) is not None


@pytest.mark.parametrize(
    ("kind", "status"), [("wet", 0), ("dirty", 1), ("mixed", 2)], ids=["wet", "dirty", "mixed"]
)
async def test_a_button_press_adds_that_kind_of_diaper_for_that_baby_now(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    server: FakeServer,
    freezer: Any,
    kind: str,
    status: int,
) -> None:
    await hass.config.async_set_time_zone("America/New_York")
    freezer.move_to(NOW)

    await press(hass, button("baby_two", kind))

    [(number, op, record)] = posted(server)
    assert (number, op) == (1, OP_ADD)
    assert isinstance(record, Diaper)
    assert record.status == status
    assert record.baby.object_id == BABY_TWO_ID
    assert record.time == NOW
    assert record.timezone == -18000  # standard, though late October is daylight time
    assert not record.note
    assert server.logs[OWN_DEVICE][0]["SyncID"] == 1


async def test_two_presses_make_two_records_with_different_ids(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, freezer: Any
) -> None:
    freezer.move_to(NOW)
    await press(hass, button("baby_one", "wet"))
    await press(hass, button("baby_one", "wet"))

    records = [record for _, _, record in posted(server)]
    assert len({record.object_id for record in records}) == 2
    assert [n for n, _, _ in posted(server)] == [1, 2]


# Writes sync first and update the sensor at once


async def test_a_write_saves_what_it_synced_and_posted(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    server: FakeServer,
    hass_storage: dict[str, Any],
) -> None:
    server.add_record(
        DEVICE_B, "Diaper", "D0000000-0000-4000-8000-0000000000C1", when=at(30), status=1
    )

    await press(hass, button("baby_one", "wet"))

    saved = hass_storage[f"{DOMAIN}.{init_integration.entry_id}"]["data"]
    assert saved["last_seen"][DEVICE_B] == 1  # the sync before the post
    assert saved["last_seen"][OWN_DEVICE] == 1  # and the post itself
    assert [op for _, op, _ in posted(server)] == [OP_ADD]


async def test_a_failed_sync_before_a_write_posts_nothing(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    server.request_error = BabyTrackerConnectionError("down")
    before = sensor_states(hass)

    with pytest.raises(HomeAssistantError) as raised:
        await press(hass, button("baby_one", "wet"))

    assert raised.value.translation_key == "sync_failed"
    assert server.posted == []
    assert sensor_states(hass) == before


# Failed writes


async def test_a_failed_write_raises_a_translated_error_and_changes_no_sensor(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    before = sensor_states(hass)
    server.post_errors.append((BabyTrackerConnectionError("HTTP 500"), False))

    with pytest.raises(HomeAssistantError) as raised:
        await press(hass, button("baby_one", "wet"))

    assert type(raised.value) is HomeAssistantError
    assert raised.value.translation_domain == DOMAIN
    assert raised.value.translation_key == "write_failed"
    assert sensor_states(hass) == before


async def test_a_press_that_got_no_answer_but_landed_succeeds(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    server.post_errors.append((NoResponseError("timed out"), True))

    await press(hass, button("baby_one", "dirty"))

    assert len(posted(server)) == 1
    assert hass.states.get(LAST_DIAPER).attributes["type"] == "dirty"


async def test_a_write_right_after_an_unanswered_one_says_it_is_still_being_confirmed(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, freezer: Any
) -> None:
    freezer.move_to(NOW)
    server.post_errors.append((NoResponseError("timed out"), False))
    with pytest.raises(HomeAssistantError) as first:
        await press(hass, button("baby_one", "wet"))
    assert first.value.translation_key == "write_failed"

    with pytest.raises(HomeAssistantError) as second:
        await press(hass, button("baby_one", "wet"))

    assert type(second.value) is HomeAssistantError
    assert second.value.translation_domain == DOMAIN
    assert second.value.translation_key == "write_outcome_unknown"
    assert server.posted == []  # nothing was resent


async def test_the_unknown_outcome_clears_once_the_grace_window_passes(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, freezer: Any
) -> None:
    freezer.move_to(NOW)
    server.post_errors.append((NoResponseError("timed out"), False))
    with pytest.raises(HomeAssistantError):
        await press(hass, button("baby_one", "wet"))

    freezer.move_to(datetime(2026, 10, 26, 15, 5, 0, tzinfo=UTC))
    await press(hass, button("baby_one", "wet"))

    assert [n for n, _, _ in posted(server)] == [1]  # the free number was reused


async def test_a_failed_write_still_saves_the_engine_state(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    server: FakeServer,
    hass_storage: dict[str, Any],
    freezer: Any,
) -> None:
    freezer.move_to(NOW)
    server.post_errors.append((NoResponseError("timed out"), False))
    with pytest.raises(HomeAssistantError):
        await press(hass, button("baby_one", "wet"))

    saved = hass_storage[f"{DOMAIN}.{init_integration.entry_id}"]["data"]
    assert saved["pending"]["sync_id"] == 1
    assert saved["pending"]["unknown_since"] is not None


async def test_a_successful_write_saves_the_new_record(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    server: FakeServer,
    hass_storage: dict[str, Any],
) -> None:
    await press(hass, button("baby_one", "wet"))

    saved = hass_storage[f"{DOMAIN}.{init_integration.entry_id}"]["data"]
    [(_, _, record)] = posted(server)
    assert saved["created"] == [record.object_id]
    assert saved["last_posted"] == 1
    assert saved["pending"] is None


async def test_a_gap_that_clears_before_the_hour_starts_the_clock_over(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, freezer: Any
) -> None:
    freezer.move_to(NOW)
    server.last_sync_override[OWN_DEVICE] = 5
    await fail_to_write(hass)
    freezer.tick(timedelta(minutes=30))
    server.last_sync_override.clear()
    await press(hass, button("baby_one", "wet"))  # the write works again

    freezer.tick(timedelta(minutes=45))  # 75 minutes after the first failure
    server.last_sync_override[OWN_DEVICE] = 6
    await fail_to_write(hass)
    freezer.tick(timedelta(minutes=45))  # 45 minutes after the second gap began
    await fail_to_write(hass)

    assert gap_issue(hass, init_integration) is None


async def test_a_second_gap_after_a_clean_stretch_can_still_become_a_repair(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, freezer: Any
) -> None:
    freezer.move_to(NOW)
    server.last_sync_override[OWN_DEVICE] = 5
    await fail_to_write(hass)
    server.last_sync_override.clear()
    await press(hass, button("baby_one", "wet"))

    freezer.tick(timedelta(hours=3))
    server.last_sync_override[OWN_DEVICE] = 9
    await fail_to_write(hass)
    freezer.tick(timedelta(hours=1, minutes=1))
    await fail_to_write(hass)

    assert gap_issue(hass, init_integration) is not None


async def test_each_account_times_its_own_gap(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    second_entry: MockConfigEntry,
    server: FakeServer,
    freezer: Any,
) -> None:
    freezer.move_to(NOW)
    server.last_sync_override[OWN_DEVICE] = 5
    await fail_to_write(hass)
    freezer.tick(timedelta(hours=1, minutes=30))

    # The first account's gap has lasted long enough, the second account's just began.
    await fail_to_write(hass)
    await press(hass, "button.baby_three_wet_diaper")

    assert gap_issue(hass, init_integration) is not None
    assert gap_issue(hass, second_entry) is None


async def test_the_repair_issue_goes_away_after_a_write_succeeds(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, freezer: Any
) -> None:
    freezer.move_to(NOW)
    server.last_sync_override[OWN_DEVICE] = 5
    await fail_to_write(hass)
    freezer.tick(timedelta(hours=2))
    await fail_to_write(hass)
    assert gap_issue(hass, init_integration) is not None
    server.last_sync_override.clear()

    await press(hass, button("baby_one", "wet"))

    assert gap_issue(hass, init_integration) is None


async def test_a_gap_in_the_own_log_still_saves_state(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    server.last_sync_override[OWN_DEVICE] = 5
    with (
        patch.object(Store, "async_save", autospec=True) as save,
        pytest.raises(HomeAssistantError),
    ):
        await press(hass, button("baby_one", "wet"))
    assert save.call_count == 1


@pytest.mark.parametrize("where", ["post", "sync"])
async def test_an_auth_error_during_a_write_starts_reauth(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, where: str
) -> None:
    if where == "post":
        server.post_errors.append((AuthenticationError("session rejected"), False))
    else:
        server.request_error = AuthenticationError("session rejected")

    with pytest.raises(HomeAssistantError) as raised:
        await press(hass, button("baby_one", "wet"))

    assert raised.value.translation_key == "auth_failed"
    assert reauth_flows(hass) == ["reauth"]
    assert server.posted == []


# Concurrency and several accounts


async def test_concurrent_presses_are_serialized(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    server.calls.clear()

    await asyncio.gather(
        press(hass, button("baby_one", "wet")),
        press(hass, button("baby_one", "dirty")),
        press(hass, button("baby_two", "mixed")),
    )

    assert [n for n, _, _ in posted(server)] == [1, 2, 3]
    assert server.max_active_posts == 1
    # Each write finishes (sync, prepare, post) before the next starts.
    assert server.calls == ["devices", "devices", "post"] * 3


async def test_buttons_for_each_account_post_to_their_own_account(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    second_entry: MockConfigEntry,
    server: FakeServer,
    second_server: FakeServer,
) -> None:
    await press(hass, button("baby_three", "dirty"))
    assert server.posted == []
    assert len(second_server.posted) == 1


# --- the Bottle button ------------------------------------------------------------------------


async def test_each_baby_gets_a_bottle_button(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    devices = dr.async_get(hass)
    entities = er.async_get(hass)
    for baby_id in (BABY_ONE_ID, BABY_TWO_ID):
        device = get_baby_device(devices, baby_id)
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
    init_integration: MockConfigEntry,
    server: FakeServer,
    freezer: Any,
) -> None:
    freezer.move_to(NOW)
    hass.config_entries.async_update_entry(
        init_integration,
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
    [(US_CUSTOMARY_SYSTEM, Volume(4, "oz")), (METRIC_SYSTEM, Volume(120, "ml"))],
    ids=["us-customary", "metric"],
)
async def test_a_bottle_press_with_no_options_logs_4_oz_in_us_customary_and_120_ml_in_metric(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
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
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    await press(hass, BOTTLE_BUTTON)
    await set_options(
        hass,
        init_integration,
        **{CONF_BOTTLE_SIZE: 5, CONF_BOTTLE_UNIT: "fl. oz.", CONF_BOTTLE_TYPE: "pumped"},
    )
    await press(hass, BOTTLE_BUTTON)
    await set_options(
        hass,
        init_integration,
        **{CONF_BOTTLE_SIZE: 6, CONF_BOTTLE_UNIT: "fl. oz.", CONF_BOTTLE_TYPE: "formula"},
    )
    await press(hass, BOTTLE_BUTTON)

    [(_, _, first), (_, _, second), (_, _, third)] = posted(server)
    assert isinstance(first, Formula)
    assert (type(second), second.volume) == (Pumped, Volume(5, "oz"))
    assert (type(third), third.volume) == (Formula, Volume(6, "oz"))


async def test_a_bottle_press_is_for_its_own_baby_and_counts_in_todays_totals(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, freezer: Any
) -> None:
    await hass.config.async_set_time_zone("America/New_York")
    hass.config.units = METRIC_SYSTEM
    freezer.move_to(NOW)

    await press(hass, "button.baby_two_bottle")

    assert only_record(server).baby.object_id == BABY_TWO_ID
    assert hass.states.get("sensor.baby_two_feeds_today").state == "1"
    assert hass.states.get(FEEDS_TODAY).state == "0"
    assert float(hass.states.get("sensor.baby_two_bottle_volume_today").state) == pytest.approx(
        120, abs=0.1
    )


async def test_a_bottle_press_with_bad_saved_options_falls_back_to_the_defaults(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    hass.config.units = METRIC_SYSTEM
    hass.config_entries.async_update_entry(
        init_integration,
        options={CONF_BOTTLE_SIZE: -3, CONF_BOTTLE_UNIT: "cups", CONF_BOTTLE_TYPE: "milk"},
    )

    await press(hass, BOTTLE_BUTTON)

    record = only_record(server)
    assert isinstance(record, Formula)
    assert record.volume == Volume(120, "ml")


async def test_a_bottle_press_with_milliliter_options_logs_a_metric_bottle(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    await set_options(
        hass,
        init_integration,
        **{CONF_BOTTLE_SIZE: 90, CONF_BOTTLE_UNIT: "mL", CONF_BOTTLE_TYPE: "pumped"},
    )

    await press(hass, BOTTLE_BUTTON)

    record = only_record(server)
    assert isinstance(record, Pumped)
    assert record.volume == Volume(90, "ml")
    assert record.to_payload()["amount"] == {
        "BCObjectType": "VolumeMeasure",
        "englishMeasure": "false",
        "value": 90,
    }


async def test_saved_ounce_options_stay_ounces_in_a_metric_home(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    hass.config.units = METRIC_SYSTEM
    await set_options(
        hass,
        init_integration,
        **{CONF_BOTTLE_SIZE: 4, CONF_BOTTLE_UNIT: "fl. oz.", CONF_BOTTLE_TYPE: "formula"},
    )

    await press(hass, BOTTLE_BUTTON)

    assert only_record(server).volume == Volume(4, "oz")


async def test_a_failed_bottle_press_raises_a_translated_error_and_changes_no_sensor(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    before = hass.states.get(LAST_FEED)
    server.post_errors.append((BabyTrackerConnectionError("the service answered 500"), False))

    with pytest.raises(HomeAssistantError) as raised:
        await press(hass, BOTTLE_BUTTON)

    assert raised.value.translation_key == "write_failed"
    assert hass.states.get(LAST_FEED) == before


async def test_two_bottle_presses_make_two_records(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    await press(hass, BOTTLE_BUTTON)
    await press(hass, BOTTLE_BUTTON)

    records = [record for _, _, record in posted(server)]
    assert len({record.object_id for record in records}) == 2
    assert [n for n, _, _ in posted(server)] == [1, 2]


# --- two accounts ------------------------------------------------------------------------------


async def test_the_bottle_button_of_each_account_uses_that_accounts_options(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    second_entry: MockConfigEntry,
    server: FakeServer,
    second_server: FakeServer,
) -> None:
    await set_options(
        hass,
        init_integration,
        **{CONF_BOTTLE_SIZE: 7, CONF_BOTTLE_UNIT: "fl. oz.", CONF_BOTTLE_TYPE: "pumped"},
    )

    await press(hass, BOTTLE_BUTTON)
    await press(hass, "button.baby_three_bottle")

    first = only_record(server)
    second = only_record(second_server)
    assert (type(first), first.volume) == (Pumped, Volume(7, "oz"))
    assert isinstance(second, Formula)
    assert second.volume != Volume(7, "oz")


@pytest.mark.parametrize(
    ("zone", "moment", "expected"),
    [
        ("America/New_York", datetime(2026, 7, 15, 16, 0, tzinfo=UTC), -18000),
        ("America/New_York", datetime(2026, 1, 15, 16, 0, tzinfo=UTC), -18000),
        ("America/Chicago", datetime(2026, 7, 15, 16, 0, tzinfo=UTC), -21600),
        ("Asia/Kolkata", datetime(2026, 7, 15, 16, 0, tzinfo=UTC), 19800),
        ("UTC", datetime(2026, 7, 15, 16, 0, tzinfo=UTC), 0),
        ("Australia/Sydney", datetime(2026, 1, 15, 16, 0, tzinfo=UTC), 36000),
    ],
    ids=["new-york-july", "new-york-january", "chicago-july", "no-dst", "utc", "southern-dst"],
)
async def test_a_press_sends_the_zones_standard_offset_year_round(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    server: FakeServer,
    freezer: Any,
    zone: str,
    moment: datetime,
    expected: int,
) -> None:
    await hass.config.async_set_time_zone(zone)
    freezer.move_to(moment)

    await press(hass, BOTTLE_BUTTON)

    assert only_record(server).timezone == expected


async def test_a_started_timer_sends_the_standard_offset_in_summer(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, freezer: Any
) -> None:
    await hass.config.async_set_time_zone("America/New_York")
    freezer.move_to(datetime(2026, 7, 15, 16, 0, tzinfo=UTC))

    await press(hass, SLEEP_BUTTON)

    assert only_record(server).timezone == -18000
