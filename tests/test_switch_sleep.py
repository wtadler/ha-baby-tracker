"""Core PR 17: the Sleep switch."""

from datetime import UTC, datetime

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr
from pybabytracker import (
    OP_ADD,
    OP_UPDATE,
    BabyTrackerConnectionError,
    Sleep,
)
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
)

from custom_components.baby_tracker.const import (
    DOMAIN,
)

from . import NOW, get_baby_device
from .factories import (
    BABY_ONE_ID,
    BABY_TWO_ID,
)
from .fake_server import FakeServer
from .helpers import (
    ASLEEP,
    LAST_SLEEP,
    NURSE_LEFT,
    NURSE_RIGHT,
    PHONE_SLEEP,
    SLEEP_SWITCH,
    SLEEP_TIMER,
    TWO_ASLEEP,
    TWO_SLEEP_SWITCH,
    TWO_SLEEP_TIMER,
    minutes,
    only_record,
    phone_ends_sleep,
    phone_sleep,
    posted,
    state,
    sync,
    toggle,
    turn_off,
    turn_on,
)


@pytest.fixture(autouse=True)
def _start_at_now(freezer: FrozenDateTimeFactory) -> None:
    """Freeze the clock at NOW before anything is set up."""
    freezer.move_to(NOW)


async def _expect_error(
    hass: HomeAssistant, entity_id: str, service: str, key: str, kind: type[Exception]
) -> None:
    with pytest.raises(kind) as err:
        await hass.services.async_call("switch", service, {"entity_id": entity_id}, blocking=True)
    assert getattr(err.value, "translation_key", None) == key
    assert getattr(err.value, "translation_domain", None) == DOMAIN


async def test_a_started_timer_sends_the_standard_offset_in_summer(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    server: FakeServer,
    freezer: FrozenDateTimeFactory,
) -> None:
    await hass.config.async_set_time_zone("America/New_York")
    freezer.move_to(datetime(2026, 7, 15, 16, 0, tzinfo=UTC))

    await toggle(hass, SLEEP_SWITCH)

    assert only_record(server).timezone == -18000


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
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    server: FakeServer,
    freezer: FrozenDateTimeFactory,
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
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    server: FakeServer,
    freezer: FrozenDateTimeFactory,
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
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    server: FakeServer,
    freezer: FrozenDateTimeFactory,
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
