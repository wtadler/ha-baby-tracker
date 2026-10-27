"""Core PRs 17 and 20: the switch platform snapshot and the switches each baby has."""

from unittest.mock import patch

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    snapshot_platform,
)
from syrupy.assertion import SnapshotAssertion

from . import NOW, get_baby_device, setup_integration
from .factories import BABY_ONE_ID, BABY_TWO_ID
from .helpers import NURSE_LEFT, NURSE_RIGHT, SLEEP_SWITCH, TWO_NURSE_LEFT, TWO_SLEEP_SWITCH, state


@pytest.fixture(autouse=True)
def _start_at_now(freezer: FrozenDateTimeFactory) -> None:
    """Freeze the clock at NOW before anything is set up."""
    freezer.move_to(NOW)


async def test_switches(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    snapshot: SnapshotAssertion,
    mock_config_entry: MockConfigEntry,
) -> None:
    with patch("custom_components.nighp_baby_tracker.PLATFORMS", [Platform.SWITCH]):
        await setup_integration(hass, mock_config_entry)

    await snapshot_platform(hass, entity_registry, snapshot, mock_config_entry.entry_id)


async def test_each_baby_gets_a_sleep_switch_and_two_nursing_switches(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    devices = dr.async_get(hass)
    entities = er.async_get(hass)
    for baby_id in (BABY_ONE_ID, BABY_TWO_ID):
        device = get_baby_device(devices, baby_id)
        assert device is not None
        keys = {
            e.translation_key
            for e in er.async_entries_for_device(entities, device.id)
            if e.domain == "switch"
        }
        assert keys == {"sleep", "nursing_left", "nursing_right"}
    for entity_id in (SLEEP_SWITCH, NURSE_LEFT, NURSE_RIGHT, TWO_SLEEP_SWITCH, TWO_NURSE_LEFT):
        assert state(hass, entity_id).state == "off"
    assert state(hass, SLEEP_SWITCH).name == "Baby One Sleep"
    assert state(hass, NURSE_LEFT).name == "Baby One Nursing left"
