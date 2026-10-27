"""Core PRs 9 through 15: writes show in the sensors at once, and what a failed write or a
removed entry leaves behind."""

import asyncio
from datetime import timedelta

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_system import METRIC_SYSTEM
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.nighp_baby_tracker.const import (
    DOMAIN,
)
from custom_components.nighp_baby_tracker.pybabytracker import (
    NoResponseError,
)

from . import (
    OWN_DEVICE,
)
from .factories import (
    BABY_ONE_ID,
)
from .fake_server import FakeServer
from .helpers import (
    BOTTLE_TODAY,
    FEEDS_TODAY,
    LAST_FEED,
    LAST_PUMP,
    NOW,
    button,
    call,
    device_id,
    fail_to_write,
    gap_issue,
    log_pump,
    press,
    state_of,
    undo,
)


@pytest.fixture(autouse=True)
def _start_at_now(freezer: FrozenDateTimeFactory) -> None:
    """Freeze the clock at NOW before anything is set up.

    Timers scheduled at setup then share the clock the tests move, so moving it forward and
    ticking reaches them the way it does in core's tests.
    """
    freezer.move_to(NOW)


async def test_log_pump_updates_the_last_pump_sensor_without_a_poll(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    await log_pump(
        hass,
        config_entry_id=init_integration.entry_id,
        left_amount=2,
        right_amount=3,
        unit="fl. oz.",
        time="2026-10-26T08:00:00+00:00",
    )

    state = state_of(hass, init_integration)
    assert state.state == "2026-10-26T08:00:00+00:00"
    assert state.attributes["amount"] == 5


async def test_the_pending_post_and_its_grace_window_survive_a_restart(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    server: FakeServer,
    freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to(NOW)
    server.post_errors.append((NoResponseError("timed out"), False))
    with pytest.raises(HomeAssistantError):
        await press(hass, button("baby_one", "wet"))

    assert await hass.config_entries.async_reload(init_integration.entry_id)
    await hass.async_block_till_done()
    assert init_integration.state is ConfigEntryState.LOADED
    with pytest.raises(HomeAssistantError) as raised:
        await press(hass, button("baby_one", "wet"))

    assert raised.value.translation_key == "write_outcome_unknown"


async def test_the_repair_issue_is_cleared_when_the_entry_is_removed(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    server: FakeServer,
    freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to(NOW)
    server.last_sync_override[OWN_DEVICE] = 5
    await fail_to_write(hass)
    freezer.tick(timedelta(hours=2))
    await fail_to_write(hass)
    issue_id = f"own_log_gap_{init_integration.entry_id}"
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None

    await hass.config_entries.async_remove(init_integration.entry_id)

    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None


async def test_a_removed_entry_forgets_its_gap_clock(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    server: FakeServer,
    freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to(NOW)
    server.last_sync_override[OWN_DEVICE] = 5
    await fail_to_write(hass)
    freezer.tick(timedelta(hours=2))
    await hass.config_entries.async_remove(init_integration.entry_id)
    await hass.async_block_till_done()
    init_integration.add_to_hass(hass)  # the same entry ID comes back, as a fresh account
    await hass.config_entries.async_setup(init_integration.entry_id)
    await hass.async_block_till_done()

    await fail_to_write(hass)

    assert gap_issue(hass, init_integration) is None


async def test_a_wet_press_bumps_todays_count_with_no_poll(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    server: FakeServer,
    freezer: FrozenDateTimeFactory,
) -> None:
    await hass.config.async_set_time_zone("America/New_York")
    freezer.move_to(NOW)
    assert hass.states.get("sensor.baby_one_diapers_today").state == "0"

    await press(hass, button("baby_one", "wet"))
    state = hass.states.get("sensor.baby_one_diapers_today")
    assert state.state == "1"
    assert (state.attributes["wet"], state.attributes["dirty"]) == (1, 0)

    await press(hass, button("baby_one", "dirty"))
    state = hass.states.get("sensor.baby_one_diapers_today")
    assert state.state == "2"
    assert (state.attributes["wet"], state.attributes["dirty"]) == (1, 1)
    assert hass.states.get("sensor.baby_two_diapers_today").state == "0"

    await undo(hass, device_id=device_id(hass, BABY_ONE_ID))
    assert hass.states.get("sensor.baby_one_diapers_today").state == "1"


async def test_a_bottle_updates_the_last_feed_sensor_without_a_poll(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    assert hass.states.get(LAST_FEED).state == "unknown"

    await call(
        hass,
        "log_bottle",
        kind="pumped",
        amount=3,
        unit="fl. oz.",
        time="2026-10-26T09:15:00+00:00",
        note="Left breast",
    )

    state = hass.states.get(LAST_FEED)
    assert state.state == "2026-10-26T09:15:00+00:00"
    assert state.attributes["kind"] == "pumped"
    assert state.attributes["amount"] == 3
    assert state.attributes["unit"] == "fl. oz."
    assert state.attributes["note"] == "Left breast"


async def test_nursing_updates_the_last_feed_sensor_without_a_poll(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    await call(
        hass,
        "log_nursing",
        left_minutes=8,
        right_minutes=6,
        finish_side="right",
        end_time="2026-10-26T10:00:00+00:00",
    )

    state = hass.states.get(LAST_FEED)
    assert state.state == "2026-10-26T09:46:00+00:00"
    assert state.attributes["kind"] == "nursing"
    assert state.attributes["left_duration_minutes"] == 8
    assert state.attributes["right_duration_minutes"] == 6
    assert state.attributes["finish_side"] == "right"


async def test_a_pump_updates_the_last_pump_sensor_without_a_poll(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    await call(
        hass,
        "log_pump",
        left_amount=2,
        right_amount=3,
        unit="fl. oz.",
        left_minutes=10,
        right_minutes=12,
        time="2026-10-26T08:00:00+00:00",
    )

    state = hass.states.get(LAST_PUMP)
    assert state.state == "2026-10-26T08:00:00+00:00"
    assert state.attributes["amount"] == 5
    assert state.attributes["left_amount"] == 2
    assert state.attributes["right_amount"] == 3
    assert state.attributes["left_duration_minutes"] == 10


async def test_a_bottle_bumps_todays_feeds_and_bottle_volume_with_no_poll(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    server: FakeServer,
    freezer: FrozenDateTimeFactory,
) -> None:
    # Metric display: the sensor still shows today's volume in mL, converted from the ounces.
    hass.config.units = METRIC_SYSTEM
    await hass.config.async_set_time_zone("America/New_York")
    freezer.move_to(NOW)
    assert hass.states.get(FEEDS_TODAY).state == "0"
    assert float(hass.states.get(BOTTLE_TODAY).state) == 0

    await call(hass, "log_bottle", kind="formula", amount=4, unit="fl. oz.")
    assert hass.states.get(FEEDS_TODAY).state == "1"
    state = hass.states.get(BOTTLE_TODAY)
    assert state.attributes["unit_of_measurement"] == "mL"
    assert float(state.state) == pytest.approx(4 * 29.5735, abs=0.1)

    await call(hass, "log_bottle", kind="pumped", amount=2, unit="fl. oz.")
    assert hass.states.get(FEEDS_TODAY).state == "2"
    assert float(hass.states.get(BOTTLE_TODAY).state) == pytest.approx(6 * 29.5735, abs=0.1)
    assert hass.states.get("sensor.baby_two_feeds_today").state == "0"

    await undo(hass, device_id=device_id(hass, BABY_ONE_ID))
    assert hass.states.get(FEEDS_TODAY).state == "1"
    assert float(hass.states.get(BOTTLE_TODAY).state) == pytest.approx(4 * 29.5735, abs=0.1)


async def test_a_scheduled_sync_waits_for_a_write_in_progress(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    gate = asyncio.Event()
    server.post_gate = gate  # the write's post waits here, so the write keeps its turn
    write = hass.async_create_task(press(hass, "button.baby_one_wet_diaper"))
    for _ in range(20):
        await asyncio.sleep(0)
    assert "post" in server.calls
    calls = server.device_list_calls

    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=5, seconds=1))
    for _ in range(20):
        await asyncio.sleep(0)
    assert server.device_list_calls == calls  # the sync is waiting for the write

    gate.set()
    await write
    await hass.async_block_till_done()

    assert server.device_list_calls == calls + 1  # then it runs
    assert [op for _, op, _ in server.posted] == [0]
