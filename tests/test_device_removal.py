"""Removing a baby's device: only once a sync shows the baby has left the account.

A deleted baby shows up in the sync data as a ``Baby`` record deleted (opcode 2), usually with
the baby's older records still in the logs and still embedding it. These tests drive that
through the fake sync server and Home Assistant's own device-removal command.
"""

import logging
from typing import Any
from unittest.mock import patch

import pytest

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.typing import WebSocketGenerator

from custom_components.nighp_baby_tracker.const import account_identifier
from custom_components.nighp_baby_tracker.pybabytracker import SyncEngine

from . import BABY_TWO
from .factories import (
    BABY_ONE_ID,
    BABY_TWO_ID,
    DEVICE_A,
    DEVICE_B,
    OP_ADD,
    OP_DELETE,
    at,
)
from .fake_server import FakeServer
from .helpers import get_baby_device, tick

BABY_TWO_DIAPER = "D0000000-0000-4000-8000-000000000002"  # from the server fixture


async def remove_device(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    entry: MockConfigEntry,
    device: dr.DeviceEntry,
) -> bool:
    """Ask Home Assistant to remove the device from the entry, as the device page does."""
    assert await async_setup_component(hass, "config", {})
    client = await hass_ws_client(hass)
    await client.send_json_auto_id(
        {
            "type": "config/device_registry/remove_config_entry",
            "config_entry_id": entry.entry_id,
            "device_id": device.id,
        }
    )
    response: dict[str, Any] = await client.receive_json()
    return bool(response["success"])


def baby_device(hass: HomeAssistant, baby_id: str) -> dr.DeviceEntry:
    device = get_baby_device(dr.async_get(hass), baby_id)
    assert device is not None
    return device


async def test_a_baby_still_in_the_account_cannot_be_removed(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    hass_ws_client: WebSocketGenerator,
) -> None:
    device = baby_device(hass, BABY_TWO_ID)

    assert not await remove_device(hass, hass_ws_client, init_integration, device)
    assert get_baby_device(dr.async_get(hass), BABY_TWO_ID) is not None


