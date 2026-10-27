"""HACS only: entries and entities saved by earlier versions are migrated and cleaned up.
This file does not go to core."""

from datetime import timedelta
from typing import Any

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import SOURCE_USER, ConfigEntryState
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.nighp_baby_tracker.const import (
    DOMAIN,
)

from . import (
    EMAIL,
    EMAIL_TWO,
    NOW,
    PASSWORD,
    get_baby_device,
    sync_interval,
    tick,
)
from .factories import (
    BABY_ONE_ID,
    BABY_THREE_ID,
    BABY_TWO_ID,
)
from .fake_server import FakeServer
from .helpers import LEGACY_INTERVAL, account_device, entry_with_options, pump_entity, state_of


def add_old_pump_entity(
    hass: HomeAssistant, entry: MockConfigEntry, baby_id: str, name: str
) -> er.RegistryEntry:
    """Register a legacy per-baby ``<baby_id>_last_pump`` sensor on the baby's device."""
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, baby_id)}, name=name
    )
    return er.async_get(hass).async_get_or_create(
        "sensor",
        DOMAIN,
        f"{baby_id}_last_pump",
        config_entry=entry,
        device_id=device.id,
        translation_key="last_pump",
        has_entity_name=True,
    )


def add_old_timer_button(
    hass: HomeAssistant, entry: MockConfigEntry, baby_id: str, key: str, name: str = "Baby"
) -> er.RegistryEntry:
    """Register a legacy ``<baby_id>_<key>`` timer button on the baby's device."""
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, baby_id)}, name=name
    )
    return er.async_get(hass).async_get_or_create(
        "button",
        DOMAIN,
        f"{baby_id}_{key}",
        config_entry=entry,
        device_id=device.id,
        translation_key=key,
        has_entity_name=True,
    )


@pytest.fixture(autouse=True)
def _start_at_now(freezer: FrozenDateTimeFactory) -> None:
    """Freeze the clock at NOW before anything is set up.

    Timers scheduled at setup then share the clock the tests move, so moving it forward and
    ticking reaches them the way it does in core's tests.
    """
    freezer.move_to(NOW)


