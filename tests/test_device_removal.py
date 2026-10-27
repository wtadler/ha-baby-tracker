"""Removing a baby's device: only once a sync shows the baby has left the account.

A deleted baby shows up in the sync data as a ``Baby`` record deleted (opcode 2), usually with
the baby's older records still in the logs and still embedding it. These tests drive that
through the fake sync server and Home Assistant's own device-removal command.
"""

from typing import Any
from unittest.mock import patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
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


async def test_a_deleted_baby_can_be_removed_on_the_next_sync_without_a_reload(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    server: FakeServer,
    hass_ws_client: WebSocketGenerator,
) -> None:
    # The app deletes the baby but the baby's older diaper still embeds it.
    server.add_payload(DEVICE_B, BABY_TWO, op=OP_DELETE)
    await tick(hass)

    assert await remove_device(
        hass, hass_ws_client, init_integration, baby_device(hass, BABY_TWO_ID)
    )
    assert get_baby_device(dr.async_get(hass), BABY_TWO_ID) is None
    assert init_integration.state is ConfigEntryState.LOADED


async def test_a_baby_record_that_was_added_and_then_deleted_can_be_removed(
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

    assert await remove_device(
        hass, hass_ws_client, init_integration, baby_device(hass, BABY_TWO_ID)
    )


async def test_a_deleted_baby_is_removable_even_when_all_its_records_remain(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    server: FakeServer,
    hass_ws_client: WebSocketGenerator,
) -> None:
    server.add_record(
        DEVICE_A, "Diaper", "D0000000-0000-4000-8000-000000000022", when=at(20), baby=BABY_TWO
    )
    server.add_payload(DEVICE_A, BABY_TWO, op=OP_DELETE)
    await tick(hass)

    assert await remove_device(
        hass, hass_ws_client, init_integration, baby_device(hass, BABY_TWO_ID)
    )


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


async def test_a_deleted_baby_stays_removable_after_a_reload(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    server: FakeServer,
    hass_ws_client: WebSocketGenerator,
) -> None:
    server.add_payload(DEVICE_B, BABY_TWO, op=OP_DELETE)
    await tick(hass)
    assert await hass.config_entries.async_reload(init_integration.entry_id)
    await hass.async_block_till_done()

    assert BABY_TWO_ID not in init_integration.runtime_data.data.babies
    assert await remove_device(
        hass, hass_ws_client, init_integration, baby_device(hass, BABY_TWO_ID)
    )


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
    server.add_payload(DEVICE_B, BABY_TWO, op=OP_DELETE)
    await tick(hass)
    device = baby_device(hass, BABY_TWO_ID)
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
