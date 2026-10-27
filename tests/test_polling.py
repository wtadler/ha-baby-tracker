# ruff: noqa: F811
"""Polling is fixed: 5 minutes, or 1 minute while a timer runs. No option changes it."""

from datetime import timedelta
from typing import Any

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType, InvalidData
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.nighp_baby_tracker.const import (
    CONF_BOTTLE_SIZE,
    CONF_BOTTLE_TYPE,
    CONF_BOTTLE_UNIT,
    CONF_DEVICE_UUID,
    CONF_EMAIL,
    CONF_PASSWORD,
    DOMAIN,
)

from .conftest import EMAIL, PASSWORD, FakeServer
from .helpers import tick
from .test_writes import (  # noqa: F401  (fixtures: a fake that can post)
    fake_client,
    press,
    second_server,
    server,
)

# The option an earlier version saved. It is no longer a setting, so these tests spell it out.
LEGACY_INTERVAL = "scan_interval"
BOTTLE_OPTIONS = {CONF_BOTTLE_SIZE, CONF_BOTTLE_UNIT, CONF_BOTTLE_TYPE}

SLEEP_BUTTON = "button.baby_one_sleep"
NURSE_LEFT = "button.baby_one_nurse_left"


async def _entry_with_options(hass: HomeAssistant, options: dict[str, Any]) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=EMAIL,
        unique_id=EMAIL,
        data={
            CONF_EMAIL: EMAIL,
            CONF_PASSWORD: PASSWORD,
            CONF_DEVICE_UUID: "EEEEEEEE-0000-4000-8000-0000000000EE",
        },
        options=options,
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    return entry


@pytest.mark.parametrize(
    "options",
    [{}, {LEGACY_INTERVAL: 1}, {LEGACY_INTERVAL: 15}, {LEGACY_INTERVAL: 1440}],
    ids=["no-options", "legacy-1-minute", "legacy-15-minutes", "legacy-a-day"],
)
async def test_polling_is_five_minutes_whatever_an_old_install_saved(
    hass: HomeAssistant, options: dict[str, Any]
) -> None:
    entry = await _entry_with_options(hass, options)

    assert entry.runtime_data.update_interval == timedelta(minutes=5)


async def test_a_legacy_interval_does_not_make_syncs_run_early(
    hass: HomeAssistant, server: FakeServer
) -> None:
    await _entry_with_options(hass, {LEGACY_INTERVAL: 1})
    calls = server.device_list_calls

    await tick(hass, minutes=1)
    await tick(hass, minutes=2)
    assert server.device_list_calls == calls  # nothing synced before five minutes

    await tick(hass, minutes=5)
    assert server.device_list_calls == calls + 1


async def test_a_legacy_interval_does_not_make_syncs_slower(
    hass: HomeAssistant, server: FakeServer
) -> None:
    await _entry_with_options(hass, {LEGACY_INTERVAL: 1440})
    calls = server.device_list_calls

    await tick(hass, minutes=5)

    assert server.device_list_calls == calls + 1


@pytest.mark.parametrize("options", [{}, {LEGACY_INTERVAL: 15}], ids=["new", "legacy"])
async def test_a_running_sleep_polls_every_minute_and_five_when_it_stops(
    hass: HomeAssistant, options: dict[str, Any], freezer: Any
) -> None:
    entry = await _entry_with_options(hass, options)

    await press(hass, SLEEP_BUTTON)
    assert entry.runtime_data.update_interval == timedelta(minutes=1)

    freezer.tick(timedelta(minutes=3))
    await press(hass, SLEEP_BUTTON)
    assert entry.runtime_data.update_interval == timedelta(minutes=5)


@pytest.mark.parametrize("options", [{}, {LEGACY_INTERVAL: 15}], ids=["new", "legacy"])
async def test_a_running_nursing_timer_polls_every_minute_and_five_when_it_stops(
    hass: HomeAssistant, options: dict[str, Any], freezer: Any
) -> None:
    entry = await _entry_with_options(hass, options)

    await press(hass, NURSE_LEFT)
    assert entry.runtime_data.update_interval == timedelta(minutes=1)

    freezer.tick(timedelta(minutes=3))
    await press(hass, NURSE_LEFT)
    assert entry.runtime_data.update_interval == timedelta(minutes=5)


