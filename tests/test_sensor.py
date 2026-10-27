"""The sensors: last activity, today's counts, timers, and the account's last pump."""

from typing import Any
from unittest.mock import patch

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.const import (
    Platform,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    snapshot_platform,
)
from syrupy.assertion import SnapshotAssertion

from . import setup_integration
from .factories import (
    BABY_ONE_ID,
    BABY_THREE_ID,
    BABY_TWO_ID,
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

KEYS = ("last_diaper", "last_feed", "last_sleep")  # a pump belongs to the account, not a baby


# The counts and the asleep sensor are covered in test_today.py; each baby also has these four.
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
def _start_at_now(freezer: Any) -> None:
    """Freeze the clock at NOW before anything is set up.

    Timers scheduled at setup then share the clock the tests move, so moving it forward and
    ticking reaches them the way it does in core's tests.
    """
    freezer.move_to(NOW)


async def test_sensors(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    snapshot: SnapshotAssertion,
    freezer: FrozenDateTimeFactory,
    mock_config_entry: MockConfigEntry,
    populated_server: FakeServer,
) -> None:
    freezer.move_to(at(180))
    with patch("custom_components.nighp_baby_tracker.PLATFORMS", [Platform.SENSOR]):
        await setup_integration(hass, mock_config_entry)

    await snapshot_platform(hass, entity_registry, snapshot, mock_config_entry.entry_id)


async def test_two_babies_make_two_devices_each_with_their_own_sensors(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    devices = dr.async_get(hass)
    entities = er.async_get(hass)
    for baby_id, name in ((BABY_ONE_ID, "Baby One"), (BABY_TWO_ID, "Baby Two")):
        device = get_baby_device(devices, baby_id)
        assert device is not None
        assert device.name == name
        sensors = [
            e for e in er.async_entries_for_device(entities, device.id) if e.domain in READINGS
        ]
        assert len(sensors) == PER_BABY
        assert {sensor.translation_key for sensor in sensors} == set(KEYS + TODAY_KEYS + TIMER_KEYS)
    # The two babies' devices and the account's (which holds the last pump).
    assert len(dr.async_entries_for_config_entry(devices, init_integration.entry_id)) == 3


async def test_entities_are_named_after_the_baby_and_the_activity(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    for baby, name in (("baby_one", "Baby One"), ("baby_two", "Baby Two")):
        for key, label in zip(KEYS, ("Last diaper", "Last feed", "Last sleep"), strict=True):
            assert _sensor_state(hass, baby, key).attributes["friendly_name"] == f"{name} {label}"


async def test_entities_have_unique_ids(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    entries = er.async_entries_for_config_entry(er.async_get(hass), init_integration.entry_id)
    unique_ids = [entry.unique_id for entry in entries if entry.domain in READINGS]
    assert len(unique_ids) == 2 * PER_BABY + 2  # and the account's last pump and pump timer
    assert len(set(unique_ids)) == 2 * PER_BABY + 2
    assert all(unique_id for unique_id in unique_ids)


async def test_unique_ids_are_built_from_the_baby_id(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    entries = er.async_entries_for_config_entry(er.async_get(hass), init_integration.entry_id)
    entries = [e for e in entries if e.domain in READINGS]
    assert len(entries) == 2 * PER_BABY + 2
    assert {e.unique_id for e in entries} == {
        f"{baby}_{key}"
        for baby in (BABY_ONE_ID, BABY_TWO_ID)
        for key in KEYS + TODAY_KEYS + TIMER_KEYS
    } | {
        f"{init_integration.entry_id}_last_pump",
        f"{init_integration.entry_id}_pump_timer",
    }


async def test_the_sensors_are_timestamps(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    for key in KEYS:
        assert _sensor_state(hass, "baby_one", key).attributes["device_class"] == "timestamp"


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
