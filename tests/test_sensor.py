"""Core PR 1: the sensor platform snapshot, and the sensors and device each baby has."""

from unittest.mock import patch

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry, snapshot_platform
from syrupy.assertion import SnapshotAssertion

from . import NOW, get_baby_device, setup_integration
from .factories import BABY_ONE_ID, BABY_TWO_ID, DEVICE_A, at, baby_payload
from .fake_server import FakeServer

KEYS = ("last_diaper", "last_feed", "last_sleep")
BABIES = ((BABY_ONE_ID, "baby_one", "Baby One"), (BABY_TWO_ID, "baby_two", "Baby Two"))


@pytest.fixture(autouse=True)
def _start_at_now(freezer: FrozenDateTimeFactory) -> None:
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
    """Test the sensor entities and their states."""
    freezer.move_to(at(180))
    with patch("custom_components.nighp_baby_tracker.PLATFORMS", [Platform.SENSOR]):
        await setup_integration(hass, mock_config_entry)

    await snapshot_platform(hass, entity_registry, snapshot, mock_config_entry.entry_id)


@pytest.mark.usefixtures("init_integration")
async def test_each_baby_has_a_device_with_the_last_activity_sensors(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    entity_registry: er.EntityRegistry,
) -> None:
    """Test each baby gets a device named after them, holding the sensors."""
    for baby_id, _, name in BABIES:
        device = get_baby_device(device_registry, baby_id)
        assert device is not None
        assert device.name == name
        sensors = er.async_entries_for_device(entity_registry, device.id)
        assert set(KEYS) <= {sensor.translation_key for sensor in sensors}


async def test_a_baby_without_a_name_gets_a_translated_device_name(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    mock_config_entry: MockConfigEntry,
    server: FakeServer,
) -> None:
    """Test a baby with no name gets the device's translated name."""
    nameless_id = "44444444-DDDD-4DDD-8DDD-000000000004"
    server.add_payload(DEVICE_A, baby_payload("", nameless_id))
    await setup_integration(hass, mock_config_entry)

    device = get_baby_device(device_registry, nameless_id)
    assert device is not None
    assert device.name == "Baby"
    state = hass.states.get("sensor.baby_last_diaper")
    assert state is not None
    assert state.attributes["friendly_name"] == "Baby Last diaper"


@pytest.mark.usefixtures("init_integration")
async def test_the_sensors_are_named_after_the_baby_and_the_activity(hass: HomeAssistant) -> None:
    """Test the sensor names combine the baby's name and the activity."""
    for _, baby, name in BABIES:
        for key, label in zip(KEYS, ("Last diaper", "Last feed", "Last sleep"), strict=True):
            state = hass.states.get(f"sensor.{baby}_{key}")
            assert state is not None
            assert state.attributes["friendly_name"] == f"{name} {label}"


async def test_unique_ids_are_built_from_the_baby_id(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    init_integration: MockConfigEntry,
) -> None:
    """Test the unique IDs are the baby's ID and the sensor's key."""
    entries = er.async_entries_for_config_entry(entity_registry, init_integration.entry_id)
    unique_ids = [entry.unique_id for entry in entries]
    assert len(set(unique_ids)) == len(unique_ids)
    assert {f"{baby_id}_{key}" for baby_id, _, _ in BABIES for key in KEYS} <= set(unique_ids)


@pytest.mark.usefixtures("init_integration")
async def test_the_sensors_are_timestamps(hass: HomeAssistant) -> None:
    """Test the last-activity sensors are timestamps."""
    for key in KEYS:
        state = hass.states.get(f"sensor.baby_one_{key}")
        assert state is not None
        assert state.attributes["device_class"] == "timestamp"
