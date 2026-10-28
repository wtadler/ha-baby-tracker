"""Core PR 15: the options flow and the bottle defaults it saves."""

import ast
import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import ConfigEntryState, ConfigFlowResult
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType, InvalidData
from homeassistant.util.unit_system import METRIC_SYSTEM, US_CUSTOMARY_SYSTEM, UnitSystem
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.baby_tracker import config_flow
from custom_components.baby_tracker.const import (
    CONF_BOTTLE_SIZE,
    CONF_BOTTLE_TYPE,
    CONF_BOTTLE_UNIT,
)

from . import NOW, sync_interval
from .fake_server import FakeServer
from .helpers import (
    INTEGRATION,
    LEGACY_INTERVAL,
    SLEEP_SWITCH,
    entry_with_options,
    set_options,
    toggle,
)


def schema_defaults(result: ConfigFlowResult) -> dict[str, Any]:
    return {str(key): key.default() for key in result["data_schema"].schema}


BOTTLE_OPTIONS = {CONF_BOTTLE_SIZE, CONF_BOTTLE_UNIT, CONF_BOTTLE_TYPE}


@pytest.fixture(autouse=True)
def _start_at_now(freezer: FrozenDateTimeFactory) -> None:
    """Freeze the clock at NOW before anything is set up.

    Timers scheduled at setup then share the clock the tests move, so moving it forward and
    ticking reaches them the way it does in core's tests.
    """
    freezer.move_to(NOW)


@pytest.mark.parametrize(
    ("size", "unit"),
    [(32, "fl. oz."), (1000, "mL")],
    ids=["32-oz", "1000-ml"],
)
async def test_the_largest_bottle_in_each_unit_is_accepted(
    hass: HomeAssistant, init_integration: MockConfigEntry, size: int, unit: str
) -> None:
    await set_options(
        hass,
        init_integration,
        **{CONF_BOTTLE_SIZE: size, CONF_BOTTLE_UNIT: unit, CONF_BOTTLE_TYPE: "formula"},
    )

    assert init_integration.options[CONF_BOTTLE_SIZE] == size
    assert init_integration.options[CONF_BOTTLE_UNIT] == unit


