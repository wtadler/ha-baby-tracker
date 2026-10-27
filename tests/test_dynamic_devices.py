"""Core PR 6: dynamic and stale devices. Babies that appear later get entities, and a baby that
is no longer in the account can be removed from the device registry."""

from typing import Any

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
)
from pytest_homeassistant_custom_component.typing import WebSocketGenerator

from custom_components.nighp_baby_tracker.const import (
    DOMAIN,
)

from .factories import (
    BABY_ONE_ID,
    BABY_THREE_ID,
    DEVICE_B,
    at,
    baby_payload,
)
from .fake_server import FakeServer
from .helpers import (
    NOW,
    get_baby_device,
    tick,
)

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


KEYS = ("last_diaper", "last_feed", "last_sleep")  # a pump belongs to the account, not a baby


# The counts and the asleep sensor are in test_sensor_today.py; each baby also has these four.
TODAY_KEYS = ("diapers_today", "feeds_today", "bottle_volume_today", "asleep")


TIMER_KEYS = ("sleep_timer", "nursing_timer", "nursing_left_time", "nursing_right_time")


PER_BABY = len(KEYS) + len(TODAY_KEYS) + len(TIMER_KEYS)


# The buttons share each baby's device, so entity counts here look at readings only.
READINGS = ("sensor", "binary_sensor")


BABY_THREE = baby_payload("Baby Three", BABY_THREE_ID)


def _entity_id(baby: str, key: str) -> str:
    return f"sensor.{baby}_{key}"


def _sensor_state(hass: HomeAssistant, baby: str, key: str):
    state = hass.states.get(_entity_id(baby, key))
    assert state is not None, f"{_entity_id(baby, key)} does not exist"
    return state


def _object_id(number: int) -> str:
    return f"A0000000-0000-4000-8000-{number:012d}"


@pytest.fixture(autouse=True)
def _start_at_now(freezer: FrozenDateTimeFactory) -> None:
    """Freeze the clock at NOW before anything is set up.

    Timers scheduled at setup then share the clock the tests move, so moving it forward and
    ticking reaches them the way it does in core's tests.
    """
    freezer.move_to(NOW)


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


async def test_a_baby_that_appears_later_is_added_without_a_reload(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    assert hass.states.get(_entity_id("baby_three", "last_diaper")) is None
    clients = len(server.clients)

    server.add_record(DEVICE_B, "Diaper", _object_id(70), when=at(300), baby=BABY_THREE, status=1)
    await tick(hass)

    assert len(server.clients) == clients
    assert _sensor_state(hass, "baby_three", "last_diaper").state == at(300).isoformat()
    assert _sensor_state(hass, "baby_three", "last_diaper").attributes["type"] == "dirty"
    device = get_baby_device(dr.async_get(hass), BABY_THREE_ID)
    assert device is not None
    entries = er.async_entries_for_device(er.async_get(hass), device.id)
    assert len([e for e in entries if e.domain in READINGS]) == PER_BABY


async def test_syncing_again_does_not_duplicate_a_baby(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    server.add_record(DEVICE_B, "Diaper", _object_id(71), when=at(300), baby=BABY_THREE)
    await tick(hass)
    await tick(hass)
    entries = er.async_entries_for_config_entry(er.async_get(hass), init_integration.entry_id)
    assert len([e for e in entries if e.domain in READINGS]) == 3 * PER_BABY + 2
