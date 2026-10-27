"""Core PR 1: setup, sync, storage, unload, and translated errors."""

import logging
from datetime import timedelta
from typing import Any
from unittest.mock import patch

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.nighp_baby_tracker.const import (
    CONF_DEVICE_UUID,
    DOMAIN,
)
from custom_components.nighp_baby_tracker.pybabytracker import (
    BabyTrackerConnectionError,
    ProtocolError,
)

from . import (
    BABY_ONE,  # HACS-only: the engine tests
    DIAPER,
    NOW,
    sync_interval,
    tick,
)
from .factories import (
    DEVICE_A,
    DEVICE_B,
    OP_DELETE,  # HACS-only: the engine tests
    at,
    record_payload,  # HACS-only: the engine tests
    volume,  # HACS-only: the engine tests
)
from .fake_server import FakeServer


@pytest.fixture(autouse=True)
def _start_at_now(freezer: FrozenDateTimeFactory) -> None:
    """Freeze the clock at NOW before anything is set up.

    Timers scheduled at setup then share the clock the tests move, so moving it forward and
    ticking reaches them the way it does in core's tests.
    """
    freezer.move_to(NOW)


def _storage_key(entry: MockConfigEntry) -> str:
    return f"{DOMAIN}.{entry.entry_id}"


async def test_setup_loads_the_entry(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """Test the entry loads."""
    assert init_integration.state is ConfigEntryState.LOADED


async def test_setup_backfills_each_device_from_the_start(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    """Test the first setup reads each device's log from the start."""
    assert server.reads_from_start() == [(DEVICE_A, 0)]
    assert hass.states.get(DIAPER).state != "unknown"


@pytest.mark.engine
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
    """Test the client signs in as the entry's own device."""
    sign_in = server.sign_ins[0]
    assert sign_in["device_name"] == "Home Assistant"
    assert sign_in["device_uuid"] == init_integration.data[CONF_DEVICE_UUID]


async def test_the_client_uses_its_own_http_session(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    """Test the client gets an HTTP session of its own."""
    session = server.clients[0].kwargs["session"]
    assert session is not async_get_clientsession(hass)
    assert not session.closed


async def test_a_connection_error_at_setup_retries_later(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, server: FakeServer
) -> None:
    """Test a connection error at setup retries, and leaves no client open."""
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
    """Test a reply the library can't read at setup retries."""
    server.request_error = ProtocolError("odd")
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert mock_config_entry.state is ConfigEntryState.SETUP_RETRY


async def test_setup_retries_and_then_loads_once_the_server_is_back(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, server: FakeServer
) -> None:
    """Test setup loads on a retry once the service is back."""
    server.sign_in_error = BabyTrackerConnectionError("down")
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert mock_config_entry.state is ConfigEntryState.SETUP_RETRY

    server.sign_in_error = None
    await tick(hass, minutes=2)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert mock_config_entry.state is ConfigEntryState.LOADED


async def test_a_newer_record_from_another_device_is_fetched_by_its_own_log(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    """Test a new device's log is read when it appears."""
    server.add_record(DEVICE_B, "Diaper", "D2000000-0000-4000-8000-000000000001", when=at(60))
    await tick(hass)
    assert (DEVICE_B, 0) in server.transaction_reads
    assert hass.states.get(DIAPER).state == at(60).isoformat()


async def test_an_idle_sync_fetches_no_transactions(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    """Test a sync with nothing new reads no logs."""
    reads = len(server.transaction_reads)
    calls = server.device_list_calls
    await tick(hass)
    assert server.device_list_calls == calls + 1
    assert len(server.transaction_reads) == reads


async def test_a_sync_reads_only_what_is_newer_than_what_was_seen(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    """Test a sync reads a log only past what it has seen."""
    server.add_record(DEVICE_A, "Diaper", "D3000000-0000-4000-8000-000000000001", when=at(30))
    await tick(hass)
    assert server.transaction_reads[-1] == (DEVICE_A, 2)


async def test_syncs_run_on_the_default_five_minute_interval(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    """Test the account syncs every five minutes."""
    assert await sync_interval(hass, server) == timedelta(minutes=5)


async def test_an_update_failure_makes_entities_unavailable_then_recovers(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    """Test a failed sync makes the entities unavailable until one works."""
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
    """Test the entities recover after a reply the library can't read."""
    server.request_error = ProtocolError("odd")
    await tick(hass)
    assert hass.states.get(DIAPER).state == "unavailable"
    server.request_error = None
    await tick(hass)
    assert hass.states.get(DIAPER).state != "unavailable"


async def test_records_that_arrived_during_an_outage_show_up_after_it(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    """Test records logged during an outage show once a sync works."""
    server.request_error = BabyTrackerConnectionError("down")
    server.add_record(DEVICE_A, "Diaper", "D4000000-0000-4000-8000-000000000001", when=at(90))
    await tick(hass)
    server.request_error = None
    await tick(hass)
    assert hass.states.get(DIAPER).state == at(90).isoformat()


async def test_the_synced_state_is_saved(
    hass: HomeAssistant, init_integration: MockConfigEntry, hass_storage: dict[str, Any]
) -> None:
    """Test the sync state is saved."""
    saved = hass_storage[_storage_key(init_integration)]["data"]
    assert saved["last_seen"] == {DEVICE_A: 2}
    assert len(saved["records"]) == 2


async def test_new_records_are_saved_after_a_sync(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    server: FakeServer,
    hass_storage: dict[str, Any],
) -> None:
    """Test a sync that finds new records saves them."""
    server.add_record(DEVICE_B, "Diaper", "D5000000-0000-4000-8000-000000000001", when=at(45))
    await tick(hass)
    saved = hass_storage[_storage_key(init_integration)]["data"]
    assert saved["last_seen"] == {DEVICE_A: 2, DEVICE_B: 1}


async def test_a_restart_loads_the_saved_state_and_does_not_reload_history(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    """Test a restart loads the saved state instead of every log."""
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
    """Test a restart reads only what is newer than the saved state."""
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
    corrupt: object,
) -> None:
    """Test a saved state that can't be read is replaced by a fresh backfill, with a warning."""
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


async def test_a_saved_state_the_store_cannot_load_falls_back_to_a_fresh_backfill(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    server: FakeServer,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test a store that fails to load leads to a fresh backfill, with a warning."""
    mock_config_entry.add_to_hass(hass)

    with (
        patch(
            "homeassistant.helpers.storage.Store.async_load",
            side_effect=HomeAssistantError("unreadable"),
        ),
        caplog.at_level(logging.WARNING),
    ):
        await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.LOADED
    assert server.reads_from_start() == [(DEVICE_A, 0)]
    assert hass.states.get(DIAPER).state == at(0).isoformat()
    assert "saved sync state couldn't be read (unreadable)" in caplog.text


async def test_removing_the_entry_removes_its_saved_state(
    hass: HomeAssistant, init_integration: MockConfigEntry, hass_storage: dict[str, Any]
) -> None:
    """Test removing the entry deletes its saved state."""
    key = _storage_key(init_integration)
    assert key in hass_storage

    await hass.config_entries.async_remove(init_integration.entry_id)
    await hass.async_block_till_done()
    assert key not in hass_storage


async def test_unloading_keeps_the_saved_state(
    hass: HomeAssistant, init_integration: MockConfigEntry, hass_storage: dict[str, Any]
) -> None:
    """Test unloading the entry keeps its saved state."""
    assert await hass.config_entries.async_unload(init_integration.entry_id)
    assert _storage_key(init_integration) in hass_storage


async def test_unloading_stops_syncing_and_frees_the_client(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    """Test unloading stops the syncs and closes the client and its session."""
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
    """Test the entities are unavailable once the entry unloads."""
    assert await hass.config_entries.async_unload(init_integration.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get(DIAPER).state == "unavailable"


async def test_an_entry_can_be_set_up_again_after_unloading(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    """Test an unloaded entry sets up again."""
    assert await hass.config_entries.async_unload(init_integration.entry_id)
    assert await hass.config_entries.async_setup(init_integration.entry_id)
    await hass.async_block_till_done()

    assert init_integration.state is ConfigEntryState.LOADED
    assert hass.states.get(DIAPER).state == at(0).isoformat()
    assert len(server.open_clients) == 1


async def test_repeated_reloads_leave_one_open_client(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    """Test reloads leave exactly one client open."""
    for _ in range(2):
        assert await hass.config_entries.async_reload(init_integration.entry_id)
        await hass.async_block_till_done()
    assert init_integration.state is ConfigEntryState.LOADED
    assert len(server.open_clients) == 1


@pytest.mark.engine
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


@pytest.mark.engine
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


@pytest.mark.engine
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


@pytest.mark.parametrize(
    ("error", "expected_state"),
    [(BabyTrackerConnectionError("down"), ConfigEntryState.SETUP_RETRY)],
    ids=["connection"],
)
async def test_a_failing_save_does_not_replace_the_sync_error(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    server: FakeServer,
    caplog: pytest.LogCaptureFixture,
    error: Exception,
    expected_state: ConfigEntryState,
) -> None:
    # Device A syncs (so there is progress to save), then device B fails.
    """Test a failed save is logged and the sync's error still decides the state."""
    server.add_record(DEVICE_B, "Diaper", "D9000000-0000-4000-8000-000000000001", when=at(5))
    server.read_errors[DEVICE_B] = error
    mock_config_entry.add_to_hass(hass)

    with patch("homeassistant.helpers.storage.Store.async_save", side_effect=OSError("disk full")):
        await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

    assert mock_config_entry.state is expected_state
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert not flows
    assert "disk full" in caplog.text


async def test_a_failed_first_sync_is_reported_with_a_translation_key(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, server: FakeServer
) -> None:
    """Test a failed first sync gives the entry a translated reason."""
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
    """Test a failed later sync logs the translated message."""
    server.request_error = BabyTrackerConnectionError("offline")
    await tick(hass)

    assert hass.states.get(DIAPER).state == STATE_UNAVAILABLE
    # The message is the translated one, not the key.
    assert "Couldn't sync with the service: offline" in caplog.text


async def test_a_rejected_login_is_reported_with_a_translation_key(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, server: FakeServer
) -> None:
    """Test a rejected login fails setup with a translated reason."""
    server.password = "other-password"
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.SETUP_ERROR
    assert mock_config_entry.error_reason_translation_key == "auth_failed"


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
    """Test unloading closes the client."""
    assert len(server.open_clients) == 1
    await hass.config_entries.async_unload(init_integration.entry_id)
    await hass.async_block_till_done()
    assert server.open_clients == []