async def test_1001_ml_is_rejected_by_the_forms_outer_bound(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    result = await hass.config_entries.options.async_init(init_integration.entry_id)
    with pytest.raises(InvalidData):
        await hass.config_entries.options.async_configure(
            result["flow_id"],
            {CONF_BOTTLE_SIZE: 1001, CONF_BOTTLE_UNIT: "mL", CONF_BOTTLE_TYPE: "formula"},
        )


async def test_33_oz_shows_a_translated_error_and_the_form_recovers(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    size, unit = 33, "fl. oz."
    result = await hass.config_entries.options.async_init(init_integration.entry_id)
    bad = {CONF_BOTTLE_SIZE: size, CONF_BOTTLE_UNIT: unit, CONF_BOTTLE_TYPE: "pumped"}

    result = await hass.config_entries.options.async_configure(result["flow_id"], bad)

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_BOTTLE_SIZE: "bottle_too_large"}
    assert init_integration.options.get(CONF_BOTTLE_SIZE) != size
    strings = json.loads((INTEGRATION / "strings.json").read_text())
    assert strings["options"]["error"]["bottle_too_large"]

    good = {**bad, CONF_BOTTLE_SIZE: 4}
    result = await hass.config_entries.options.async_configure(result["flow_id"], good)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert init_integration.options[CONF_BOTTLE_SIZE] == 4


async def test_the_form_after_an_error_keeps_what_was_typed(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    result = await hass.config_entries.options.async_init(init_integration.entry_id)
    bad = {CONF_BOTTLE_SIZE: 40, CONF_BOTTLE_UNIT: "fl. oz.", CONF_BOTTLE_TYPE: "pumped"}

    result = await hass.config_entries.options.async_configure(result["flow_id"], bad)

    assert schema_defaults(result) == bad


@pytest.mark.parametrize(
    ("units", "expected"),
    [
        (US_CUSTOMARY_SYSTEM, (4, "fl. oz.", "formula")),
        (METRIC_SYSTEM, (120, "mL", "formula")),
    ],
    ids=["us-customary", "metric"],
)
async def test_options_default_the_bottle_to_4_oz_in_us_customary_and_120_ml_in_metric(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    units: UnitSystem,
    expected: tuple[Any, ...],
) -> None:
    hass.config.units = units

    result = await hass.config_entries.options.async_init(init_integration.entry_id)

    defaults = schema_defaults(result)
    assert (
        defaults[CONF_BOTTLE_SIZE],
        defaults[CONF_BOTTLE_UNIT],
        defaults[CONF_BOTTLE_TYPE],
    ) == expected


async def test_options_show_the_saved_bottle(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    hass.config_entries.async_update_entry(
        init_integration,
        options={CONF_BOTTLE_SIZE: 90, CONF_BOTTLE_UNIT: "mL", CONF_BOTTLE_TYPE: "pumped"},
    )

    result = await hass.config_entries.options.async_init(init_integration.entry_id)

    defaults = schema_defaults(result)
    assert (defaults[CONF_BOTTLE_SIZE], defaults[CONF_BOTTLE_UNIT]) == (90, "mL")
    assert defaults[CONF_BOTTLE_TYPE] == "pumped"


async def test_saving_the_default_bottle_in_a_metric_home_saves_120_ml_of_formula(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    hass.config.units = METRIC_SYSTEM
    result = await hass.config_entries.options.async_init(init_integration.entry_id)
    defaults = schema_defaults(result)

    await set_options(hass, init_integration, **defaults)

    assert init_integration.options[CONF_BOTTLE_SIZE] == 120
    assert init_integration.options[CONF_BOTTLE_UNIT] == "mL"
    assert init_integration.options[CONF_BOTTLE_TYPE] == "formula"


async def test_saved_ounce_options_are_shown_unchanged_in_a_metric_home(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    hass.config.units = METRIC_SYSTEM
    hass.config_entries.async_update_entry(
        init_integration,
        options={CONF_BOTTLE_SIZE: 3, CONF_BOTTLE_UNIT: "fl. oz.", CONF_BOTTLE_TYPE: "pumped"},
    )

    result = await hass.config_entries.options.async_init(init_integration.entry_id)

    defaults = schema_defaults(result)
    assert (defaults[CONF_BOTTLE_SIZE], defaults[CONF_BOTTLE_UNIT]) == (3, "fl. oz.")


@pytest.mark.parametrize(
    "bad",
    [
        {CONF_BOTTLE_SIZE: 0},
        {CONF_BOTTLE_SIZE: -4},
        {CONF_BOTTLE_UNIT: "cups"},
        {CONF_BOTTLE_TYPE: "milk"},
    ],
    ids=["zero-size", "negative-size", "unknown-unit", "unknown-type"],
)
async def test_options_reject_a_bad_bottle(
    hass: HomeAssistant, init_integration: MockConfigEntry, bad: dict[str, Any]
) -> None:
    good = {
        CONF_BOTTLE_SIZE: 4,
        CONF_BOTTLE_UNIT: "fl. oz.",
        CONF_BOTTLE_TYPE: "formula",
    }
    result = await hass.config_entries.options.async_init(init_integration.entry_id)
    with pytest.raises(InvalidData):
        await hass.config_entries.options.async_configure(result["flow_id"], {**good, **bad})


async def test_the_options_flow_has_the_bottle_fields_and_no_interval(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    result = await hass.config_entries.options.async_init(init_integration.entry_id)

    assert result["type"] is FlowResultType.FORM
    assert {str(key) for key in result["data_schema"].schema} == BOTTLE_OPTIONS


async def test_the_options_flow_refuses_an_interval(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    result = await hass.config_entries.options.async_init(init_integration.entry_id)

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
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    result = await hass.config_entries.options.async_init(init_integration.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_BOTTLE_SIZE: 5.5, CONF_BOTTLE_UNIT: "fl. oz.", CONF_BOTTLE_TYPE: "pumped"},
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert init_integration.options == {
        CONF_BOTTLE_SIZE: 5.5,
        CONF_BOTTLE_UNIT: "fl. oz.",
        CONF_BOTTLE_TYPE: "pumped",
    }


async def test_saving_the_options_drops_a_legacy_interval(
    hass: HomeAssistant, server: FakeServer
) -> None:
    entry = await entry_with_options(hass, {LEGACY_INTERVAL: 15, CONF_BOTTLE_SIZE: 6})

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert {str(key) for key in result["data_schema"].schema} == BOTTLE_OPTIONS
    await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_BOTTLE_SIZE: 6, CONF_BOTTLE_UNIT: "fl. oz.", CONF_BOTTLE_TYPE: "formula"},
    )
    await hass.async_block_till_done()

    assert LEGACY_INTERVAL not in entry.options
    assert entry.state is ConfigEntryState.LOADED
    assert await sync_interval(hass, server) == timedelta(minutes=5)


async def test_an_options_change_leaves_a_running_timer_at_five_minutes(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    await toggle(hass, SLEEP_SWITCH)

    result = await hass.config_entries.options.async_init(init_integration.entry_id)
    await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_BOTTLE_SIZE: 5, CONF_BOTTLE_UNIT: "fl. oz.", CONF_BOTTLE_TYPE: "formula"},
    )
    await hass.async_block_till_done()

    assert await sync_interval(hass, server) == timedelta(minutes=5)


async def test_saving_the_options_neither_reloads_nor_syncs(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    clients = len(server.clients)
    calls = server.device_list_calls

    result = await hass.config_entries.options.async_init(init_integration.entry_id)
    await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_BOTTLE_SIZE: 5, CONF_BOTTLE_UNIT: "fl. oz.", CONF_BOTTLE_TYPE: "formula"},
    )
    await hass.async_block_till_done()

    assert init_integration.state is ConfigEntryState.LOADED
    assert len(server.clients) == clients  # not reloaded: no new client signed in
    assert server.device_list_calls == calls


async def test_options_show_one_form(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    result = await hass.config_entries.options.async_init(init_integration.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"


async def test_options_errors_keep_the_form_open_and_the_flow_recovers(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    result = await hass.config_entries.options.async_init(init_integration.entry_id)
    good = {CONF_BOTTLE_SIZE: 5.5, CONF_BOTTLE_UNIT: "fl. oz.", CONF_BOTTLE_TYPE: "pumped"}

    for bad in (
        {**good, CONF_BOTTLE_SIZE: 0},  # below the smallest bottle
        {**good, CONF_BOTTLE_SIZE: 5000},  # more than any bottle
        {**good, CONF_BOTTLE_UNIT: "cups"},  # not a unit the form offers
        {**good, CONF_BOTTLE_TYPE: "juice"},  # not a bottle kind
    ):
        with pytest.raises(InvalidData):
            await hass.config_entries.options.async_configure(result["flow_id"], bad)

    result = await hass.config_entries.options.async_configure(result["flow_id"], good)

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert init_integration.options == good


def test_config_flow_takes_its_bottle_options_from_bottle_not_writes() -> None:
    source = Path(config_flow.__file__).read_text()
    imported = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom):
            imported.add(f"{'.' * node.level}{node.module or ''}")
            imported.update(f"{'.' * node.level}{node.module or ''}.{a.name}" for a in node.names)
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)

    assert not {name for name in imported if name.endswith("writes")}
    assert ".bottle" in imported


async def test_changing_the_bottle_options_does_not_reload_the_entry(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    clients = len(server.clients)

    await set_options(
        hass,
        init_integration,
        **{CONF_BOTTLE_SIZE: 5, CONF_BOTTLE_UNIT: "fl. oz.", CONF_BOTTLE_TYPE: "pumped"},
    )

    assert init_integration.state is ConfigEntryState.LOADED
    assert len(server.clients) == clients  # not reloaded: no new client signed in
    assert init_integration.options[CONF_BOTTLE_SIZE] == 5


async def test_options_save_only_the_bottle(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    await set_options(
        hass,
        init_integration,
        **{CONF_BOTTLE_SIZE: 5.5, CONF_BOTTLE_UNIT: "fl. oz.", CONF_BOTTLE_TYPE: "pumped"},
    )

    assert init_integration.options == {
        CONF_BOTTLE_SIZE: 5.5,
        CONF_BOTTLE_UNIT: "fl. oz.",
        CONF_BOTTLE_TYPE: "pumped",
    }
