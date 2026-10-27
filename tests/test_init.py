"""Setup, sync, persistence, errors, and unloading."""

import asyncio
import logging
from datetime import timedelta
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.nighp_baby_tracker.const import (
    CONF_DEVICE_UUID,
    CONF_SCAN_INTERVAL,
    DOMAIN,
)
from custom_components.nighp_baby_tracker.pybabytracker import (
    AuthenticationError,
    BabyTrackerConnectionError,
    ProtocolError,
)

from .conftest import BABY_ONE, NEW_PASSWORD, FakeServer
from .factories import DEVICE_A, DEVICE_B, OP_DELETE, at, volume
from .helpers import tick

DIAPER = "sensor.baby_one_last_diaper"


def _storage_key(entry: MockConfigEntry) -> str:
    return f"{DOMAIN}.{entry.entry_id}"


# Setup


async def test_setup_loads_the_entry(hass: HomeAssistant, setup_entry: MockConfigEntry) -> None:
    assert setup_entry.state is ConfigEntryState.LOADED


async def test_setup_backfills_each_device_from_the_start(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer
) -> None:
    assert server.reads_from_start() == [(DEVICE_A, 0)]
    assert hass.states.get(DIAPER).state != "unknown"


async def test_first_setup_pages_through_a_long_log_in_one_sync(
    hass: HomeAssistant, config_entry: MockConfigEntry, server: FakeServer
) -> None:
    for number in range(1, 1201):
        server.add_record(
            DEVICE_A,
            "Diaper",
            f"D1000000-0000-4000-8000-{number:012d}",
            when=at(number),
            status=0,
        )
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    assert config_entry.state is ConfigEntryState.LOADED
    assert server.device_list_calls == 1
    assert [since for _, since in server.transaction_reads] == [0, 500, 1000]
    assert hass.states.get(DIAPER).state == at(1200).isoformat()


async def test_the_client_signs_in_as_its_own_device_named_home_assistant(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer
) -> None:
    sign_in = server.sign_ins[0]
    assert sign_in["device_name"] == "Home Assistant"
    assert sign_in["device_uuid"] == setup_entry.data[CONF_DEVICE_UUID]


async def test_the_client_uses_its_own_http_session(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer
) -> None:
    session = server.clients[0].kwargs["session"]
    assert session is not async_get_clientsession(hass)
    assert not session.closed


async def test_a_connection_error_at_setup_retries_later(
    hass: HomeAssistant, config_entry: MockConfigEntry, server: FakeServer
) -> None:
    server.sign_in_error = BabyTrackerConnectionError("down")
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    assert config_entry.state is ConfigEntryState.SETUP_RETRY
    assert server.open_clients == []
    assert all(client.kwargs["session"].closed for client in server.clients)


async def test_a_reply_in_the_wrong_shape_at_setup_retries_later(
    hass: HomeAssistant, config_entry: MockConfigEntry, server: FakeServer
) -> None:
    server.request_error = ProtocolError("odd")
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.SETUP_RETRY


async def test_setup_retries_and_then_loads_once_the_server_is_back(
    hass: HomeAssistant, config_entry: MockConfigEntry, server: FakeServer
) -> None:
    server.sign_in_error = BabyTrackerConnectionError("down")
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.SETUP_RETRY

    server.sign_in_error = None
    await tick(hass, minutes=2)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert config_entry.state is ConfigEntryState.LOADED


async def test_a_rejected_login_at_setup_starts_reauth(
    hass: HomeAssistant, config_entry: MockConfigEntry, server: FakeServer
) -> None:
    server.password = NEW_PASSWORD
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    assert config_entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [flow["context"]["source"] for flow in flows] == ["reauth"]
    assert server.open_clients == []


# Sync


async def test_a_newer_record_from_another_device_is_fetched_by_its_own_log(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer
) -> None:
    server.add_record(DEVICE_B, "Diaper", "D2000000-0000-4000-8000-000000000001", when=at(60))
    await tick(hass)
    assert (DEVICE_B, 0) in server.transaction_reads
    assert hass.states.get(DIAPER).state == at(60).isoformat()


async def test_an_idle_sync_fetches_no_transactions(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer
) -> None:
    reads = len(server.transaction_reads)
    calls = server.device_list_calls
    await tick(hass)
    assert server.device_list_calls == calls + 1
    assert len(server.transaction_reads) == reads


async def test_a_sync_reads_only_what_is_newer_than_what_was_seen(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer
) -> None:
    server.add_record(DEVICE_A, "Diaper", "D3000000-0000-4000-8000-000000000001", when=at(30))
    await tick(hass)
    assert server.transaction_reads[-1] == (DEVICE_A, 2)


async def test_syncs_run_on_the_default_five_minute_interval(
    hass: HomeAssistant, setup_entry: MockConfigEntry
) -> None:
    assert setup_entry.runtime_data.update_interval == timedelta(minutes=5)


