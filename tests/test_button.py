"""Core PR 14: the button platform snapshot and the buttons each baby has. Later PRs add their
buttons to the snapshot."""

from unittest.mock import patch

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry, snapshot_platform
from syrupy.assertion import SnapshotAssertion

from . import setup_integration
from .factories import (
    BABY_ONE_ID,
    BABY_TWO_ID,
    at,
)
from .fake_server import FakeServer
from .helpers import (
    NOW,
    button,
    get_baby_device,
)


@pytest.fixture(autouse=True)
def _start_at_now(freezer: FrozenDateTimeFactory) -> None:
    """Freeze the clock at NOW before anything is set up.

    Timers scheduled at setup then share the clock the tests move, so moving it forward and
    ticking reaches them the way it does in core's tests.
    """
    freezer.move_to(NOW)


async def test_buttons(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    snapshot: SnapshotAssertion,
    freezer: FrozenDateTimeFactory,
    mock_config_entry: MockConfigEntry,
    populated_server: FakeServer,
) -> None:
    freezer.move_to(at(180))
    with patch("custom_components.nighp_baby_tracker.PLATFORMS", [Platform.BUTTON]):
        await setup_integration(hass, mock_config_entry)

    await snapshot_platform(hass, entity_registry, snapshot, mock_config_entry.entry_id)


async def test_each_baby_gets_wet_dirty_mixed_bottle_and_discard_nursing_buttons(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    devices = dr.async_get(hass)
    entities = er.async_get(hass)
    for baby_id in (BABY_ONE_ID, BABY_TWO_ID):
        device = get_baby_device(devices, baby_id)
        assert device is not None
        buttons = [
            e for e in er.async_entries_for_device(entities, device.id) if e.domain == "button"
        ]
        assert {b.translation_key for b in buttons} == {
            "wet_diaper",
            "dirty_diaper",
            "mixed_diaper",
            "bottle",
            "discard_nursing",
        }
        assert len({b.unique_id for b in buttons}) == 5
    assert hass.states.get(button("baby_one", "wet")) is not None
    assert hass.states.get(button("baby_two", "mixed")) is not None
