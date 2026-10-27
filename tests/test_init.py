"""Setup, sync, polling, persistence, migration, stale devices, unloading, and failures."""

import asyncio
import logging
from datetime import timedelta
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.config_entries import SOURCE_USER, ConfigEntryState
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_system import METRIC_SYSTEM
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed
from pytest_homeassistant_custom_component.typing import WebSocketGenerator

from custom_components.nighp_baby_tracker.const import (
    CONF_DEVICE_UUID,
    DOMAIN,
)
from custom_components.nighp_baby_tracker.pybabytracker import (
    AuthenticationError,
    BabyTrackerConnectionError,
    NoResponseError,
    ProtocolError,
)

from . import (
    BABY_ONE,
    EMAIL,
    EMAIL_TWO,
    NEW_PASSWORD,
    OWN_DEVICE,
    PASSWORD,
)
from .factories import (
    BABY_ONE_ID,
    BABY_THREE_ID,
    BABY_TWO_ID,
    DEVICE_A,
    DEVICE_B,
    OP_DELETE,
    at,
    record_payload,
    volume,
)
from .fake_server import FakeServer
from .helpers import (
    ASLEEP,
    BOTTLE_TODAY,
    DIAPER,
    FEEDS_TODAY,
    LAST_FEED,
    LAST_PUMP,
    LEGACY_INTERVAL,
    NOW,
    NURSE_LEFT,
    NURSING_TIMER,
    PHONE_SLEEP,
    SLEEP_BUTTON,
    account_device,
    button,
    call,
    device_id,
    entry_with_options,
    fail_to_write,
    gap_issue,
    log_pump,
    minutes,
    nurse,
    nursing_posts,
    phone_ends_sleep,
    phone_sleep,
    press,
    pump_entity,
    state,
    state_of,
    sync_interval,
    tick,
    undo,
)


def add_old_pump_entity(
    hass: HomeAssistant, entry: MockConfigEntry, baby_id: str, name: str
) -> er.RegistryEntry:
    """What an earlier version registered: ``<baby_id>_last_pump`` on the baby's device."""
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, baby_id)}, name=name
    )
    return er.async_get(hass).async_get_or_create(
        "sensor",
        DOMAIN,
        f"{baby_id}_last_pump",
        config_entry=entry,
        device_id=device.id,
        translation_key="last_pump",
        has_entity_name=True,
    )


@pytest.fixture(autouse=True)
def _start_at_now(freezer: Any) -> None:
    """Freeze the clock at NOW before anything is set up.

    Timers scheduled at setup then share the clock the tests move, so moving it forward and
    ticking reaches them the way it does in core's tests.
    """
    freezer.move_to(NOW)


def _storage_key(entry: MockConfigEntry) -> str:
    return f"{DOMAIN}.{entry.entry_id}"


