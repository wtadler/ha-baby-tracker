"""Core PR 18: the nursing timer actions."""

import asyncio

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from .fake_server import FakeServer
from .helpers import (
    NOW,
    NURSE_LEFT,
    NURSING_TIMER,
    call,
    minutes,
    nurse,
    nursing_posts,
    posted,
    state,
    toggle,
)


@pytest.fixture(autouse=True)
def _start_at_now(freezer: FrozenDateTimeFactory) -> None:
    """Freeze the clock at NOW before anything is set up.

    Timers scheduled at setup then share the clock the tests move, so moving it forward and
    ticking reaches them the way it does in core's tests.
    """
    freezer.move_to(NOW)


async def test_start_switch_and_stop_nursing_actions(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    server: FakeServer,
    freezer: FrozenDateTimeFactory,
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
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    server: FakeServer,
    freezer: FrozenDateTimeFactory,
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
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    server: FakeServer,
    freezer: FrozenDateTimeFactory,
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
