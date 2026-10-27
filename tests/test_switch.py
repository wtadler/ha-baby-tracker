"""The timer switches: Sleep, and Nursing left and Nursing right, one set per baby."""

from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.storage import Store
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
    snapshot_platform,
)
from syrupy.assertion import SnapshotAssertion

from custom_components.nighp_baby_tracker.const import DOMAIN
from custom_components.nighp_baby_tracker.pybabytracker import (
    OP_ADD,
    OP_UPDATE,
    BabyTrackerConnectionError,
    Sleep,
)

from . import setup_integration
from .factories import BABY_ONE_ID, BABY_TWO_ID
from .fake_server import FakeServer
from .helpers import (
    ASLEEP,
    DISCARD_NURSING,
    LAST_SLEEP,
    NOW,
    NURSE_LEFT,
    NURSE_RIGHT,
    NURSING_TIMER,
    PHONE_SLEEP,
    SLEEP_SWITCH,
    SLEEP_TIMER,
    TWO_ASLEEP,
    TWO_NURSE_LEFT,
    TWO_NURSE_RIGHT,
    TWO_SLEEP_SWITCH,
    TWO_SLEEP_TIMER,
    get_baby_device,
    minutes,
    nursing_posts,
    phone_ends_sleep,
    phone_sleep,
    posted,
    press,
    state,
    sync,
    toggle,
    turn_off,
    turn_on,
)

TWO_NURSING_TIMER = "sensor.baby_two_nursing_timer"


@pytest.fixture(autouse=True)
def _start_at_now(freezer: Any) -> None:
    """Freeze the clock at NOW before anything is set up."""
    freezer.move_to(NOW)


def _issue(hass: HomeAssistant, entry: MockConfigEntry, baby_id: str = BABY_ONE_ID) -> Any:
    return ir.async_get(hass).async_get_issue(
        DOMAIN, f"nursing_timer_long_{entry.entry_id}_{baby_id}"
    )


async def _expect_error(
    hass: HomeAssistant, entity_id: str, service: str, key: str, kind: type[Exception]
) -> None:
    with pytest.raises(kind) as err:
        await hass.services.async_call("switch", service, {"entity_id": entity_id}, blocking=True)
    assert getattr(err.value, "translation_key", None) == key
    assert getattr(err.value, "translation_domain", None) == DOMAIN


# Entities


async def test_switches(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    snapshot: SnapshotAssertion,
    mock_config_entry: MockConfigEntry,
) -> None:
    with patch("custom_components.nighp_baby_tracker.PLATFORMS", [Platform.SWITCH]):
        await setup_integration(hass, mock_config_entry)

    await snapshot_platform(hass, entity_registry, snapshot, mock_config_entry.entry_id)


async def test_each_baby_gets_a_sleep_switch_and_two_nursing_switches(
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
            if e.domain == "switch"
        }
        assert keys == {"sleep", "nursing_left", "nursing_right"}
    for entity_id in (SLEEP_SWITCH, NURSE_LEFT, NURSE_RIGHT, TWO_SLEEP_SWITCH, TWO_NURSE_LEFT):
        assert state(hass, entity_id).state == "off"
    assert state(hass, SLEEP_SWITCH).name == "Baby One Sleep"
    assert state(hass, NURSE_LEFT).name == "Baby One Nursing left"


# Sleep switch