async def _setup_entry(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


GONE_ID = "55555555-EEEE-4EEE-8EEE-000000000005"


async def _remove(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    entry: MockConfigEntry,
    device_id: str,
) -> dict[str, Any]:
    assert await async_setup_component(hass, "config", {})
    client = await hass_ws_client(hass)
    await client.send_json(
        {
            "id": 1,
            "type": "config/device_registry/remove_config_entry",
            "config_entry_id": entry.entry_id,
            "device_id": device_id,
        }
    )
    return await client.receive_json()


def _device(hass: HomeAssistant, entry: MockConfigEntry, identifier: str) -> dr.DeviceEntry:
    [device] = dr.async_get(hass).async_get_devices(identifiers={(DOMAIN, identifier)})
    return device


# --- log_pump -------------------------------------------------------------------------------------


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


# --- one-time cleanup of the old per-baby last-pump entities ---------------------------------


async def test_setup_removes_the_old_per_baby_last_pump_entities(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    mock_config_entry.add_to_hass(hass)
    old_one = add_old_pump_entity(hass, mock_config_entry, BABY_ONE_ID, "Baby One")
    old_two = add_old_pump_entity(hass, mock_config_entry, BABY_TWO_ID, "Baby Two")
    registry = er.async_get(hass)
    assert registry.async_get(old_one.entity_id) is not None

    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert registry.async_get(old_one.entity_id) is None
    assert registry.async_get(old_two.entity_id) is None
    assert registry.async_get_entity_id("sensor", DOMAIN, f"{BABY_ONE_ID}_last_pump") is None
    assert registry.async_get_entity_id("sensor", DOMAIN, f"{BABY_TWO_ID}_last_pump") is None
    assert hass.states.get(old_one.entity_id) is None
    # ... and the account's replaces them.
    assert state_of(hass, mock_config_entry).state == "unknown"


async def test_the_cleanup_leaves_every_other_per_baby_entity_alone(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    mock_config_entry.add_to_hass(hass)
    add_old_pump_entity(hass, mock_config_entry, BABY_ONE_ID, "Baby One")

    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    registry = er.async_get(hass)
    assert registry.async_get_entity_id("sensor", DOMAIN, f"{BABY_ONE_ID}_last_pump") is None
    for key in ("last_diaper", "last_feed", "last_sleep", "diapers_today", "feeds_today"):
        assert registry.async_get_entity_id("sensor", DOMAIN, f"{BABY_ONE_ID}_{key}") is not None, (
            key
        )
    assert hass.states.get("sensor.baby_one_last_diaper") is not None


async def test_the_cleanup_runs_again_harmlessly_on_a_reload(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    mock_config_entry.add_to_hass(hass)
    add_old_pump_entity(hass, mock_config_entry, BABY_ONE_ID, "Baby One")
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    first = pump_entity(hass, mock_config_entry)

    assert await hass.config_entries.async_reload(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert pump_entity(hass, mock_config_entry) == first
    assert (
        er.async_get(hass).async_get_entity_id("sensor", DOMAIN, f"{BABY_ONE_ID}_last_pump") is None
    )


async def test_the_cleanup_keeps_the_new_account_sensors_registry_entry(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    registry = er.async_get(hass)
    before = registry.async_get(pump_entity(hass, init_integration))

    assert await hass.config_entries.async_reload(init_integration.entry_id)
    await hass.async_block_till_done()

    after = registry.async_get(pump_entity(hass, init_integration))
    assert before is not None and after is not None
    assert after.id == before.id  # kept, not removed and recreated


async def test_the_cleanup_runs_for_every_entry_and_each_keeps_its_own_account_sensor(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    mock_config_entry.add_to_hass(hass)
    other = MockConfigEntry(
        domain=DOMAIN, title=EMAIL_TWO, unique_id=EMAIL_TWO, data=dict(mock_config_entry.data)
    )
    other.add_to_hass(hass)
    old_first = add_old_pump_entity(hass, mock_config_entry, BABY_ONE_ID, "Baby One")
    old_second = add_old_pump_entity(hass, other, BABY_THREE_ID, "Baby Three")

    await hass.config_entries.async_setup(mock_config_entry.entry_id)  # sets up the whole domain
    await hass.async_block_till_done()

    registry = er.async_get(hass)
    assert registry.async_get(old_first.entity_id) is None
    assert registry.async_get(old_second.entity_id) is None
    assert pump_entity(hass, mock_config_entry) != pump_entity(hass, other)
    assert account_device(hass, mock_config_entry).id != account_device(hass, other).id


async def test_the_cleanup_removes_an_old_entity_for_a_baby_no_longer_in_the_account(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    mock_config_entry.add_to_hass(hass)
    gone = add_old_pump_entity(hass, mock_config_entry, BABY_THREE_ID, "Baby Three")

    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert er.async_get(hass).async_get(gone.entity_id) is None


# Nursing: surviving a restart


async def test_a_nursing_timer_survives_a_restart(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, freezer: Any
) -> None:
    await nurse(hass, freezer, (0, "left"), (4, "right"))

    await hass.config_entries.async_reload(init_integration.entry_id)
    await hass.async_block_till_done()
    assert init_integration.state is ConfigEntryState.LOADED
    timer = state(hass, NURSING_TIMER)
    assert timer.state == NOW.isoformat()
    assert timer.attributes["side"] == "right"
    assert timer.attributes["left_seconds"] == 240

    await nurse(hass, freezer, (10, "right"))  # the time across the restart counts

    [record] = nursing_posts(server)
    assert (record.left_duration, record.right_duration, record.finish_side) == (4, 6, 1)


async def test_a_corrupt_saved_nursing_timer_is_ignored(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    store: Store = Store(hass, 1, f"{DOMAIN}.{mock_config_entry.entry_id}.nursing")
    await store.async_save({BABY_ONE_ID: {"side": "middle"}})
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.LOADED
    assert state(hass, NURSING_TIMER).state == "unknown"


async def test_removing_the_entry_deletes_the_saved_nursing_timers(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    freezer: Any,
    hass_storage: dict[str, Any],
) -> None:
    await nurse(hass, freezer, (0, "left"))
    key = f"{DOMAIN}.{init_integration.entry_id}.nursing"
    await hass.async_block_till_done()
    assert key in hass_storage

    await hass.config_entries.async_remove(init_integration.entry_id)
    await hass.async_block_till_done()

    assert key not in hass_storage


# Polling


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
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, freezer: Any
) -> None:
    await nurse(hass, freezer, (0, "left"))
    assert await sync_interval(hass, server) == timedelta(minutes=1)

    await nurse(hass, freezer, (2, "left"))

    assert await sync_interval(hass, server) == timedelta(minutes=5)


async def test_a_sleep_home_assistant_starts_switches_polling_at_once(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, freezer: Any
) -> None:
    freezer.move_to(NOW)
    await press(hass, SLEEP_BUTTON)
    assert await sync_interval(hass, server) == timedelta(minutes=1)
    freezer.move_to(minutes(3))
    await press(hass, SLEEP_BUTTON)
    assert await sync_interval(hass, server) == timedelta(minutes=5)


async def test_a_second_account_polls_on_its_own_schedule(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    second_entry: MockConfigEntry,
    server: FakeServer,
    second_server: FakeServer,
    freezer: Any,
) -> None:
    await nurse(hass, freezer, (0, "left"))

    assert await sync_interval(hass, server) == timedelta(minutes=1)
    assert await sync_interval(hass, second_server) == timedelta(minutes=5)


# Nursing timers under the lock, orphaned timers, and a failed save


async def test_a_saved_nursing_timer_for_a_baby_no_longer_in_the_account_is_dropped(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    server: FakeServer,
    hass_storage: dict[str, Any],
) -> None:
    key = f"{DOMAIN}.{mock_config_entry.entry_id}.nursing"
    gone = "99999999-0000-4000-8000-000000000099"
    timer = {
        "object_id": "AB000000-0000-4000-8000-000000000001",
        "started": NOW.isoformat(),
        "side": "left",
        "side_started": NOW.isoformat(),
        "left_seconds": 0,
        "right_seconds": 0,
    }
    hass_storage[key] = {"version": 1, "minor_version": 1, "key": key, "data": {gone: timer}}
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.LOADED
    assert state(hass, NURSING_TIMER).state == "unknown"  # no timer runs for anyone
    assert gone not in hass_storage[key]["data"]
    assert await sync_interval(hass, server) == timedelta(minutes=5)


# Failed writes


async def test_the_pending_post_and_its_grace_window_survive_a_restart(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, freezer: Any
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
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, freezer: Any
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
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, freezer: Any
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


# undo


async def test_a_wet_press_bumps_todays_count_with_no_poll(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, freezer: Any
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


# --- sensors update at once -------------------------------------------------------------------


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
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, freezer: Any
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


# Setup


async def test_setup_loads_the_entry(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    assert init_integration.state is ConfigEntryState.LOADED


async def test_setup_backfills_each_device_from_the_start(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    assert server.reads_from_start() == [(DEVICE_A, 0)]
    assert hass.states.get(DIAPER).state != "unknown"


async def test_first_setup_pages_through_a_long_log_in_one_sync(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, server: FakeServer
) -> None:
    for number in range(1, 1201):
        server.add_record(
            DEVICE_A,
            "Diaper",
            f"D1000000-0000-4000-8000-{number:012d}",
            when=at(number),
            status=0,
        )
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.LOADED
    assert server.device_list_calls == 1
    assert [since for _, since in server.transaction_reads] == [0, 500, 1000]
    assert hass.states.get(DIAPER).state == at(1200).isoformat()


async def test_the_client_signs_in_as_its_own_device_named_home_assistant(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    sign_in = server.sign_ins[0]
    assert sign_in["device_name"] == "Home Assistant"
    assert sign_in["device_uuid"] == init_integration.data[CONF_DEVICE_UUID]


async def test_the_client_uses_its_own_http_session(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    session = server.clients[0].kwargs["session"]
    assert session is not async_get_clientsession(hass)
    assert not session.closed


async def test_a_connection_error_at_setup_retries_later(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, server: FakeServer
) -> None:
    server.sign_in_error = BabyTrackerConnectionError("down")
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.SETUP_RETRY
    assert server.open_clients == []
    assert all(client.kwargs["session"].closed for client in server.clients)


async def test_a_reply_in_the_wrong_shape_at_setup_retries_later(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, server: FakeServer
) -> None:
    server.request_error = ProtocolError("odd")
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert mock_config_entry.state is ConfigEntryState.SETUP_RETRY


async def test_setup_retries_and_then_loads_once_the_server_is_back(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, server: FakeServer
) -> None:
    server.sign_in_error = BabyTrackerConnectionError("down")
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert mock_config_entry.state is ConfigEntryState.SETUP_RETRY

    server.sign_in_error = None
    await tick(hass, minutes=2)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert mock_config_entry.state is ConfigEntryState.LOADED


async def test_a_rejected_login_at_setup_starts_reauth(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, server: FakeServer
) -> None:
    server.password = NEW_PASSWORD
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [flow["context"]["source"] for flow in flows] == ["reauth"]
    assert server.open_clients == []


# Sync


async def test_a_newer_record_from_another_device_is_fetched_by_its_own_log(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    server.add_record(DEVICE_B, "Diaper", "D2000000-0000-4000-8000-000000000001", when=at(60))
    await tick(hass)
    assert (DEVICE_B, 0) in server.transaction_reads
    assert hass.states.get(DIAPER).state == at(60).isoformat()


async def test_an_idle_sync_fetches_no_transactions(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    reads = len(server.transaction_reads)
    calls = server.device_list_calls
    await tick(hass)
    assert server.device_list_calls == calls + 1
    assert len(server.transaction_reads) == reads


async def test_a_sync_reads_only_what_is_newer_than_what_was_seen(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    server.add_record(DEVICE_A, "Diaper", "D3000000-0000-4000-8000-000000000001", when=at(30))
    await tick(hass)
    assert server.transaction_reads[-1] == (DEVICE_A, 2)


async def test_syncs_run_on_the_default_five_minute_interval(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    assert await sync_interval(hass, server) == timedelta(minutes=5)


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


# Errors while running


async def test_an_update_failure_makes_entities_unavailable_then_recovers(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    before = hass.states.get(DIAPER).state
    server.request_error = BabyTrackerConnectionError("down")
    await tick(hass)
    assert hass.states.get(DIAPER).state == "unavailable"
    assert init_integration.state is ConfigEntryState.LOADED

    server.request_error = None
    await tick(hass)
    assert hass.states.get(DIAPER).state == before


async def test_a_protocol_error_during_an_update_also_recovers(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    server.request_error = ProtocolError("odd")
    await tick(hass)
    assert hass.states.get(DIAPER).state == "unavailable"
    server.request_error = None
    await tick(hass)
    assert hass.states.get(DIAPER).state != "unavailable"


async def test_records_that_arrived_during_an_outage_show_up_after_it(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    server.request_error = BabyTrackerConnectionError("down")
    server.add_record(DEVICE_A, "Diaper", "D4000000-0000-4000-8000-000000000001", when=at(90))
    await tick(hass)
    server.request_error = None
    await tick(hass)
    assert hass.states.get(DIAPER).state == at(90).isoformat()


async def test_a_password_that_stops_working_starts_reauth(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    server.password = NEW_PASSWORD
    await tick(hass)

    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [flow["context"]["source"] for flow in flows] == ["reauth"]
    assert hass.states.get(DIAPER).state == "unavailable"


async def test_an_auth_error_raised_mid_update_starts_reauth(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    server.request_error = AuthenticationError("session rejected")
    await tick(hass)
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [flow["context"]["source"] for flow in flows] == ["reauth"]


# Persistence


async def test_the_synced_state_is_saved(
    hass: HomeAssistant, init_integration: MockConfigEntry, hass_storage: dict[str, Any]
) -> None:
    saved = hass_storage[_storage_key(init_integration)]["data"]
    assert saved["last_seen"] == {DEVICE_A: 2}
    assert len(saved["records"]) == 2


async def test_new_records_are_saved_after_a_sync(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    server: FakeServer,
    hass_storage: dict[str, Any],
) -> None:
    server.add_record(DEVICE_B, "Diaper", "D5000000-0000-4000-8000-000000000001", when=at(45))
    await tick(hass)
    saved = hass_storage[_storage_key(init_integration)]["data"]
    assert saved["last_seen"] == {DEVICE_A: 2, DEVICE_B: 1}


async def test_a_restart_loads_the_saved_state_and_does_not_reload_history(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    assert await hass.config_entries.async_unload(init_integration.entry_id)
    server.transaction_reads.clear()

    assert await hass.config_entries.async_setup(init_integration.entry_id)
    await hass.async_block_till_done()

    assert init_integration.state is ConfigEntryState.LOADED
    assert server.transaction_reads == []
    assert hass.states.get(DIAPER).state == at(0).isoformat()


async def test_a_restart_fetches_only_what_is_newer(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    assert await hass.config_entries.async_unload(init_integration.entry_id)
    server.add_record(DEVICE_A, "Diaper", "D6000000-0000-4000-8000-000000000001", when=at(120))
    server.transaction_reads.clear()

    assert await hass.config_entries.async_setup(init_integration.entry_id)
    await hass.async_block_till_done()

    assert server.transaction_reads == [(DEVICE_A, 2)]
    assert hass.states.get(DIAPER).state == at(120).isoformat()


@pytest.mark.parametrize(
    "corrupt",
    [
        {"version": 99, "last_seen": {}, "records": []},
        {"garbage": True},
        "not a mapping",
        {"version": 3, "last_seen": {DEVICE_A: 2}, "records": [{"payload": {"no": "id"}}]},
    ],
    ids=["unsupported-version", "missing-keys", "wrong-type", "bad-record"],
)
async def test_corrupt_storage_falls_back_to_a_fresh_backfill_with_a_warning(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    server: FakeServer,
    hass_storage: dict[str, Any],
    caplog: pytest.LogCaptureFixture,
    corrupt: Any,
) -> None:
    key = _storage_key(mock_config_entry)
    hass_storage[key] = {"version": 1, "minor_version": 1, "key": key, "data": corrupt}
    mock_config_entry.add_to_hass(hass)

    with caplog.at_level(logging.WARNING):
        await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.LOADED
    assert server.reads_from_start() == [(DEVICE_A, 0)]
    assert hass.states.get(DIAPER).state == at(0).isoformat()
    assert any(
        record.levelno == logging.WARNING and "saved sync state" in record.getMessage().lower()
        for record in caplog.records
    )
    assert hass_storage[key]["data"]["last_seen"] == {DEVICE_A: 2}  # replaced by a good copy


async def test_removing_the_entry_removes_its_saved_state(
    hass: HomeAssistant, init_integration: MockConfigEntry, hass_storage: dict[str, Any]
) -> None:
    key = _storage_key(init_integration)
    assert key in hass_storage

    await hass.config_entries.async_remove(init_integration.entry_id)
    await hass.async_block_till_done()
    assert key not in hass_storage


async def test_unloading_keeps_the_saved_state(
    hass: HomeAssistant, init_integration: MockConfigEntry, hass_storage: dict[str, Any]
) -> None:
    assert await hass.config_entries.async_unload(init_integration.entry_id)
    assert _storage_key(init_integration) in hass_storage


# Unloading


async def test_unloading_stops_syncing_and_frees_the_client(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    session = server.clients[0].kwargs["session"]

    assert await hass.config_entries.async_unload(init_integration.entry_id)
    await hass.async_block_till_done()

    assert init_integration.state is ConfigEntryState.NOT_LOADED
    assert server.open_clients == []
    assert session.closed

    calls = server.device_list_calls
    await tick(hass, minutes=30)
    assert server.device_list_calls == calls


async def test_entities_go_unavailable_on_unload(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    assert await hass.config_entries.async_unload(init_integration.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get(DIAPER).state == "unavailable"


async def test_an_entry_can_be_set_up_again_after_unloading(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    assert await hass.config_entries.async_unload(init_integration.entry_id)
    assert await hass.config_entries.async_setup(init_integration.entry_id)
    await hass.async_block_till_done()

    assert init_integration.state is ConfigEntryState.LOADED
    assert hass.states.get(DIAPER).state == at(0).isoformat()
    assert len(server.open_clients) == 1


async def test_repeated_reloads_leave_one_open_client(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    for _ in range(2):
        assert await hass.config_entries.async_reload(init_integration.entry_id)
        await hass.async_block_till_done()
    assert init_integration.state is ConfigEntryState.LOADED
    assert len(server.open_clients) == 1


# Records the integration doesn't show


async def test_a_deleted_record_from_another_device_is_dropped(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    server.add_record(
        DEVICE_B,
        "Diaper",
        "D7000000-0000-4000-8000-000000000001",
        when=at(200),
        baby=BABY_ONE,
    )
    await tick(hass)
    assert hass.states.get(DIAPER).state == at(200).isoformat()

    server.add_record(
        DEVICE_B,
        "Diaper",
        "D7000000-0000-4000-8000-000000000001",
        when=at(200),
        baby=BABY_ONE,
        op=OP_DELETE,
    )
    await tick(hass)
    assert hass.states.get(DIAPER).state == at(0).isoformat()


async def test_volumes_in_the_sync_format_do_not_break_a_sync(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    server.add_record(
        DEVICE_A,
        "Formula",
        "F8000000-0000-4000-8000-000000000001",
        when=at(15),
        amount=volume(4),
    )
    await tick(hass)
    assert init_integration.state is ConfigEntryState.LOADED
    assert hass.states.get("sensor.baby_one_last_feed").state == at(15).isoformat()


# Saved state from the older library


async def test_saved_state_from_the_earlier_library_version_still_loads(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    server: FakeServer,
    hass_storage: dict[str, Any],
) -> None:

    def saved(object_id: str, status: int, minutes: float, sync_id: int) -> dict[str, Any]:
        payload = record_payload(
            "Diaper", object_id, when=at(minutes), baby=BABY_ONE, status=status
        )
        stamp = at(minutes).timestamp()
        return {
            "version": {"timestamp": stamp, "rank": 0, "device": DEVICE_A, "sync_id": sync_id},
            "payload": payload,
        }

    key = _storage_key(mock_config_entry)
    state = {
        "version": 3,
        "last_seen": {DEVICE_A: 2},
        "records": [
            saved("D0000000-0000-4000-8000-000000000001", 0, 0, 1),
            saved("D0000000-0000-4000-8000-000000000002", 1, 10, 2),
        ],
        "tombstones": [],
    }
    hass_storage[key] = {"version": 1, "minor_version": 1, "key": key, "data": state}
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.LOADED
    assert server.transaction_reads == []
    assert hass.states.get(DIAPER).state == at(10).isoformat()


# A failing save must not hide the sync error


@pytest.mark.parametrize(
    ("error", "expected_state", "reauth"),
    [
        (BabyTrackerConnectionError("down"), ConfigEntryState.SETUP_RETRY, False),
        (AuthenticationError("rejected"), ConfigEntryState.SETUP_ERROR, True),
    ],
    ids=["connection", "auth"],
)
async def test_a_failing_save_does_not_replace_the_sync_error(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    server: FakeServer,
    caplog: pytest.LogCaptureFixture,
    error: Exception,
    expected_state: ConfigEntryState,
    reauth: bool,
) -> None:
    # Device A syncs (so there is progress to save), then device B fails.
    server.add_record(DEVICE_B, "Diaper", "D9000000-0000-4000-8000-000000000001", when=at(5))
    server.read_errors[DEVICE_B] = error
    mock_config_entry.add_to_hass(hass)

    with patch("homeassistant.helpers.storage.Store.async_save", side_effect=OSError("disk full")):
        await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

    assert mock_config_entry.state is expected_state
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert bool(flows) is reauth
    assert "disk full" in caplog.text


# The option an earlier version saved. It is no longer a setting, so these tests spell it out.


@pytest.mark.parametrize(
    "options",
    [{}, {LEGACY_INTERVAL: 1}, {LEGACY_INTERVAL: 15}, {LEGACY_INTERVAL: 1440}],
    ids=["no-options", "legacy-1-minute", "legacy-15-minutes", "legacy-a-day"],
)
async def test_polling_is_five_minutes_whatever_an_old_install_saved(
    hass: HomeAssistant, server: FakeServer, options: dict[str, Any]
) -> None:
    await entry_with_options(hass, options)

    assert await sync_interval(hass, server) == timedelta(minutes=5)


async def test_a_legacy_interval_does_not_make_syncs_run_early(
    hass: HomeAssistant, server: FakeServer
) -> None:
    await entry_with_options(hass, {LEGACY_INTERVAL: 1})
    calls = server.device_list_calls

    await tick(hass, minutes=1)
    await tick(hass, minutes=2)
    assert server.device_list_calls == calls  # nothing synced before five minutes

    await tick(hass, minutes=5)
    assert server.device_list_calls == calls + 1


async def test_a_legacy_interval_does_not_make_syncs_slower(
    hass: HomeAssistant, server: FakeServer
) -> None:
    await entry_with_options(hass, {LEGACY_INTERVAL: 1440})
    calls = server.device_list_calls

    await tick(hass, minutes=5)

    assert server.device_list_calls == calls + 1


@pytest.mark.parametrize("options", [{}, {LEGACY_INTERVAL: 15}], ids=["new", "legacy"])
async def test_a_running_sleep_polls_every_minute_and_five_when_it_stops(
    hass: HomeAssistant, server: FakeServer, options: dict[str, Any], freezer: Any
) -> None:
    await entry_with_options(hass, options)

    await press(hass, SLEEP_BUTTON)
    assert await sync_interval(hass, server) == timedelta(minutes=1)

    freezer.tick(timedelta(minutes=3))
    await press(hass, SLEEP_BUTTON)
    assert await sync_interval(hass, server) == timedelta(minutes=5)


@pytest.mark.parametrize("options", [{}, {LEGACY_INTERVAL: 15}], ids=["new", "legacy"])
async def test_a_running_nursing_timer_polls_every_minute_and_five_when_it_stops(
    hass: HomeAssistant, server: FakeServer, options: dict[str, Any], freezer: Any
) -> None:
    await entry_with_options(hass, options)

    await press(hass, NURSE_LEFT)
    assert await sync_interval(hass, server) == timedelta(minutes=1)

    freezer.tick(timedelta(minutes=3))
    await press(hass, NURSE_LEFT)
    assert await sync_interval(hass, server) == timedelta(minutes=5)


async def test_a_running_timer_syncs_every_minute(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    await press(hass, SLEEP_BUTTON)
    calls = server.device_list_calls

    await tick(hass, minutes=1)

    assert server.device_list_calls == calls + 1


# The options flow


async def test_no_update_listener_is_left_to_apply_an_interval(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    assert not init_integration.update_listeners


async def test_an_entry_from_before_the_migration_is_bumped_and_cleaned(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    mock_config_entry.add_to_hass(hass)
    assert (mock_config_entry.version, mock_config_entry.minor_version) == (1, 1)
    old = add_old_pump_entity(hass, mock_config_entry, BABY_ONE_ID, "Baby One")

    await _setup_entry(hass, mock_config_entry)

    assert mock_config_entry.state is ConfigEntryState.LOADED
    assert (mock_config_entry.version, mock_config_entry.minor_version) == (1, 2)
    assert er.async_get(hass).async_get(old.entity_id) is None


async def test_the_cleanup_runs_only_once(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    mock_config_entry.add_to_hass(hass)
    await _setup_entry(hass, mock_config_entry)
    assert mock_config_entry.minor_version == 2

    # Something that looks like the old entity turns up later: setup must leave it alone.
    later = add_old_pump_entity(hass, mock_config_entry, BABY_TWO_ID, "Baby Two")
    assert await hass.config_entries.async_reload(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert er.async_get(hass).async_get(later.entity_id) is not None


async def test_the_migration_is_idempotent_when_the_cleanup_already_ran(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """The live install already ran the cleanup on setup, so nothing is left to remove."""
    mock_config_entry.add_to_hass(hass)
    await _setup_entry(hass, mock_config_entry)
    registry = er.async_get(hass)
    before = {
        e.entity_id for e in er.async_entries_for_config_entry(registry, mock_config_entry.entry_id)
    }

    # Put the entry back at the old minor version, as a live install that has run the cleanup is.
    await hass.config_entries.async_unload(mock_config_entry.entry_id)
    hass.config_entries.async_update_entry(mock_config_entry, minor_version=1)
    await _setup_entry(hass, mock_config_entry)

    assert mock_config_entry.state is ConfigEntryState.LOADED
    assert mock_config_entry.minor_version == 2
    after = {
        e.entity_id for e in er.async_entries_for_config_entry(registry, mock_config_entry.entry_id)
    }
    assert after == before


async def test_the_migration_keeps_the_accounts_own_last_pump_entity(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    mock_config_entry.add_to_hass(hass)
    await _setup_entry(hass, mock_config_entry)
    registry = er.async_get(hass)
    account = registry.async_get_entity_id(
        "sensor", DOMAIN, f"{mock_config_entry.entry_id}_last_pump"
    )
    assert account is not None

    await hass.config_entries.async_unload(mock_config_entry.entry_id)
    hass.config_entries.async_update_entry(mock_config_entry, minor_version=1)
    await _setup_entry(hass, mock_config_entry)

    assert registry.async_get(account) is not None


async def test_the_migration_removes_every_old_entity_including_one_for_an_absent_baby(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    mock_config_entry.add_to_hass(hass)
    old = [
        add_old_pump_entity(hass, mock_config_entry, baby_id, name)
        for baby_id, name in (
            (BABY_ONE_ID, "Baby One"),
            (BABY_TWO_ID, "Baby Two"),
            (BABY_THREE_ID, "Baby Three"),
        )
    ]
    await _setup_entry(hass, mock_config_entry)
    registry = er.async_get(hass)
    assert [registry.async_get(entity.entity_id) for entity in old] == [None, None, None]


async def test_the_migration_leaves_other_entries_entities_alone(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    other = MockConfigEntry(
        domain=DOMAIN,
        title="other",
        unique_id="other@example.com",
        minor_version=2,
        data=dict(mock_config_entry.data),
    )
    other.add_to_hass(hass)
    mock_config_entry.add_to_hass(hass)
    theirs = add_old_pump_entity(hass, other, BABY_THREE_ID, "Baby Three")

    await _setup_entry(hass, mock_config_entry)

    assert er.async_get(hass).async_get(theirs.entity_id) is not None
    assert dr.async_get(hass).async_get(theirs.device_id) is not None


async def test_an_entry_from_a_newer_version_fails_to_migrate(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    newer = MockConfigEntry(
        domain=DOMAIN, title=EMAIL, unique_id=EMAIL, version=2, data=dict(mock_config_entry.data)
    )
    newer.add_to_hass(hass)
    await _setup_entry(hass, newer)
    assert newer.state is ConfigEntryState.MIGRATION_ERROR


async def test_a_new_entry_is_created_at_the_current_minor_version(
    hass: HomeAssistant, server: FakeServer
) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_EMAIL: EMAIL, CONF_PASSWORD: PASSWORD}
    )
    await hass.async_block_till_done()
    assert (result["result"].version, result["result"].minor_version) == (1, 2)


async def test_a_baby_no_longer_in_the_account_can_be_removed(
    hass: HomeAssistant, init_integration: MockConfigEntry, hass_ws_client: WebSocketGenerator
) -> None:
    stale = dr.async_get(hass).async_get_or_create(
        config_entry_id=init_integration.entry_id, identifiers={(DOMAIN, GONE_ID)}, name="Gone"
    )

    response = await _remove(hass, hass_ws_client, init_integration, stale.id)

    assert response["success"], response
    assert dr.async_get(hass).async_get(stale.id) is None


async def test_a_baby_still_in_the_account_cannot_be_removed(
    hass: HomeAssistant, init_integration: MockConfigEntry, hass_ws_client: WebSocketGenerator
) -> None:
    present = _device(hass, init_integration, BABY_ONE_ID)

    response = await _remove(hass, hass_ws_client, init_integration, present.id)

    assert not response["success"]
    assert dr.async_get(hass).async_get(present.id) is not None


async def test_the_account_device_cannot_be_removed(
    hass: HomeAssistant, init_integration: MockConfigEntry, hass_ws_client: WebSocketGenerator
) -> None:
    account = _device(hass, init_integration, f"{init_integration.entry_id}_account")

    response = await _remove(hass, hass_ws_client, init_integration, account.id)

    assert not response["success"]
    assert dr.async_get(hass).async_get(account.id) is not None


async def test_removing_a_stale_baby_leaves_the_others_alone(
    hass: HomeAssistant, init_integration: MockConfigEntry, hass_ws_client: WebSocketGenerator
) -> None:
    registry = dr.async_get(hass)
    stale = registry.async_get_or_create(
        config_entry_id=init_integration.entry_id,
        identifiers={(DOMAIN, BABY_THREE_ID)},
        name="Three",
    )
    before = {d.id for d in dr.async_entries_for_config_entry(registry, init_integration.entry_id)}

    response = await _remove(hass, hass_ws_client, init_integration, stale.id)

    assert response["success"], response
    after = {d.id for d in dr.async_entries_for_config_entry(registry, init_integration.entry_id)}
    assert after == before - {stale.id}


# Errors carry translation keys


async def test_a_failed_first_sync_is_reported_with_a_translation_key(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, server: FakeServer
) -> None:
    server.request_error = BabyTrackerConnectionError("offline")
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.SETUP_RETRY
    assert mock_config_entry.error_reason_translation_key == "sync_failed_update"
    assert "offline" in mock_config_entry.error_reason_translation_placeholders["error"]


async def test_a_failed_later_sync_raises_a_translated_update_failure(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    server: FakeServer,
    caplog: pytest.LogCaptureFixture,
) -> None:
    server.request_error = BabyTrackerConnectionError("offline")
    await tick(hass)

    assert hass.states.get(DIAPER).state == STATE_UNAVAILABLE
    # The message is the translated one, not the key.
    assert "Couldn't sync with the service: offline" in caplog.text


async def test_a_rejected_login_is_reported_with_a_translation_key(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, server: FakeServer
) -> None:
    server.password = "other-password"
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.SETUP_ERROR
    assert mock_config_entry.error_reason_translation_key == "auth_failed"


# Constants and registry cleanup helpers


async def test_a_failed_setup_closes_the_client(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, server: FakeServer
) -> None:
    """Regression guard for the move to ``entry.async_on_unload(client.close)``."""
    server.request_error = BabyTrackerConnectionError("offline")
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert server.clients
    assert server.open_clients == []


async def test_unloading_closes_the_client(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    assert len(server.open_clients) == 1
    await hass.config_entries.async_unload(init_integration.entry_id)
    await hass.async_block_till_done()
    assert server.open_clients == []
