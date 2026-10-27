"""The one-time registry cleanup runs as a config entry migration, exactly once."""

from homeassistant.config_entries import SOURCE_USER, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.nighp_baby_tracker.const import CONF_EMAIL, CONF_PASSWORD, DOMAIN

from .conftest import EMAIL, PASSWORD, FakeServer
from .factories import BABY_ONE_ID, BABY_THREE_ID, BABY_TWO_ID
from .test_pump_account import add_old_pump_entity


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def test_an_entry_from_before_the_migration_is_bumped_and_cleaned(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    config_entry.add_to_hass(hass)
    assert (config_entry.version, config_entry.minor_version) == (1, 1)
    old = add_old_pump_entity(hass, config_entry, BABY_ONE_ID, "Baby One")

    await _setup(hass, config_entry)

    assert config_entry.state is ConfigEntryState.LOADED
    assert (config_entry.version, config_entry.minor_version) == (1, 2)
    assert er.async_get(hass).async_get(old.entity_id) is None


async def test_the_cleanup_runs_only_once(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    config_entry.add_to_hass(hass)
    await _setup(hass, config_entry)
    assert config_entry.minor_version == 2

    # Something that looks like the old entity turns up later: setup must leave it alone.
    later = add_old_pump_entity(hass, config_entry, BABY_TWO_ID, "Baby Two")
    assert await hass.config_entries.async_reload(config_entry.entry_id)
    await hass.async_block_till_done()

    assert er.async_get(hass).async_get(later.entity_id) is not None


async def test_the_migration_is_idempotent_when_the_cleanup_already_ran(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    """The live install already ran the cleanup on setup, so nothing is left to remove."""
    config_entry.add_to_hass(hass)
    await _setup(hass, config_entry)
    registry = er.async_get(hass)
    before = {e.entity_id for e in er.async_entries_for_config_entry(registry, config_entry.entry_id)}

    # Put the entry back at the old minor version, as a live install that has run the cleanup is.
    await hass.config_entries.async_unload(config_entry.entry_id)
    hass.config_entries.async_update_entry(config_entry, minor_version=1)
    await _setup(hass, config_entry)

    assert config_entry.state is ConfigEntryState.LOADED
    assert config_entry.minor_version == 2
    after = {e.entity_id for e in er.async_entries_for_config_entry(registry, config_entry.entry_id)}
    assert after == before


async def test_the_migration_keeps_the_accounts_own_last_pump_entity(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    config_entry.add_to_hass(hass)
    await _setup(hass, config_entry)
    registry = er.async_get(hass)
    account = registry.async_get_entity_id("sensor", DOMAIN, f"{config_entry.entry_id}_last_pump")
    assert account is not None

    await hass.config_entries.async_unload(config_entry.entry_id)
    hass.config_entries.async_update_entry(config_entry, minor_version=1)
    await _setup(hass, config_entry)

    assert registry.async_get(account) is not None


async def test_the_migration_removes_every_old_entity_including_one_for_an_absent_baby(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    config_entry.add_to_hass(hass)
    old = [
        add_old_pump_entity(hass, config_entry, baby_id, name)
        for baby_id, name in (
            (BABY_ONE_ID, "Baby One"),
            (BABY_TWO_ID, "Baby Two"),
            (BABY_THREE_ID, "Baby Three"),
        )
    ]
    await _setup(hass, config_entry)
    registry = er.async_get(hass)
    assert [registry.async_get(entity.entity_id) for entity in old] == [None, None, None]


async def test_the_migration_leaves_other_entries_entities_alone(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    other = MockConfigEntry(domain=DOMAIN, title="other", unique_id="other@example.com", data={})
    other.add_to_hass(hass)
    config_entry.add_to_hass(hass)
    theirs = add_old_pump_entity(hass, other, BABY_THREE_ID, "Baby Three")

    await _setup(hass, config_entry)

    assert er.async_get(hass).async_get(theirs.entity_id) is not None
    assert dr.async_get(hass).async_get(theirs.device_id) is not None


async def test_an_entry_from_a_newer_version_fails_to_migrate(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    newer = MockConfigEntry(
        domain=DOMAIN, title=EMAIL, unique_id=EMAIL, version=2, data=dict(config_entry.data)
    )
    newer.add_to_hass(hass)
    await _setup(hass, newer)
    assert newer.state is ConfigEntryState.MIGRATION_ERROR


async def test_a_new_entry_is_created_at_the_current_minor_version(
    hass: HomeAssistant, server: FakeServer
) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_EMAIL: EMAIL, CONF_PASSWORD: PASSWORD}
    )
    await hass.async_block_till_done()
    assert (result["result"].version, result["result"].minor_version) == (1, 2)