async def test_syncs_run_on_the_configured_interval(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(config_entry, options={CONF_SCAN_INTERVAL: 2})
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.runtime_data.update_interval == timedelta(minutes=2)


async def test_an_options_change_applies_without_a_reload(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer
) -> None:
    coordinator = setup_entry.runtime_data
    clients = len(server.clients)

    result = await hass.config_entries.options.async_init(setup_entry.entry_id)
    await hass.config_entries.options.async_configure(result["flow_id"], {CONF_SCAN_INTERVAL: 10})
    await hass.async_block_till_done()

    assert setup_entry.state is ConfigEntryState.LOADED
    assert setup_entry.runtime_data is coordinator
    assert coordinator.update_interval == timedelta(minutes=10)
    assert len(server.clients) == clients


async def test_a_shorter_interval_takes_effect_right_away(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer
) -> None:
    result = await hass.config_entries.options.async_init(setup_entry.entry_id)
    await hass.config_entries.options.async_configure(result["flow_id"], {CONF_SCAN_INTERVAL: 1})
    await hass.async_block_till_done()
    calls = server.device_list_calls

    await tick(hass, minutes=1)  # well before the old five-minute schedule
    assert server.device_list_calls == calls + 1


async def test_engine_use_is_serialized_behind_one_lock(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer
) -> None:
    coordinator = setup_entry.runtime_data
    calls = server.device_list_calls

    async with coordinator.lock:
        refresh = asyncio.create_task(coordinator.async_refresh())
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert server.device_list_calls == calls  # waiting on the lock
        assert not refresh.done()

    await refresh
    assert server.device_list_calls == calls + 1


# Errors while running


async def test_an_update_failure_makes_entities_unavailable_then_recovers(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer
) -> None:
    before = hass.states.get(DIAPER).state
    server.request_error = BabyTrackerConnectionError("down")
    await tick(hass)
    assert hass.states.get(DIAPER).state == "unavailable"
    assert setup_entry.state is ConfigEntryState.LOADED

    server.request_error = None
    await tick(hass)
    assert hass.states.get(DIAPER).state == before


async def test_a_protocol_error_during_an_update_also_recovers(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer
) -> None:
    server.request_error = ProtocolError("odd")
    await tick(hass)
    assert hass.states.get(DIAPER).state == "unavailable"
    server.request_error = None
    await tick(hass)
    assert hass.states.get(DIAPER).state != "unavailable"


async def test_records_that_arrived_during_an_outage_show_up_after_it(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer
) -> None:
    server.request_error = BabyTrackerConnectionError("down")
    server.add_record(DEVICE_A, "Diaper", "D4000000-0000-4000-8000-000000000001", when=at(90))
    await tick(hass)
    server.request_error = None
    await tick(hass)
    assert hass.states.get(DIAPER).state == at(90).isoformat()


async def test_a_password_that_stops_working_starts_reauth(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer
) -> None:
    server.password = NEW_PASSWORD
    await tick(hass)

    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [flow["context"]["source"] for flow in flows] == ["reauth"]
    assert hass.states.get(DIAPER).state == "unavailable"


async def test_an_auth_error_raised_mid_update_starts_reauth(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer
) -> None:
    server.request_error = AuthenticationError("session rejected")
    await tick(hass)
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [flow["context"]["source"] for flow in flows] == ["reauth"]


# Persistence


async def test_the_synced_state_is_saved(
    hass: HomeAssistant, setup_entry: MockConfigEntry, hass_storage: dict[str, Any]
) -> None:
    saved = hass_storage[_storage_key(setup_entry)]["data"]
    assert saved["last_seen"] == {DEVICE_A: 2}
    assert len(saved["records"]) == 2


async def test_new_records_are_saved_after_a_sync(
    hass: HomeAssistant,
    setup_entry: MockConfigEntry,
    server: FakeServer,
    hass_storage: dict[str, Any],
) -> None:
    server.add_record(DEVICE_B, "Diaper", "D5000000-0000-4000-8000-000000000001", when=at(45))
    await tick(hass)
    saved = hass_storage[_storage_key(setup_entry)]["data"]
    assert saved["last_seen"] == {DEVICE_A: 2, DEVICE_B: 1}


async def test_a_restart_loads_the_saved_state_and_does_not_reload_history(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer
) -> None:
    assert await hass.config_entries.async_unload(setup_entry.entry_id)
    server.transaction_reads.clear()

    assert await hass.config_entries.async_setup(setup_entry.entry_id)
    await hass.async_block_till_done()

    assert setup_entry.state is ConfigEntryState.LOADED
    assert server.transaction_reads == []
    assert hass.states.get(DIAPER).state == at(0).isoformat()


async def test_a_restart_fetches_only_what_is_newer(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer
) -> None:
    assert await hass.config_entries.async_unload(setup_entry.entry_id)
    server.add_record(DEVICE_A, "Diaper", "D6000000-0000-4000-8000-000000000001", when=at(120))
    server.transaction_reads.clear()

    assert await hass.config_entries.async_setup(setup_entry.entry_id)
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
    config_entry: MockConfigEntry,
    server: FakeServer,
    hass_storage: dict[str, Any],
    caplog: pytest.LogCaptureFixture,
    corrupt: Any,
) -> None:
    key = _storage_key(config_entry)
    hass_storage[key] = {"version": 1, "minor_version": 1, "key": key, "data": corrupt}
    config_entry.add_to_hass(hass)

    with caplog.at_level(logging.WARNING):
        await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()

    assert config_entry.state is ConfigEntryState.LOADED
    assert server.reads_from_start() == [(DEVICE_A, 0)]
    assert hass.states.get(DIAPER).state == at(0).isoformat()
    assert any(
        record.levelno == logging.WARNING and "saved sync state" in record.getMessage().lower()
        for record in caplog.records
    )
    assert hass_storage[key]["data"]["last_seen"] == {DEVICE_A: 2}  # replaced by a good copy