async def test_a_deleted_baby_is_removed_on_the_next_sync_without_a_reload(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    # The app deletes the baby but the baby's older diaper still embeds it.
    server.add_payload(DEVICE_B, BABY_TWO, op=OP_DELETE)
    await tick(hass)

    assert get_baby_device(dr.async_get(hass), BABY_TWO_ID) is None
    assert init_integration.state is ConfigEntryState.LOADED


async def test_a_baby_record_that_was_added_and_then_deleted_is_removed(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    server: FakeServer,
    hass_ws_client: WebSocketGenerator,
) -> None:
    server.add_payload(DEVICE_B, BABY_TWO, op=OP_ADD)
    await tick(hass)
    assert not await remove_device(
        hass, hass_ws_client, init_integration, baby_device(hass, BABY_TWO_ID)
    )

    server.add_payload(DEVICE_B, BABY_TWO, op=OP_DELETE)
    await tick(hass)

    assert get_baby_device(dr.async_get(hass), BABY_TWO_ID) is None


async def test_a_deleted_baby_is_removed_even_when_all_its_records_remain(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    server.add_record(
        DEVICE_A, "Diaper", "D0000000-0000-4000-8000-000000000022", when=at(20), baby=BABY_TWO
    )
    server.add_payload(DEVICE_A, BABY_TWO, op=OP_DELETE)
    await tick(hass)

    assert get_baby_device(dr.async_get(hass), BABY_TWO_ID) is None


async def test_deleting_one_baby_leaves_the_other_present_and_not_removable(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    server: FakeServer,
    hass_ws_client: WebSocketGenerator,
) -> None:
    server.add_payload(DEVICE_B, BABY_TWO, op=OP_DELETE)
    await tick(hass)

    assert not await remove_device(
        hass, hass_ws_client, init_integration, baby_device(hass, BABY_ONE_ID)
    )
    assert init_integration.runtime_data.data.babies.keys() == {BABY_ONE_ID}


async def test_a_deleted_baby_stays_out_of_the_data_after_more_syncs(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    server.add_payload(DEVICE_B, BABY_TWO, op=OP_DELETE)
    await tick(hass)
    server.add_record(DEVICE_A, "Diaper", "D0000000-0000-4000-8000-000000000023", when=at(30))
    await tick(hass)

    assert BABY_TWO_ID not in init_integration.runtime_data.data.babies


async def test_a_deleted_baby_stays_gone_after_a_reload(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    server.add_payload(DEVICE_B, BABY_TWO, op=OP_DELETE)
    await tick(hass)
    assert await hass.config_entries.async_reload(init_integration.entry_id)
    await hass.async_block_till_done()

    assert BABY_TWO_ID not in init_integration.runtime_data.data.babies
    assert get_baby_device(dr.async_get(hass), BABY_TWO_ID) is None


async def test_a_baby_added_again_after_its_delete_is_present_again(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    server: FakeServer,
    hass_ws_client: WebSocketGenerator,
) -> None:
    server.add_payload(DEVICE_B, BABY_TWO, op=OP_DELETE)
    await tick(hass)
    assert BABY_TWO_ID not in init_integration.runtime_data.data.babies

    revived = {**BABY_TWO, "timestamp": "2026-10-26 12:00:00 +0000"}
    server.add_payload(DEVICE_B, revived, op=OP_ADD)
    await tick(hass)

    assert BABY_TWO_ID in init_integration.runtime_data.data.babies
    assert not await remove_device(
        hass, hass_ws_client, init_integration, baby_device(hass, BABY_TWO_ID)
    )


async def test_a_baby_whose_records_were_all_deleted_is_still_present(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    server: FakeServer,
    hass_ws_client: WebSocketGenerator,
) -> None:
    """Without a Baby delete the sync data can't tell a removed baby from an empty one."""
    server.add_record(DEVICE_B, "Diaper", BABY_TWO_DIAPER, when=at(10), baby=BABY_TWO, op=OP_DELETE)
    await tick(hass)

    assert BABY_TWO_ID in init_integration.runtime_data.data.babies
    assert not await remove_device(
        hass, hass_ws_client, init_integration, baby_device(hass, BABY_TWO_ID)
    )


async def test_the_accounts_own_device_cannot_be_removed(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    hass_ws_client: WebSocketGenerator,
) -> None:
    registry = dr.async_get(hass)
    devices = registry.async_get_devices(
        identifiers={(init_integration.domain, account_identifier(init_integration.entry_id))}
    )
    assert len(devices) == 1
    device = devices[0]

    assert not await remove_device(hass, hass_ws_client, init_integration, device)


async def test_nothing_can_be_removed_while_the_entry_is_not_loaded(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    server: FakeServer,
    hass_ws_client: WebSocketGenerator,
) -> None:
    device = baby_device(hass, BABY_ONE_ID)
    assert await hass.config_entries.async_unload(init_integration.entry_id)

    assert not await remove_device(hass, hass_ws_client, init_integration, device)


async def test_a_sync_and_refresh_do_not_encode_the_engine_to_find_deleted_babies(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    server: FakeServer,
) -> None:
    server.add_payload(DEVICE_B, BABY_TWO, op=OP_DELETE)
    with patch.object(SyncEngine, "to_dict", side_effect=AssertionError("encoded the engine")):
        await init_integration.runtime_data.async_refresh()

    assert BABY_TWO_ID not in init_integration.runtime_data.data.babies


# --- removed automatically, as soon as a sync shows the baby is deleted ---------------------------


def entity_ids_on(hass: HomeAssistant, device: dr.DeviceEntry) -> set[str]:
    return {
        entry.entity_id
        for entry in er.async_entries_for_device(
            er.async_get(hass), device.id, include_disabled_entities=True
        )
    }


async def test_a_deleted_babys_device_and_entities_are_gone_after_the_next_refresh(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    device = baby_device(hass, BABY_TWO_ID)
    entity_ids = entity_ids_on(hass, device)
    assert entity_ids

    server.add_payload(DEVICE_B, BABY_TWO, op=OP_DELETE)
    await tick(hass)

    assert get_baby_device(dr.async_get(hass), BABY_TWO_ID) is None
    registry = er.async_get(hass)
    assert all(registry.async_get(entity_id) is None for entity_id in entity_ids)
    assert all(hass.states.get(entity_id) is None for entity_id in entity_ids)
    assert init_integration.state is ConfigEntryState.LOADED


async def test_a_present_baby_keeps_its_device_and_entities(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    device = baby_device(hass, BABY_ONE_ID)
    entity_ids = entity_ids_on(hass, device)

    server.add_payload(DEVICE_B, BABY_TWO, op=OP_DELETE)
    await tick(hass)

    assert baby_device(hass, BABY_ONE_ID).id == device.id
    assert entity_ids_on(hass, device) == entity_ids
    assert all(hass.states.get(entity_id) is not None for entity_id in entity_ids)


async def test_a_refresh_with_no_deletion_removes_nothing(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    before = {
        device.id
        for device in dr.async_entries_for_config_entry(
            dr.async_get(hass), init_integration.entry_id
        )
    }
    server.add_record(DEVICE_A, "Diaper", "D0000000-0000-4000-8000-000000000024", when=at(40))
    await tick(hass)

    after = {
        device.id
        for device in dr.async_entries_for_config_entry(
            dr.async_get(hass), init_integration.entry_id
        )
    }
    assert after == before


async def test_the_account_device_survives_a_baby_deletion(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    registry = dr.async_get(hass)
    identifier = (init_integration.domain, account_identifier(init_integration.entry_id))
    assert registry.async_get_devices(identifiers={identifier})

    server.add_payload(DEVICE_B, BABY_TWO, op=OP_DELETE)
    await tick(hass)

    accounts = registry.async_get_devices(identifiers={identifier})
    assert len(accounts) == 1
    assert entity_ids_on(hass, accounts[0])


async def test_a_baby_added_again_gets_its_device_and_entities_back(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    before = entity_ids_on(hass, baby_device(hass, BABY_TWO_ID))
    server.add_payload(DEVICE_B, BABY_TWO, op=OP_DELETE)
    await tick(hass)
    assert get_baby_device(dr.async_get(hass), BABY_TWO_ID) is None

    revived = {**BABY_TWO, "timestamp": "2026-10-26 12:00:00 +0000"}
    server.add_payload(DEVICE_B, revived, op=OP_ADD)
    await tick(hass)
    await hass.async_block_till_done()

    device = baby_device(hass, BABY_TWO_ID)
    assert device.identifiers == {(init_integration.domain, BABY_TWO_ID)}
    assert entity_ids_on(hass, device) == before
    assert all(hass.states.get(entity_id) is not None for entity_id in before)


async def test_a_deleted_baby_is_removed_on_a_write_sync_too(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    server.add_payload(DEVICE_B, BABY_TWO, op=OP_DELETE)
    await hass.services.async_call(
        "button",
        "press",
        {"entity_id": entity_ids_on_wet_button(hass)},
        blocking=True,
    )

    assert get_baby_device(dr.async_get(hass), BABY_TWO_ID) is None


def entity_ids_on_wet_button(hass: HomeAssistant) -> str:
    registry = er.async_get(hass)
    ids = [
        entry.entity_id
        for entry in er.async_entries_for_device(registry, baby_device(hass, BABY_ONE_ID).id)
        if entry.entity_id.startswith("button.") and "wet" in entry.entity_id
    ]
    assert len(ids) == 1
    return ids[0]


async def test_removing_a_deleted_baby_logs_no_warning_or_error(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    server: FakeServer,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.clear()
    with caplog.at_level(logging.DEBUG):
        server.add_payload(DEVICE_B, BABY_TWO, op=OP_DELETE)
        await tick(hass)

    assert get_baby_device(dr.async_get(hass), BABY_TWO_ID) is None
    noisy = [
        record.getMessage()
        for record in caplog.records
        if record.levelno >= logging.WARNING or record.name == "homeassistant.helpers.frame"
    ]
    assert noisy == []


async def test_a_corrupt_store_removes_no_device_until_a_real_delete_syncs(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    server: FakeServer,
    hass_storage: dict[str, Any],
) -> None:
    key = f"{init_integration.domain}.{init_integration.entry_id}"
    assert await hass.config_entries.async_unload(init_integration.entry_id)
    hass_storage[key] = {"version": 1, "minor_version": 1, "key": key, "data": "corrupt"}
    assert await hass.config_entries.async_setup(init_integration.entry_id)
    await hass.async_block_till_done()

    # The fresh engine backfilled the whole log: both babies are still on the account.
    assert init_integration.runtime_data.data.babies.keys() == {BABY_ONE_ID, BABY_TWO_ID}
    assert get_baby_device(dr.async_get(hass), BABY_ONE_ID) is not None
    assert get_baby_device(dr.async_get(hass), BABY_TWO_ID) is not None

    server.add_payload(DEVICE_B, BABY_TWO, op=OP_DELETE)
    await tick(hass)

    assert get_baby_device(dr.async_get(hass), BABY_ONE_ID) is not None
    assert get_baby_device(dr.async_get(hass), BABY_TWO_ID) is None
