"""Core PRs 8, 18, and 21: the account syncs every five minutes, whatever timer is running.

Running nursing and pump timers live only in Home Assistant, so a faster sync would show nothing
new. A synced sleep is the one timer a phone can end, and that stop can take up to five minutes
to appear.
"""

from datetime import timedelta
from typing import Any

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from . import NOW, sync_interval, tick
from .fake_server import FakeServer
from .helpers import (
    ASLEEP,
    LEGACY_INTERVAL,
    NURSE_LEFT,
    NURSE_RIGHT,
    PHONE_SLEEP,
    PUMP_SWITCH,
    SLEEP_SWITCH,
    entry_with_options,
    minutes,
    nurse,
    phone_ends_sleep,
    phone_sleep,
    state,
    toggle,
    turn_off,
    turn_on,
)

FIVE_MINUTES = timedelta(minutes=5)


def _configured(entry: MockConfigEntry) -> timedelta | None:
    return entry.runtime_data.update_interval


@pytest.fixture(autouse=True)
def _start_at_now(freezer: FrozenDateTimeFactory) -> None:
    """Freeze the clock at NOW before anything is set up.

    Timers scheduled at setup then share the clock the tests move, so moving it forward and
    ticking reaches them the way it does in core's tests.
    """
    freezer.move_to(NOW)