async def test_removing_the_entry_removes_its_saved_state(
    hass: HomeAssistant, setup_entry: MockConfigEntry, hass_storage: dict[str, Any]
) -> None:
    key = _storage_key(setup_entry)
    assert key in hass_storage

    await hass.config_entries.async_remove(setup_entry.entry_id)
    await hass.async_block_till_done()
    assert key not in hass_storage


async def test_unloading_keeps_the_saved_state(
    hass: HomeAssistant, setup_entry: MockConfigEntry, hass_storage: dict[str, Any]
) -> None:
    assert await hass.config_entries.async_unload(setup_entry.entry_id)
    assert _storage_key(setup_entry) in hass_storage


# Unloading


async def test_unloading_stops_syncing_and_frees_the_client(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer
) -> None:
    session = server.clients[0].kwargs["session"]

    assert await hass.config_entries.async_unload(setup_entry.entry_id)
    await hass.async_block_till_done()

    assert setup_entry.state is ConfigEntryState.NOT_LOADED
    assert server.open_clients == []
    assert session.closed

    calls = server.device_list_calls
    await tick(hass, minutes=30)
    assert server.device_list_calls == calls


async def test_entities_go_unavailable_on_unload(
    hass: HomeAssistant, setup_entry: MockConfigEntry
) -> None:
    assert await hass.config_entries.async_unload(setup_entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get(DIAPER).state == "unavailable"


async def test_an_entry_can_be_set_up_again_after_unloading(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer
) -> None:
    assert await hass.config_entries.async_unload(setup_entry.entry_id)
    assert await hass.config_entries.async_setup(setup_entry.entry_id)
    await hass.async_block_till_done()

    assert setup_entry.state is ConfigEntryState.LOADED
    assert hass.states.get(DIAPER).state == at(0).isoformat()
    assert len(server.open_clients) == 1


async def test_repeated_reloads_leave_one_open_client(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer
) -> None:
    for _ in range(2):
        assert await hass.config_entries.async_reload(setup_entry.entry_id)
        await hass.async_block_till_done()
    assert setup_entry.state is ConfigEntryState.LOADED
    assert len(server.open_clients) == 1


# Records the integration doesn't show


async def test_a_deleted_record_from_another_device_is_dropped(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer
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
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer
) -> None:
    server.add_record(
        DEVICE_A,
        "Formula",
        "F8000000-0000-4000-8000-000000000001",
        when=at(15),
        amount=volume(4),
    )
    await tick(hass)
    assert setup_entry.state is ConfigEntryState.LOADED
    assert hass.states.get("sensor.baby_one_last_feed").state == at(15).isoformat()


# Saved state from the older library


async def test_saved_state_from_the_earlier_library_version_still_loads(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    server: FakeServer,
    hass_storage: dict[str, Any],
) -> None:
    from .factories import record_payload

    def saved(object_id: str, status: int, minutes: float, sync_id: int) -> dict[str, Any]:
        payload = record_payload(
            "Diaper", object_id, when=at(minutes), baby=BABY_ONE, status=status
        )
        stamp = at(minutes).timestamp()
        return {
            "version": {"timestamp": stamp, "rank": 0, "device": DEVICE_A, "sync_id": sync_id},
            "payload": payload,
        }

    key = _storage_key(config_entry)
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
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    assert config_entry.state is ConfigEntryState.LOADED
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
    config_entry: MockConfigEntry,
    server: FakeServer,
    caplog: pytest.LogCaptureFixture,
    error: Exception,
    expected_state: ConfigEntryState,
    reauth: bool,
) -> None:
    # Device A syncs (so there is progress to save), then device B fails.
    server.add_record(DEVICE_B, "Diaper", "D9000000-0000-4000-8000-000000000001", when=at(5))
    server.read_errors[DEVICE_B] = error
    config_entry.add_to_hass(hass)

    with patch("homeassistant.helpers.storage.Store.async_save", side_effect=OSError("disk full")):
        await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()

    assert config_entry.state is expected_state
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert bool(flows) is reauth
    assert "disk full" in caplog.text
