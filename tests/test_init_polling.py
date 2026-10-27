"""Core PRs 8, 18, and 21: one-minute polling while a sleep, nursing, or pump timer runs."""

from datetime import timedelta
from typing import Any

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from .fake_server import FakeServer
from .helpers import (
    ASLEEP,
    LEGACY_INTERVAL,
    NOW,
    NURSE_LEFT,
    PHONE_SLEEP,
    SLEEP_SWITCH,
    entry_with_options,
    minutes,
    nurse,
    phone_ends_sleep,
    phone_sleep,
    state,
    sync_interval,
    tick,
    toggle,
)


@pytest.fixture(autouse=True)
def _start_at_now(freezer: FrozenDateTimeFactory) -> None:
    """Freeze the clock at NOW before anything is set up.

    Timers scheduled at setup then share the clock the tests move, so moving it forward and
    ticking reaches them the way it does in core's tests.
    """
    freezer.move_to(NOW)


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
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    server: FakeServer,
    freezer: FrozenDateTimeFactory,
) -> None:
    await nurse(hass, freezer, (0, "left"))
    assert await sync_interval(hass, server) == timedelta(minutes=1)

    await nurse(hass, freezer, (2, "left"))

    assert await sync_interval(hass, server) == timedelta(minutes=5)


async def test_a_sleep_home_assistant_starts_switches_polling_at_once(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    server: FakeServer,
    freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to(NOW)
    await toggle(hass, SLEEP_SWITCH)
    assert await sync_interval(hass, server) == timedelta(minutes=1)
    freezer.move_to(minutes(3))
    await toggle(hass, SLEEP_SWITCH)
    assert await sync_interval(hass, server) == timedelta(minutes=5)


async def test_a_second_account_polls_on_its_own_schedule(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    second_entry: MockConfigEntry,
    server: FakeServer,
    second_server: FakeServer,
    freezer: FrozenDateTimeFactory,
) -> None:
    await nurse(hass, freezer, (0, "left"))

    assert await sync_interval(hass, server) == timedelta(minutes=1)
    assert await sync_interval(hass, second_server) == timedelta(minutes=5)


@pytest.mark.parametrize("options", [{}, {LEGACY_INTERVAL: 15}], ids=["new", "legacy"])
async def test_a_running_sleep_polls_every_minute_and_five_when_it_stops(
    hass: HomeAssistant, server: FakeServer, options: dict[str, Any], freezer: FrozenDateTimeFactory
) -> None:
    await entry_with_options(hass, options)

    await toggle(hass, SLEEP_SWITCH)
    assert await sync_interval(hass, server) == timedelta(minutes=1)

    freezer.tick(timedelta(minutes=3))
    await toggle(hass, SLEEP_SWITCH)
    assert await sync_interval(hass, server) == timedelta(minutes=5)


@pytest.mark.parametrize("options", [{}, {LEGACY_INTERVAL: 15}], ids=["new", "legacy"])
async def test_a_running_nursing_timer_polls_every_minute_and_five_when_it_stops(
    hass: HomeAssistant, server: FakeServer, options: dict[str, Any], freezer: FrozenDateTimeFactory
) -> None:
    await entry_with_options(hass, options)

    await toggle(hass, NURSE_LEFT)
    assert await sync_interval(hass, server) == timedelta(minutes=1)

    freezer.tick(timedelta(minutes=3))
    await toggle(hass, NURSE_LEFT)
    assert await sync_interval(hass, server) == timedelta(minutes=5)


async def test_a_running_timer_syncs_every_minute(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    await toggle(hass, SLEEP_SWITCH)
    calls = server.device_list_calls

    await tick(hass, minutes=1)

    assert server.device_list_calls == calls + 1