async def _setup_entry(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


OLD_TIMER_BUTTONS = ("sleep", "nurse_left", "nurse_right")


@pytest.mark.parametrize("key", ["sleep", "nurse_left", "nurse_right"])
async def test_the_old_timer_buttons_are_gone(
    hass: HomeAssistant, init_integration: MockConfigEntry, key: str
) -> None:
    registry = er.async_get(hass)
    for baby_id in (BABY_ONE_ID, BABY_TWO_ID):
        assert registry.async_get_entity_id("button", DOMAIN, f"{baby_id}_{key}") is None
    assert hass.states.get(f"button.baby_one_{key}") is None


async def test_the_one_shot_buttons_and_discard_are_the_only_buttons_per_baby(
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
            if e.domain == "button"
        }
        assert keys == {
            "wet_diaper",
            "dirty_diaper",
            "mixed_diaper",
            "bottle",
            "discard_nursing",
        }


async def test_setup_removes_the_old_per_baby_last_pump_entities(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    mock_config_entry.add_to_hass(hass)
    old_one = add_old_pump_entity(hass, mock_config_entry, BABY_ONE_ID, "Baby One")
    old_two = add_old_pump_entity(hass, mock_config_entry, BABY_TWO_ID, "Baby Two")
    registry = er.async_get(hass)
    assert registry.async_get(old_one.entity_id) is not None

    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert registry.async_get(old_one.entity_id) is None
    assert registry.async_get(old_two.entity_id) is None
    assert registry.async_get_entity_id("sensor", DOMAIN, f"{BABY_ONE_ID}_last_pump") is None
    assert registry.async_get_entity_id("sensor", DOMAIN, f"{BABY_TWO_ID}_last_pump") is None
    assert hass.states.get(old_one.entity_id) is None
    # ... and the account's replaces them.
    assert state_of(hass, mock_config_entry).state == "unknown"


async def test_the_cleanup_leaves_every_other_per_baby_entity_alone(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    mock_config_entry.add_to_hass(hass)
    add_old_pump_entity(hass, mock_config_entry, BABY_ONE_ID, "Baby One")

    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    registry = er.async_get(hass)
    assert registry.async_get_entity_id("sensor", DOMAIN, f"{BABY_ONE_ID}_last_pump") is None
    for key in ("last_diaper", "last_feed", "last_sleep", "diapers_today", "feeds_today"):
        assert registry.async_get_entity_id("sensor", DOMAIN, f"{BABY_ONE_ID}_{key}") is not None, (
            key
        )
    assert hass.states.get("sensor.baby_one_last_diaper") is not None


async def test_the_cleanup_runs_again_harmlessly_on_a_reload(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    mock_config_entry.add_to_hass(hass)
    add_old_pump_entity(hass, mock_config_entry, BABY_ONE_ID, "Baby One")
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    first = pump_entity(hass, mock_config_entry)

    assert await hass.config_entries.async_reload(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert pump_entity(hass, mock_config_entry) == first
    assert (
        er.async_get(hass).async_get_entity_id("sensor", DOMAIN, f"{BABY_ONE_ID}_last_pump") is None
    )


async def test_the_cleanup_keeps_the_new_account_sensors_registry_entry(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    registry = er.async_get(hass)
    before = registry.async_get(pump_entity(hass, init_integration))

    assert await hass.config_entries.async_reload(init_integration.entry_id)
    await hass.async_block_till_done()

    after = registry.async_get(pump_entity(hass, init_integration))
    assert before is not None and after is not None
    assert after.id == before.id  # kept, not removed and recreated


async def test_the_cleanup_runs_for_every_entry_and_each_keeps_its_own_account_sensor(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    mock_config_entry.add_to_hass(hass)
    other = MockConfigEntry(
        domain=DOMAIN, title=EMAIL_TWO, unique_id=EMAIL_TWO, data=dict(mock_config_entry.data)
    )
    other.add_to_hass(hass)
    old_first = add_old_pump_entity(hass, mock_config_entry, BABY_ONE_ID, "Baby One")
    old_second = add_old_pump_entity(hass, other, BABY_THREE_ID, "Baby Three")

    await hass.config_entries.async_setup(mock_config_entry.entry_id)  # sets up the whole domain
    await hass.async_block_till_done()

    registry = er.async_get(hass)
    assert registry.async_get(old_first.entity_id) is None
    assert registry.async_get(old_second.entity_id) is None
    assert pump_entity(hass, mock_config_entry) != pump_entity(hass, other)
    assert account_device(hass, mock_config_entry).id != account_device(hass, other).id


async def test_the_cleanup_removes_an_old_entity_for_a_baby_no_longer_in_the_account(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    mock_config_entry.add_to_hass(hass)
    gone = add_old_pump_entity(hass, mock_config_entry, BABY_THREE_ID, "Baby Three")

    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert er.async_get(hass).async_get(gone.entity_id) is None


@pytest.mark.parametrize(
    "options",
    [{}, {LEGACY_INTERVAL: 1}, {LEGACY_INTERVAL: 15}, {LEGACY_INTERVAL: 1440}],
    ids=["no-options", "legacy-1-minute", "legacy-15-minutes", "legacy-a-day"],
)
async def test_polling_is_five_minutes_whatever_an_old_install_saved(
    hass: HomeAssistant, server: FakeServer, options: dict[str, Any]
) -> None:
    await entry_with_options(hass, options)

    assert await sync_interval(hass, server) == timedelta(minutes=5)


async def test_a_legacy_interval_does_not_make_syncs_run_early(
    hass: HomeAssistant, server: FakeServer
) -> None:
    await entry_with_options(hass, {LEGACY_INTERVAL: 1})
    calls = server.device_list_calls

    await tick(hass, minutes=1)
    await tick(hass, minutes=2)
    assert server.device_list_calls == calls  # nothing synced before five minutes

    await tick(hass, minutes=5)
    assert server.device_list_calls == calls + 1


async def test_a_legacy_interval_does_not_make_syncs_slower(
    hass: HomeAssistant, server: FakeServer
) -> None:
    await entry_with_options(hass, {LEGACY_INTERVAL: 1440})
    calls = server.device_list_calls

    await tick(hass, minutes=5)

    assert server.device_list_calls == calls + 1


async def test_no_update_listener_is_left_to_apply_an_interval(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    assert not init_integration.update_listeners


async def test_an_entry_from_before_the_migration_is_bumped_and_cleaned(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    mock_config_entry.add_to_hass(hass)
    assert (mock_config_entry.version, mock_config_entry.minor_version) == (1, 1)
    old = add_old_pump_entity(hass, mock_config_entry, BABY_ONE_ID, "Baby One")

    await _setup_entry(hass, mock_config_entry)

    assert mock_config_entry.state is ConfigEntryState.LOADED
    assert (mock_config_entry.version, mock_config_entry.minor_version) == (1, 2)
    assert er.async_get(hass).async_get(old.entity_id) is None


async def test_the_cleanup_runs_only_once(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    mock_config_entry.add_to_hass(hass)
    await _setup_entry(hass, mock_config_entry)
    assert mock_config_entry.minor_version == 2

    # Something that looks like the old entity turns up later: setup must leave it alone.
    later = add_old_pump_entity(hass, mock_config_entry, BABY_TWO_ID, "Baby Two")
    assert await hass.config_entries.async_reload(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert er.async_get(hass).async_get(later.entity_id) is not None


async def test_the_migration_is_idempotent_when_the_cleanup_already_ran(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """The live install already ran the cleanup on setup, so nothing is left to remove."""
    mock_config_entry.add_to_hass(hass)
    await _setup_entry(hass, mock_config_entry)
    registry = er.async_get(hass)
    before = {
        e.entity_id for e in er.async_entries_for_config_entry(registry, mock_config_entry.entry_id)
    }

    # Put the entry back at the old minor version, as a live install that has run the cleanup is.
    await hass.config_entries.async_unload(mock_config_entry.entry_id)
    hass.config_entries.async_update_entry(mock_config_entry, minor_version=1)
    await _setup_entry(hass, mock_config_entry)

    assert mock_config_entry.state is ConfigEntryState.LOADED
    assert mock_config_entry.minor_version == 2
    after = {
        e.entity_id for e in er.async_entries_for_config_entry(registry, mock_config_entry.entry_id)
    }
    assert after == before


async def test_the_migration_keeps_the_accounts_own_last_pump_entity(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    mock_config_entry.add_to_hass(hass)
    await _setup_entry(hass, mock_config_entry)
    registry = er.async_get(hass)
    account = registry.async_get_entity_id(
        "sensor", DOMAIN, f"{mock_config_entry.entry_id}_last_pump"
    )
    assert account is not None

    await hass.config_entries.async_unload(mock_config_entry.entry_id)
    hass.config_entries.async_update_entry(mock_config_entry, minor_version=1)
    await _setup_entry(hass, mock_config_entry)

    assert registry.async_get(account) is not None


async def test_the_migration_removes_every_old_entity_including_one_for_an_absent_baby(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    mock_config_entry.add_to_hass(hass)
    old = [
        add_old_pump_entity(hass, mock_config_entry, baby_id, name)
        for baby_id, name in (
            (BABY_ONE_ID, "Baby One"),
            (BABY_TWO_ID, "Baby Two"),
            (BABY_THREE_ID, "Baby Three"),
        )
    ]
    await _setup_entry(hass, mock_config_entry)
    registry = er.async_get(hass)
    assert [registry.async_get(entity.entity_id) for entity in old] == [None, None, None]


async def test_the_migration_leaves_other_entries_entities_alone(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    other = MockConfigEntry(
        domain=DOMAIN,
        title="other",
        unique_id="other@example.com",
        minor_version=2,
        data=dict(mock_config_entry.data),
    )
    other.add_to_hass(hass)
    mock_config_entry.add_to_hass(hass)
    theirs = add_old_pump_entity(hass, other, BABY_THREE_ID, "Baby Three")

    await _setup_entry(hass, mock_config_entry)

    assert er.async_get(hass).async_get(theirs.entity_id) is not None
    assert dr.async_get(hass).async_get(theirs.device_id) is not None


async def test_an_entry_from_a_newer_version_fails_to_migrate(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    newer = MockConfigEntry(
        domain=DOMAIN, title=EMAIL, unique_id=EMAIL, version=2, data=dict(mock_config_entry.data)
    )
    newer.add_to_hass(hass)
    await _setup_entry(hass, newer)
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


async def test_setup_removes_the_old_sleep_and_nurse_buttons_from_the_registry(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    mock_config_entry.add_to_hass(hass)
    old = [
        add_old_timer_button(hass, mock_config_entry, baby_id, key)
        for baby_id in (BABY_ONE_ID, BABY_TWO_ID)
        for key in OLD_TIMER_BUTTONS
    ]
    registry = er.async_get(hass)
    assert all(registry.async_get(entry.entity_id) is not None for entry in old)

    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    for entry in old:
        assert registry.async_get(entry.entity_id) is None
        assert hass.states.get(entry.entity_id) is None  # no orphaned "unavailable" button


async def test_the_button_cleanup_leaves_the_other_buttons_and_sleep_named_sensors_alone(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    mock_config_entry.add_to_hass(hass)
    for key in OLD_TIMER_BUTTONS:
        add_old_timer_button(hass, mock_config_entry, BABY_ONE_ID, key)
    kept = [
        add_old_timer_button(hass, mock_config_entry, BABY_ONE_ID, key)
        for key in ("wet_diaper", "dirty_diaper", "mixed_diaper", "bottle")
    ]
    registry = er.async_get(hass)
    before = {entry.unique_id: entry.id for entry in kept}

    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    for unique_id, registry_id in before.items():
        entity_id = registry.async_get_entity_id("button", DOMAIN, unique_id)
        assert entity_id is not None, unique_id
        entity = registry.async_get(entity_id)
        assert entity is not None and entity.id == registry_id  # kept, not recreated
    for key in ("last_sleep", "sleep_timer", "last_feed"):
        assert registry.async_get_entity_id("sensor", DOMAIN, f"{BABY_ONE_ID}_{key}") is not None
    assert registry.async_get_entity_id("switch", DOMAIN, f"{BABY_ONE_ID}_sleep") is not None


async def test_the_button_cleanup_does_not_touch_another_integrations_buttons(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    mock_config_entry.add_to_hass(hass)
    registry = er.async_get(hass)
    other = registry.async_get_or_create("button", "other_integration", f"{BABY_ONE_ID}_sleep")

    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert registry.async_get(other.entity_id) is not None


async def test_the_button_cleanup_runs_again_harmlessly_on_a_reload(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    mock_config_entry.add_to_hass(hass)
    add_old_timer_button(hass, mock_config_entry, BABY_ONE_ID, "nurse_left")
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    switches = {
        e.entity_id
        for e in er.async_entries_for_config_entry(er.async_get(hass), mock_config_entry.entry_id)
        if e.domain == "switch"
    }

    assert await hass.config_entries.async_reload(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert (
        er.async_get(hass).async_get_entity_id("button", DOMAIN, f"{BABY_ONE_ID}_nurse_left")
        is None
    )
    assert switches == {
        e.entity_id
        for e in er.async_entries_for_config_entry(er.async_get(hass), mock_config_entry.entry_id)
        if e.domain == "switch"
    }