async def test_a_running_timer_syncs_every_minute(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer
) -> None:
    await press(hass, SLEEP_BUTTON)
    calls = server.device_list_calls

    await tick(hass, minutes=1)

    assert server.device_list_calls == calls + 1


# The options flow


async def test_the_options_flow_has_the_bottle_fields_and_no_interval(
    hass: HomeAssistant, setup_entry: MockConfigEntry
) -> None:
    result = await hass.config_entries.options.async_init(setup_entry.entry_id)

    assert result["type"] is FlowResultType.FORM
    assert {str(key) for key in result["data_schema"].schema} == BOTTLE_OPTIONS


async def test_the_options_flow_refuses_an_interval(
    hass: HomeAssistant, setup_entry: MockConfigEntry
) -> None:
    result = await hass.config_entries.options.async_init(setup_entry.entry_id)

    with pytest.raises(InvalidData):
        await hass.config_entries.options.async_configure(
            result["flow_id"],
            {
                LEGACY_INTERVAL: 10,
                CONF_BOTTLE_SIZE: 4,
                CONF_BOTTLE_UNIT: "fl. oz.",
                CONF_BOTTLE_TYPE: "formula",
            },
        )


async def test_saving_the_options_saves_only_the_bottle(
    hass: HomeAssistant, setup_entry: MockConfigEntry
) -> None:
    result = await hass.config_entries.options.async_init(setup_entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_BOTTLE_SIZE: 5.5, CONF_BOTTLE_UNIT: "fl. oz.", CONF_BOTTLE_TYPE: "pumped"},
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert setup_entry.options == {
        CONF_BOTTLE_SIZE: 5.5,
        CONF_BOTTLE_UNIT: "fl. oz.",
        CONF_BOTTLE_TYPE: "pumped",
    }


async def test_saving_the_options_drops_a_legacy_interval(hass: HomeAssistant) -> None:
    entry = await _entry_with_options(hass, {LEGACY_INTERVAL: 15, CONF_BOTTLE_SIZE: 6})

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert {str(key) for key in result["data_schema"].schema} == BOTTLE_OPTIONS
    await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_BOTTLE_SIZE: 6, CONF_BOTTLE_UNIT: "fl. oz.", CONF_BOTTLE_TYPE: "formula"},
    )
    await hass.async_block_till_done()

    assert LEGACY_INTERVAL not in entry.options
    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data.update_interval == timedelta(minutes=5)


async def test_an_options_change_leaves_a_running_timer_at_one_minute(
    hass: HomeAssistant, setup_entry: MockConfigEntry
) -> None:
    await press(hass, SLEEP_BUTTON)

    result = await hass.config_entries.options.async_init(setup_entry.entry_id)
    await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_BOTTLE_SIZE: 5, CONF_BOTTLE_UNIT: "fl. oz.", CONF_BOTTLE_TYPE: "formula"},
    )
    await hass.async_block_till_done()

    assert setup_entry.runtime_data.update_interval == timedelta(minutes=1)


async def test_no_update_listener_is_left_to_apply_an_interval(
    hass: HomeAssistant, setup_entry: MockConfigEntry
) -> None:
    assert not setup_entry.update_listeners


async def test_saving_the_options_neither_reloads_nor_syncs(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer
) -> None:
    coordinator = setup_entry.runtime_data
    calls = server.device_list_calls

    result = await hass.config_entries.options.async_init(setup_entry.entry_id)
    await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_BOTTLE_SIZE: 5, CONF_BOTTLE_UNIT: "fl. oz.", CONF_BOTTLE_TYPE: "formula"},
    )
    await hass.async_block_till_done()

    assert setup_entry.runtime_data is coordinator
    assert server.device_list_calls == calls