async def test_an_idle_account_syncs_every_five_minutes(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    assert _configured(init_integration) == FIVE_MINUTES
    assert await sync_interval(hass, server) == FIVE_MINUTES


async def test_a_running_synced_sleep_keeps_the_interval_at_five_minutes_until_it_ends(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    start = dt_util.utcnow().replace(microsecond=0) - timedelta(minutes=10)
    phone_sleep(server, start=start)
    await tick(hass, minutes=5)  # the five-minute sync finds the running sleep
    assert state(hass, ASLEEP).state == "on"
    assert _configured(init_integration) == FIVE_MINUTES
    assert await sync_interval(hass, server) == FIVE_MINUTES

    phone_ends_sleep(server, PHONE_SLEEP, start=start, duration=11, changed=start)
    await tick(hass, minutes=5)

    assert state(hass, ASLEEP).state == "off"
    assert _configured(init_integration) == FIVE_MINUTES
    assert await sync_interval(hass, server) == FIVE_MINUTES


async def test_a_sleep_ended_on_a_phone_shows_up_at_the_next_five_minute_sync_not_sooner(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    server: FakeServer,
    freezer: FrozenDateTimeFactory,
) -> None:
    start = dt_util.utcnow().replace(microsecond=0) - timedelta(minutes=10)
    phone_sleep(server, start=start)
    await tick(hass, minutes=5)
    assert state(hass, ASLEEP).state == "on"

    phone_ends_sleep(server, PHONE_SLEEP, start=start, duration=11, changed=start)
    freezer.tick(timedelta(minutes=2))
    await tick(hass, minutes=0)
    assert state(hass, ASLEEP).state == "on"

    freezer.tick(timedelta(minutes=3))
    await tick(hass, minutes=0)
    assert state(hass, ASLEEP).state == "off"


async def test_a_nursing_timer_keeps_the_interval_at_five_minutes_through_start_switch_and_stop(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    server: FakeServer,
    freezer: FrozenDateTimeFactory,
) -> None:
    await nurse(hass, freezer, (0, "left"))
    assert _configured(init_integration) == FIVE_MINUTES
    assert await sync_interval(hass, server) == FIVE_MINUTES

    await nurse(hass, freezer, (1, "right"))
    assert _configured(init_integration) == FIVE_MINUTES

    await nurse(hass, freezer, (2, "right"))  # a second press on the side that runs stops it
    assert _configured(init_integration) == FIVE_MINUTES
    assert await sync_interval(hass, server) == FIVE_MINUTES


async def test_a_sleep_home_assistant_starts_keeps_the_interval_at_five_minutes(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    server: FakeServer,
    freezer: FrozenDateTimeFactory,
) -> None:
    await toggle(hass, SLEEP_SWITCH)
    assert state(hass, SLEEP_SWITCH).state == "on"
    assert _configured(init_integration) == FIVE_MINUTES
    assert await sync_interval(hass, server) == FIVE_MINUTES

    freezer.move_to(minutes(3))
    await toggle(hass, SLEEP_SWITCH)
    assert state(hass, SLEEP_SWITCH).state == "off"
    assert _configured(init_integration) == FIVE_MINUTES
    assert await sync_interval(hass, server) == FIVE_MINUTES


async def test_a_pump_timer_keeps_the_interval_at_five_minutes_through_start_and_stop(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    await turn_on(hass, PUMP_SWITCH)
    assert state(hass, PUMP_SWITCH).state == "on"
    assert _configured(init_integration) == FIVE_MINUTES
    assert await sync_interval(hass, server) == FIVE_MINUTES

    await turn_off(hass, PUMP_SWITCH)
    assert state(hass, PUMP_SWITCH).state == "off"
    assert _configured(init_integration) == FIVE_MINUTES
    assert await sync_interval(hass, server) == FIVE_MINUTES


async def test_every_kind_of_timer_at_once_still_syncs_every_five_minutes(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    server: FakeServer,
    freezer: FrozenDateTimeFactory,
) -> None:
    await toggle(hass, SLEEP_SWITCH)
    await toggle(hass, NURSE_LEFT)
    await turn_on(hass, PUMP_SWITCH)

    assert state(hass, SLEEP_SWITCH).state == "on"
    assert state(hass, NURSE_LEFT).state == "on"
    assert state(hass, PUMP_SWITCH).state == "on"
    assert _configured(init_integration) == FIVE_MINUTES
    assert await sync_interval(hass, server) == FIVE_MINUTES


async def test_a_nursing_timer_past_its_guard_hour_still_syncs_every_five_minutes(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    server: FakeServer,
    freezer: FrozenDateTimeFactory,
) -> None:
    await toggle(hass, NURSE_LEFT)

    freezer.move_to(minutes(61))
    await tick(hass, minutes=5)

    assert state(hass, NURSE_LEFT).state == "on"
    assert _configured(init_integration) == FIVE_MINUTES
    assert await sync_interval(hass, server) == FIVE_MINUTES


async def test_a_second_account_with_a_timer_leaves_both_accounts_at_five_minutes(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    second_entry: MockConfigEntry,
    server: FakeServer,
    second_server: FakeServer,
    freezer: FrozenDateTimeFactory,
) -> None:
    await nurse(hass, freezer, (0, "left"))

    assert _configured(init_integration) == FIVE_MINUTES
    assert _configured(second_entry) == FIVE_MINUTES
    assert await sync_interval(hass, server) == FIVE_MINUTES
    assert await sync_interval(hass, second_server) == FIVE_MINUTES


@pytest.mark.parametrize("options", [{}, {LEGACY_INTERVAL: 15}], ids=["new", "legacy"])
@pytest.mark.parametrize("switch", [SLEEP_SWITCH, NURSE_RIGHT], ids=["sleep", "nursing"])
async def test_a_running_timer_leaves_the_interval_at_five_minutes_whatever_an_old_install_saved(
    hass: HomeAssistant,
    server: FakeServer,
    options: dict[str, Any],
    switch: str,
    freezer: FrozenDateTimeFactory,
) -> None:
    entry = await entry_with_options(hass, options)

    await toggle(hass, switch)
    assert _configured(entry) == FIVE_MINUTES
    assert await sync_interval(hass, server) == FIVE_MINUTES

    freezer.tick(timedelta(minutes=3))
    await toggle(hass, switch)
    assert _configured(entry) == FIVE_MINUTES
    assert await sync_interval(hass, server) == FIVE_MINUTES


async def test_a_running_timer_syncs_on_the_five_minute_schedule_and_not_every_minute(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    server: FakeServer,
    freezer: FrozenDateTimeFactory,
) -> None:
    await toggle(hass, SLEEP_SWITCH)
    calls = server.device_list_calls

    freezer.tick(timedelta(minutes=1))
    await tick(hass, minutes=0)
    assert server.device_list_calls == calls

    freezer.tick(timedelta(minutes=4))
    await tick(hass, minutes=0)
    assert server.device_list_calls == calls + 1
