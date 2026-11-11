"""HACS only: six entities that were dropped, and the rule that no entity starts disabled.

The integration keeps what Home Assistant can act on (see CONTRIBUTING.md). Last milestone, Last
vaccine, the three Last nursing minutes sensors, and Latest picture only repeated the app, so they
are gone, and an entity that is only worth adding switched off isn't added. This file does not go
to core.
"""

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.baby_tracker import image, sensor
from custom_components.baby_tracker.const import DOMAIN

from .factories import BABY_ONE_ID, BABY_TWO_ID

REMOVED = (
    ("sensor", "last_milestone"),
    ("sensor", "last_vaccine"),
    ("sensor", "last_nursing_duration"),
    ("sensor", "last_nursing_left_time"),
    ("sensor", "last_nursing_right_time"),
    ("image", "latest_picture"),
)


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
@pytest.mark.parametrize("baby_id", [BABY_ONE_ID, BABY_TWO_ID])
@pytest.mark.parametrize(("domain", "key"), REMOVED)
async def test_a_removed_entity_is_not_created(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    baby_id: str,
    domain: str,
    key: str,
) -> None:
    """Test the entity has no registry entry and no state, even with every entity enabled."""
    registry = er.async_get(hass)
    assert registry.async_get_entity_id(domain, DOMAIN, f"{baby_id}_{key}") is None
    assert not [
        entity
        for entity in er.async_entries_for_config_entry(registry, init_integration.entry_id)
        if entity.translation_key == key
    ]


@pytest.mark.parametrize(("domain", "key"), REMOVED)
def test_a_removed_entity_has_no_description(domain: str, key: str) -> None:
    """Test the sensor and image descriptions no longer list it."""
    descriptions = {
        "sensor": [
            description
            for rows, _ in (*sensor.BABY_SENSORS, *sensor.ACCOUNT_SENSORS)
            for description in rows
        ],
        "image": list(image.IMAGES),
    }[domain]
    assert key not in {description.key for description in descriptions}


async def test_the_babys_photo_is_kept(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """Test the per-baby Photo image, the only image, is still there and enabled."""
    registry = er.async_get(hass)
    for baby_id in (BABY_ONE_ID, BABY_TWO_ID):
        entity_id = registry.async_get_entity_id("image", DOMAIN, f"{baby_id}_photo")
        assert entity_id is not None
        entry = registry.async_get(entity_id)
        assert entry is not None
        assert entry.disabled_by is None
    assert [entity.key for entity in image.IMAGES] == ["photo"]


@pytest.mark.parametrize("baby_id", [BABY_ONE_ID, BABY_TWO_ID])
async def test_last_nursing_is_enabled_by_default(
    hass: HomeAssistant, init_integration: MockConfigEntry, baby_id: str
) -> None:
    """Test Last nursing is registered enabled and has a state, without any help."""
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id("sensor", DOMAIN, f"{baby_id}_last_nursing")
    assert entity_id is not None
    entry = registry.async_get(entity_id)
    assert entry is not None
    assert entry.disabled_by is None
    assert hass.states.get(entity_id) is not None


async def test_no_entity_starts_disabled(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """Test every entity the integration creates is enabled: one worth adding is worth showing."""
    registry = er.async_get(hass)
    entries = er.async_entries_for_config_entry(registry, init_integration.entry_id)
    assert entries
    assert [entry.entity_id for entry in entries if entry.disabled_by is not None] == []
