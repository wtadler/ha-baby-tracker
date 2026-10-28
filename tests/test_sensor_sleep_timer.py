"""Core PR 8: the sleep timer sensor, reading a phone's running sleep."""

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
)

from . import BABY_TWO, NOW, tick
from .fake_server import FakeServer
from .helpers import (
    ASLEEP,
    LAST_SLEEP,
    PHONE_SLEEP,
    SLEEP_TIMER,
    TWO_ASLEEP,
    TWO_SLEEP_TIMER,
    minutes,
    phone_ends_sleep,
    phone_sleep,
    state,
    sync,
)

PHONE_SLEEP_TWO = "5EE90000-0000-4000-8000-000000000002"


@pytest.fixture(autouse=True)
def _start_at_now(freezer: FrozenDateTimeFactory) -> None:
    """Freeze the clock at NOW before anything is set up.

    Timers scheduled at setup then share the clock the tests move, so moving it forward and
    ticking reaches them the way it does in core's tests.
    """
    freezer.move_to(NOW)


async def test_a_running_sleep_synced_from_a_phone_turns_asleep_on_and_shows_its_start(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    server: FakeServer,
    freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to(NOW)
    phone_sleep(server, start=minutes(-40))

    await sync(hass, init_integration)

    assert state(hass, ASLEEP).state == "on"
    timer = state(hass, SLEEP_TIMER)
    assert timer.state == minutes(-40).isoformat()
    assert timer.attributes["object_id"] == PHONE_SLEEP


async def test_a_sleep_a_phone_ends_while_home_assistant_shows_it_running_clears_next_sync(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    server: FakeServer,
    freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to(NOW)
    phone_sleep(server, start=minutes(-40))
    await sync(hass, init_integration)
    assert state(hass, ASLEEP).state == "on"

    phone_ends_sleep(server, PHONE_SLEEP, start=minutes(-40), duration=40, changed=NOW)
    await tick(hass, minutes=5)

    assert state(hass, ASLEEP).state == "off"
    assert state(hass, SLEEP_TIMER).state == "unknown"
    assert state(hass, LAST_SLEEP).attributes["duration_minutes"] == 40


async def test_a_running_sleep_for_one_baby_leaves_the_other_awake(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    server: FakeServer,
    freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to(NOW)
    phone_sleep(server, PHONE_SLEEP_TWO, start=minutes(-15), baby=BABY_TWO)

    await sync(hass, init_integration)

    assert state(hass, TWO_ASLEEP).state == "on"
    assert state(hass, TWO_SLEEP_TIMER).state == minutes(-15).isoformat()
    assert state(hass, ASLEEP).state == "off"
    assert state(hass, SLEEP_TIMER).state == "unknown"