async def test_turning_the_sleep_switch_on_starts_one_sleep_now(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    await hass.config.async_set_time_zone("America/New_York")

    await turn_on(hass, SLEEP_SWITCH)

    [(number, op, record)] = posted(server)
    assert (number, op) == (1, OP_ADD)
    assert isinstance(record, Sleep)
    assert record.duration == 0
    assert record.time == NOW
    assert record.timezone == -18000
    assert record.baby.object_id == BABY_ONE_ID
    assert state(hass, SLEEP_SWITCH).state == "on"
    assert state(hass, ASLEEP).state == "on"
    assert state(hass, SLEEP_TIMER).state == NOW.isoformat()


async def test_turning_the_sleep_switch_off_ends_the_sleep(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, freezer: Any
) -> None:
    await turn_on(hass, SLEEP_SWITCH)
    freezer.move_to(minutes(25))

    await turn_off(hass, SLEEP_SWITCH)

    [(_, op1, started), (_, op2, ended)] = posted(server)
    assert (op1, op2) == (OP_ADD, OP_UPDATE)
    assert ended.object_id == started.object_id
    assert ended.duration == 25
    assert ended.new_flag == "false"
    assert state(hass, SLEEP_SWITCH).state == "off"
    assert state(hass, ASLEEP).state == "off"
    assert state(hass, SLEEP_TIMER).state == "unknown"


async def test_a_sleep_started_on_a_phone_shows_the_switch_on_and_turning_it_off_ends_it(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    phone_sleep(server, start=minutes(-40))
    await sync(hass, init_integration)
    assert state(hass, SLEEP_SWITCH).state == "on"

    await turn_off(hass, SLEEP_SWITCH)

    [(number, op, record)] = posted(server)
    assert (number, op) == (1, OP_UPDATE)
    assert record.object_id == PHONE_SLEEP
    assert record.duration == 40
    assert record.new_flag == "false"
    assert record.timestamp == NOW
    assert state(hass, SLEEP_SWITCH).state == "off"
    assert state(hass, LAST_SLEEP).attributes["duration_minutes"] == 40


async def test_the_switch_follows_a_phone_that_ends_the_sleep(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    phone_sleep(server, start=minutes(-40))
    await sync(hass, init_integration)
    phone_ends_sleep(server, PHONE_SLEEP, start=minutes(-40), duration=38, changed=minutes(-2))

    await sync(hass, init_integration)

    assert state(hass, SLEEP_SWITCH).state == "off"


async def test_turning_the_sleep_switch_on_twice_is_the_start_sleep_error_and_posts_once(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    await turn_on(hass, SLEEP_SWITCH)

    await _expect_error(
        hass, SLEEP_SWITCH, "turn_on", "sleep_already_running", ServiceValidationError
    )

    assert [(n, op) for n, op, _ in posted(server)] == [(1, OP_ADD)]
    assert state(hass, SLEEP_SWITCH).state == "on"


async def test_turning_on_while_a_phone_sleep_is_not_yet_synced_starts_no_second_one(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    phone_sleep(server, start=minutes(-1))  # the write's own sync finds it

    await _expect_error(
        hass, SLEEP_SWITCH, "turn_on", "sleep_already_running", ServiceValidationError
    )

    assert posted(server) == []
    # The write's own sync found the sleep, so the switch shows it at once.
    assert state(hass, SLEEP_SWITCH).state == "on"
    assert state(hass, ASLEEP).state == "on"
    assert state(hass, SLEEP_TIMER).attributes["object_id"] == PHONE_SLEEP


async def test_turning_the_sleep_switch_off_when_no_sleep_runs_is_the_stop_sleep_error(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    await _expect_error(hass, SLEEP_SWITCH, "turn_off", "no_running_sleep", ServiceValidationError)

    assert posted(server) == []
    assert state(hass, SLEEP_SWITCH).state == "off"


async def test_turning_off_after_the_app_already_ended_the_sleep_posts_nothing(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    """The switch still reads on, but the sync before the write finds the sleep ended."""
    phone_sleep(server, start=minutes(-40))
    await sync(hass, init_integration)
    assert state(hass, SLEEP_SWITCH).state == "on"
    phone_ends_sleep(server, PHONE_SLEEP, start=minutes(-40), duration=38, changed=minutes(-2))

    await _expect_error(hass, SLEEP_SWITCH, "turn_off", "no_running_sleep", ServiceValidationError)

    assert posted(server) == []
    # The write's own sync found the sleep ended, so the switch shows it at once.
    assert state(hass, SLEEP_SWITCH).state == "off"
    assert state(hass, ASLEEP).state == "off"
    assert state(hass, SLEEP_TIMER).state == "unknown"
    assert state(hass, LAST_SLEEP).attributes["duration_minutes"] == 38


async def test_a_failed_end_keeps_the_switch_on_and_the_retry_lands_once(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    phone_sleep(server, start=minutes(-40))
    await sync(hass, init_integration)
    server.post_errors.append((BabyTrackerConnectionError("down"), False))

    with pytest.raises(HomeAssistantError):
        await turn_off(hass, SLEEP_SWITCH)
    assert state(hass, SLEEP_SWITCH).state == "on"
    await turn_off(hass, SLEEP_SWITCH)

    assert [(n, op) for n, op, _ in posted(server)] == [(1, OP_UPDATE)]
    assert state(hass, SLEEP_SWITCH).state == "off"


async def test_a_phone_and_home_assistant_ending_one_sleep_leave_one_finished_sleep(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    phone_sleep(server, start=minutes(-40))
    await sync(hass, init_integration)
    # The phone's end lands after Home Assistant's pre-write sync, just before its post.
    server.before_post.append(
        lambda: phone_ends_sleep(
            server, PHONE_SLEEP, start=minutes(-40), duration=39, changed=minutes(1)
        )
    )

    await turn_off(hass, SLEEP_SWITCH)
    await sync(hass, init_integration)

    assert state(hass, LAST_SLEEP).attributes["duration_minutes"] == 39  # the phone's end wins
    assert [(n, op) for n, op, _ in posted(server)] == [(1, OP_UPDATE)]
    assert state(hass, SLEEP_SWITCH).state == "off"


async def test_two_babies_sleep_switches_run_independently(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, freezer: Any
) -> None:
    await turn_on(hass, SLEEP_SWITCH)
    assert state(hass, TWO_SLEEP_SWITCH).state == "off"
    freezer.move_to(minutes(5))
    await turn_on(hass, TWO_SLEEP_SWITCH)
    freezer.move_to(minutes(30))

    await turn_off(hass, SLEEP_SWITCH)

    assert state(hass, SLEEP_SWITCH).state == "off"
    assert state(hass, TWO_SLEEP_SWITCH).state == "on"
    assert state(hass, TWO_ASLEEP).state == "on"
    assert state(hass, TWO_SLEEP_TIMER).state == minutes(5).isoformat()
    [(_, _, one), (_, _, two), (_, op, ended)] = posted(server)
    assert ended.object_id == one.object_id
    assert op == OP_UPDATE
    assert two.baby.object_id == BABY_TWO_ID


async def test_the_sleep_switch_and_the_start_and_stop_actions_agree(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, freezer: Any
) -> None:
    await hass.services.async_call(
        DOMAIN,
        "start_sleep",
        {"device_id": get_baby_device(dr.async_get(hass), BABY_ONE_ID).id},  # type: ignore[union-attr]
        blocking=True,
    )
    assert state(hass, SLEEP_SWITCH).state == "on"
    freezer.move_to(minutes(10))

    await turn_off(hass, SLEEP_SWITCH)

    assert [op for _, op, _ in posted(server)] == [OP_ADD, OP_UPDATE]


async def test_a_sync_failure_makes_the_switches_unavailable_and_writes_say_why(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    server.request_error = BabyTrackerConnectionError("down")

    await _expect_error(hass, SLEEP_SWITCH, "turn_on", "sync_failed", HomeAssistantError)

    assert server.posted == []
    server.request_error = None
    await sync(hass, init_integration)
    assert state(hass, SLEEP_SWITCH).state == "off"


async def test_the_switches_are_unavailable_while_a_sync_is_failing(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    server.request_error = BabyTrackerConnectionError("down")

    await sync(hass, init_integration)

    for entity_id in (SLEEP_SWITCH, NURSE_LEFT, NURSE_RIGHT):
        assert state(hass, entity_id).state == "unavailable"


# Nursing switches


async def test_turning_a_side_on_with_no_session_starts_a_timer_on_it_and_posts_nothing(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    await turn_on(hass, NURSE_LEFT)

    assert posted(server) == []
    assert state(hass, NURSE_LEFT).state == "on"
    assert state(hass, NURSE_RIGHT).state == "off"
    timer = state(hass, NURSING_TIMER)
    assert timer.state == NOW.isoformat()
    assert timer.attributes["side"] == "left"


async def test_turning_the_other_side_on_switches_sides_and_the_first_reads_off(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, freezer: Any
) -> None:
    await turn_on(hass, NURSE_LEFT)
    freezer.move_to(minutes(5))

    await turn_on(hass, NURSE_RIGHT)

    assert posted(server) == []
    assert state(hass, NURSE_LEFT).state == "off"
    assert state(hass, NURSE_RIGHT).state == "on"
    timer = state(hass, NURSING_TIMER)
    assert timer.state == NOW.isoformat()  # one session, still
    assert timer.attributes["side"] == "right"
    assert timer.attributes["left_seconds"] == 300


async def test_turning_off_the_running_side_stops_the_session_and_logs_both_sides(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, freezer: Any
) -> None:
    await turn_on(hass, NURSE_LEFT)
    freezer.move_to(minutes(5))
    await turn_on(hass, NURSE_RIGHT)
    freezer.move_to(minutes(12))

    await turn_off(hass, NURSE_RIGHT)

    [record] = nursing_posts(server)
    assert len(posted(server)) == 1
    assert (record.left_duration, record.right_duration, record.finish_side) == (5, 7, 1)
    assert record.time == NOW
    assert record.baby.object_id == BABY_ONE_ID
    assert state(hass, NURSE_LEFT).state == "off"
    assert state(hass, NURSE_RIGHT).state == "off"
    assert state(hass, NURSING_TIMER).state == "unknown"


async def test_turning_off_the_left_side_ends_on_the_left(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, freezer: Any
) -> None:
    await turn_on(hass, NURSE_RIGHT)
    freezer.move_to(minutes(4))
    await turn_on(hass, NURSE_LEFT)
    freezer.move_to(minutes(6))

    await turn_off(hass, NURSE_LEFT)

    [record] = nursing_posts(server)
    assert (record.left_duration, record.right_duration, record.finish_side) == (2, 4, 0)


async def test_turning_on_the_side_already_running_does_nothing(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, freezer: Any
) -> None:
    await turn_on(hass, NURSE_LEFT)
    before = state(hass, NURSING_TIMER)
    freezer.move_to(minutes(3))

    await turn_on(hass, NURSE_LEFT)

    assert posted(server) == []
    after = state(hass, NURSING_TIMER)
    assert after.attributes == before.attributes  # side_started is unchanged
    assert state(hass, NURSE_LEFT).state == "on"


async def test_turning_off_a_side_with_no_session_does_nothing(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    await turn_off(hass, NURSE_LEFT)
    await turn_off(hass, NURSE_RIGHT)

    assert posted(server) == []
    assert state(hass, NURSING_TIMER).state == "unknown"
    assert state(hass, NURSE_LEFT).state == "off"


async def test_turning_off_the_idle_side_leaves_the_running_session_alone(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, freezer: Any
) -> None:
    await turn_on(hass, NURSE_LEFT)
    freezer.move_to(minutes(3))

    await turn_off(hass, NURSE_RIGHT)

    assert posted(server) == []
    assert state(hass, NURSE_LEFT).state == "on"
    assert state(hass, NURSING_TIMER).attributes["side"] == "left"
    freezer.move_to(minutes(6))
    await turn_off(hass, NURSE_LEFT)
    [record] = nursing_posts(server)
    assert (record.left_duration, record.right_duration) == (6, 0)


async def test_the_two_nursing_switches_are_never_both_on(
    hass: HomeAssistant, init_integration: MockConfigEntry, freezer: Any
) -> None:
    for minute, entity_id in ((0, NURSE_LEFT), (1, NURSE_RIGHT), (2, NURSE_RIGHT), (3, NURSE_LEFT)):
        freezer.move_to(minutes(minute))
        await turn_on(hass, entity_id)
        states = {state(hass, NURSE_LEFT).state, state(hass, NURSE_RIGHT).state}
        assert states == {"on", "off"}
        assert state(hass, entity_id).state == "on"


async def test_a_session_can_start_again_after_a_stop(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, freezer: Any
) -> None:
    await toggle(hass, NURSE_LEFT)
    freezer.move_to(minutes(2))
    await toggle(hass, NURSE_LEFT)
    freezer.move_to(minutes(10))
    await toggle(hass, NURSE_RIGHT)
    freezer.move_to(minutes(13))
    await toggle(hass, NURSE_RIGHT)

    first, second = nursing_posts(server)
    assert first.object_id != second.object_id
    assert (first.left_duration, first.right_duration) == (2, 0)
    assert (second.left_duration, second.right_duration) == (0, 3)


async def test_two_babies_nursing_switches_run_independently(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, freezer: Any
) -> None:
    await turn_on(hass, NURSE_LEFT)
    freezer.move_to(minutes(1))
    await turn_on(hass, TWO_NURSE_RIGHT)
    freezer.move_to(minutes(6))

    await turn_off(hass, NURSE_LEFT)

    [record] = nursing_posts(server)
    assert record.baby.object_id == BABY_ONE_ID
    assert record.left_duration == 6
    assert state(hass, NURSE_LEFT).state == "off"
    assert state(hass, TWO_NURSE_RIGHT).state == "on"
    assert state(hass, TWO_NURSE_LEFT).state == "off"
    two = state(hass, TWO_NURSING_TIMER)
    assert two.state == minutes(1).isoformat()
    assert two.attributes["side"] == "right"


async def test_a_failed_nursing_write_keeps_the_side_on_and_the_retry_lands_once(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, freezer: Any
) -> None:
    await turn_on(hass, NURSE_LEFT)
    server.post_errors.append((BabyTrackerConnectionError("down"), False))
    freezer.move_to(minutes(5))

    with pytest.raises(HomeAssistantError):
        await turn_off(hass, NURSE_LEFT)
    assert state(hass, NURSE_LEFT).state == "on"
    await turn_off(hass, NURSE_LEFT)

    [record] = nursing_posts(server)
    assert record.left_duration == 5
    assert state(hass, NURSE_LEFT).state == "off"


async def test_stopping_during_a_failing_sync_raises_sync_failed_and_keeps_the_timer(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, freezer: Any
) -> None:
    await turn_on(hass, NURSE_LEFT)
    freezer.move_to(minutes(5))
    server.request_error = BabyTrackerConnectionError("down")

    await _expect_error(hass, NURSE_LEFT, "turn_off", "sync_failed", HomeAssistantError)

    assert posted(server) == []
    server.request_error = None
    await turn_off(hass, NURSE_LEFT)
    [record] = nursing_posts(server)
    assert record.left_duration == 5


async def test_switching_sides_works_while_a_sync_is_failing(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, freezer: Any
) -> None:
    """A start or a switch never syncs or posts, the same as the actions."""
    await turn_on(hass, NURSE_LEFT)
    server.request_error = BabyTrackerConnectionError("down")
    freezer.move_to(minutes(2))

    await turn_on(hass, NURSE_RIGHT)

    assert state(hass, NURSING_TIMER).attributes["side"] == "right"


# The 60-minute repair, through the new controls


async def test_a_timer_started_with_a_switch_raises_the_repair_at_an_hour(
    hass: HomeAssistant, init_integration: MockConfigEntry, freezer: Any
) -> None:
    await turn_on(hass, NURSE_LEFT)

    freezer.move_to(minutes(59))
    async_fire_time_changed(hass, minutes(59))
    await hass.async_block_till_done()
    assert _issue(hass, init_integration) is None

    freezer.move_to(minutes(60))
    async_fire_time_changed(hass, minutes(60))
    await hass.async_block_till_done()
    assert _issue(hass, init_integration) is not None


@pytest.mark.parametrize("how", ["turn_off", "discard", "switch_then_off"])
async def test_the_repair_clears_when_the_timer_stops_or_is_discarded(
    hass: HomeAssistant, init_integration: MockConfigEntry, freezer: Any, how: str
) -> None:
    await turn_on(hass, NURSE_LEFT)
    freezer.move_to(minutes(61))
    async_fire_time_changed(hass, minutes(61))
    await hass.async_block_till_done()
    assert _issue(hass, init_integration) is not None

    if how == "turn_off":
        await turn_off(hass, NURSE_LEFT)
    elif how == "discard":
        await press(hass, DISCARD_NURSING)
    else:
        await turn_on(hass, NURSE_RIGHT)  # a side switch doesn't restart the hour
        assert _issue(hass, init_integration) is not None
        await turn_off(hass, NURSE_RIGHT)

    assert _issue(hass, init_integration) is None


async def test_the_repair_for_one_baby_stays_when_the_other_babys_timer_stops(
    hass: HomeAssistant, init_integration: MockConfigEntry, freezer: Any
) -> None:
    await turn_on(hass, NURSE_LEFT)
    freezer.move_to(minutes(30))
    await turn_on(hass, TWO_NURSE_LEFT)
    freezer.move_to(minutes(61))
    async_fire_time_changed(hass, minutes(61))
    await hass.async_block_till_done()
    assert _issue(hass, init_integration) is not None
    assert _issue(hass, init_integration, BABY_TWO_ID) is None

    await turn_off(hass, TWO_NURSE_LEFT)

    assert _issue(hass, init_integration) is not None


async def test_a_timer_that_cannot_be_saved_still_turns_the_switch_on_and_says_so(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    real_save = Store.async_save

    async def save(self: Store[Any], data: Any) -> None:
        if self.key.endswith(".nursing"):
            raise OSError("disk full")
        await real_save(self, data)

    with patch.object(Store, "async_save", save):
        await _expect_error(hass, NURSE_LEFT, "turn_on", "nursing_not_saved", HomeAssistantError)

    assert state(hass, NURSE_LEFT).state == "on"


async def test_the_stop_sleep_action_also_shows_the_fresh_state_when_refused_as_stale(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    phone_sleep(server, start=minutes(-40))
    await sync(hass, init_integration)
    phone_ends_sleep(server, PHONE_SLEEP, start=minutes(-40), duration=38, changed=minutes(-2))

    with pytest.raises(ServiceValidationError) as err:
        await hass.services.async_call(
            DOMAIN,
            "stop_sleep",
            {"device_id": get_baby_device(dr.async_get(hass), BABY_ONE_ID).id},  # type: ignore[union-attr]
            blocking=True,
        )

    assert err.value.translation_key == "no_running_sleep"
    assert state(hass, SLEEP_SWITCH).state == "off"
    assert state(hass, ASLEEP).state == "off"


async def test_a_failing_sync_before_a_write_publishes_nothing_new(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    phone_sleep(server, start=minutes(-1))  # a phone's sleep Home Assistant can't fetch now
    server.request_error = BabyTrackerConnectionError("down")

    await _expect_error(hass, SLEEP_SWITCH, "turn_on", "sync_failed", HomeAssistantError)

    assert state(hass, SLEEP_SWITCH).state == "off"  # nothing was learned, so nothing changes
    assert posted(server) == []
