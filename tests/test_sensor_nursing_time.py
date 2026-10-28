"""Core PR 19: the nursing timer and the left and right time sensors."""

from datetime import timedelta
from typing import Any

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.baby_tracker.pybabytracker import BabyTrackerConnectionError

from . import NOW, get_baby_device
from .factories import (
    BABY_ONE_ID,
    BABY_TWO_ID,
)
from .fake_server import FakeServer
from .helpers import (
    NURSE_LEFT,
    NURSE_RIGHT,
    NURSING_LEFT_TIME,
    NURSING_RIGHT_TIME,
    NURSING_TIMER,
    SLEEP_TIMER,
    minutes,
    state,
    sync,
    toggle,
    turn_off,
    turn_on,
)


@pytest.fixture(autouse=True)
def _start_at_now(freezer: FrozenDateTimeFactory) -> None:
    """Freeze the clock at NOW before anything is set up.

    Timers scheduled at setup then share the clock the tests move, so moving it forward and
    ticking reaches them the way it does in core's tests.
    """
    freezer.move_to(NOW)


async def _tick_seconds(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, seconds: float
) -> None:
    """Move the clock and fire the timers due, without any sync being asked for."""
    freezer.tick(timedelta(seconds=seconds))
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()


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
    freezer: FrozenDateTimeFactory,
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
    freezer: FrozenDateTimeFactory,
) -> None:
    await turn_on(hass, NURSE_LEFT)
    changes: list[str] = []

    @callback
    def record(event: Event[Any]) -> None:
        changes.append(event.data["new_state"].state)

    @callback
    def only_left(data: dict[str, Any]) -> bool:
        return bool(data["entity_id"] == NURSING_LEFT_TIME)

    hass.bus.async_listen("state_changed", record, event_filter=only_left)
    for _ in range(4):
        await _tick_seconds(hass, freezer, 30)

    assert len(changes) >= 2  # at least one write per minute
    assert changes[-1] == "2.0"


async def test_the_time_on_each_side_across_a_side_switch(
    hass: HomeAssistant, init_integration: MockConfigEntry, freezer: FrozenDateTimeFactory
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
    hass: HomeAssistant, init_integration: MockConfigEntry, freezer: FrozenDateTimeFactory
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
    hass: HomeAssistant, init_integration: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    await turn_on(hass, NURSE_LEFT)
    freezer.move_to(minutes(4))
    await hass.services.async_call(
        "button", "press", {"entity_id": "button.baby_one_discard_nursing"}, blocking=True
    )

    assert state(hass, NURSING_LEFT_TIME).state == "unknown"


async def test_a_new_session_after_a_stop_starts_from_zero(
    hass: HomeAssistant, init_integration: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    await toggle(hass, NURSE_LEFT)
    freezer.move_to(minutes(9))
    await toggle(hass, NURSE_LEFT)
    freezer.move_to(minutes(20))

    await toggle(hass, NURSE_RIGHT)

    assert state(hass, NURSING_LEFT_TIME).state == "0.0"
    assert state(hass, NURSING_RIGHT_TIME).state == "0.0"


async def test_two_babies_duration_sensors_run_independently(
    hass: HomeAssistant, init_integration: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    await turn_on(hass, NURSE_LEFT)
    freezer.move_to(minutes(2))
    await turn_on(hass, "switch.baby_two_nursing_right")
    await _tick_seconds(hass, freezer, 180)

    assert state(hass, NURSING_LEFT_TIME).state == "5.0"
    assert state(hass, "sensor.baby_two_nursing_left_time").state == "0.0"
    assert state(hass, "sensor.baby_two_nursing_right_time").state == "3.0"


async def test_the_duration_sensors_hold_their_value_when_the_clock_goes_backwards(
    hass: HomeAssistant, init_integration: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    await turn_on(hass, NURSE_LEFT)
    freezer.move_to(NOW - timedelta(minutes=5))
    async_fire_time_changed(hass, NOW - timedelta(minutes=5))
    await hass.async_block_till_done()

    assert state(hass, NURSING_LEFT_TIME).state == "0.0"  # never negative


async def test_a_session_saved_before_a_restart_shows_its_time_after_it(
    hass: HomeAssistant, init_integration: MockConfigEntry, freezer: FrozenDateTimeFactory
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
