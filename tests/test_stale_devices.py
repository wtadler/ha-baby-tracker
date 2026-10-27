"""A baby that has left the account can be removed from the device page; a present one can't."""

from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.typing import WebSocketGenerator

from custom_components.nighp_baby_tracker.const import DOMAIN

from .factories import BABY_ONE_ID, BABY_THREE_ID

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


async def test_a_baby_no_longer_in_the_account_can_be_removed(
    hass: HomeAssistant, setup_entry: MockConfigEntry, hass_ws_client: WebSocketGenerator
) -> None:
    stale = dr.async_get(hass).async_get_or_create(
        config_entry_id=setup_entry.entry_id, identifiers={(DOMAIN, GONE_ID)}, name="Gone"
    )

    response = await _remove(hass, hass_ws_client, setup_entry, stale.id)

    assert response["success"], response
    assert dr.async_get(hass).async_get(stale.id) is None


async def test_a_baby_still_in_the_account_cannot_be_removed(
    hass: HomeAssistant, setup_entry: MockConfigEntry, hass_ws_client: WebSocketGenerator
) -> None:
    present = _device(hass, setup_entry, BABY_ONE_ID)

    response = await _remove(hass, hass_ws_client, setup_entry, present.id)

    assert not response["success"]
    assert dr.async_get(hass).async_get(present.id) is not None


async def test_the_account_device_cannot_be_removed(
    hass: HomeAssistant, setup_entry: MockConfigEntry, hass_ws_client: WebSocketGenerator
) -> None:
    account = _device(hass, setup_entry, f"{setup_entry.entry_id}_account")

    response = await _remove(hass, hass_ws_client, setup_entry, account.id)

    assert not response["success"]
    assert dr.async_get(hass).async_get(account.id) is not None


async def test_removing_a_stale_baby_leaves_the_others_alone(
    hass: HomeAssistant, setup_entry: MockConfigEntry, hass_ws_client: WebSocketGenerator
) -> None:
    registry = dr.async_get(hass)
    stale = registry.async_get_or_create(
        config_entry_id=setup_entry.entry_id, identifiers={(DOMAIN, BABY_THREE_ID)}, name="Three"
    )
    before = {d.id for d in dr.async_entries_for_config_entry(registry, setup_entry.entry_id)}

    response = await _remove(hass, hass_ws_client, setup_entry, stale.id)

    assert response["success"], response
    after = {d.id for d in dr.async_entries_for_config_entry(registry, setup_entry.entry_id)}
    assert after == before - {stale.id}
