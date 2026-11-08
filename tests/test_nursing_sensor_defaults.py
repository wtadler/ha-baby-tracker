"""Core PR 4: which of the last nursing and sleep sensors start enabled."""

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.baby_tracker.const import DOMAIN

from .factories import BABY_ONE_ID, BABY_TWO_ID

ENABLED = ("last_nursing", "last_sleep_duration")
DISABLED = ("last_nursing_duration", "last_nursing_left_time", "last_nursing_right_time")


@pytest.mark.parametrize("baby_id", [BABY_ONE_ID, BABY_TWO_ID])
@pytest.mark.parametrize("key", ENABLED)
async def test_the_main_readings_start_enabled(
    hass: HomeAssistant, init_integration: MockConfigEntry, baby_id: str, key: str
) -> None:
    """Test the last nursing time and the last sleep duration are on from the start."""
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id("sensor", DOMAIN, f"{baby_id}_{key}")
    assert entity_id is not None
    entry = registry.async_get(entity_id)
    assert entry is not None
    assert entry.disabled_by is None
    assert hass.states.get(entity_id) is not None


@pytest.mark.parametrize("baby_id", [BABY_ONE_ID, BABY_TWO_ID])
@pytest.mark.parametrize("key", DISABLED)
async def test_the_nursing_minutes_start_disabled(
    hass: HomeAssistant, init_integration: MockConfigEntry, baby_id: str, key: str
) -> None:
    """Test the three nursing minutes are registered, off, and have no state until enabled."""
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id("sensor", DOMAIN, f"{baby_id}_{key}")
    assert entity_id is not None
    entry = registry.async_get(entity_id)
    assert entry is not None
    assert entry.disabled_by is er.RegistryEntryDisabler.INTEGRATION
    assert hass.states.get(entity_id) is None
