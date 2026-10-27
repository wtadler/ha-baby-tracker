"""The setup screen, re-sign-in, and the options screen."""

import uuid
from datetime import timedelta
from typing import Any
from unittest.mock import AsyncMock

import pytest
from homeassistant.config_entries import SOURCE_USER, ConfigEntryState
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType, InvalidData
from homeassistant.util.unit_system import METRIC_SYSTEM, US_CUSTOMARY_SYSTEM
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.nighp_baby_tracker.const import (
    CONF_BOTTLE_SIZE,
    CONF_BOTTLE_TYPE,
    CONF_BOTTLE_UNIT,
    CONF_DEVICE_UUID,
    DOMAIN,
)
from custom_components.nighp_baby_tracker.pybabytracker import (
    AuthenticationError,
    BabyTrackerConnectionError,
    ProtocolError,
)

from . import EMAIL, NEW_PASSWORD, PASSWORD
from .fake_server import FakeServer
from .helpers import (
    LEGACY_INTERVAL,
    SLEEP_BUTTON,
    entry_with_options,
    press,
    set_options,
    sync_interval,
)


def schema_defaults(result: Any) -> dict[str, Any]:
    return {str(key): key.default() for key in result["data_schema"].schema}


BOTTLE_OPTIONS = {CONF_BOTTLE_SIZE, CONF_BOTTLE_UNIT, CONF_BOTTLE_TYPE}


async def _start(hass: HomeAssistant):
    return await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})


async def _submit(hass: HomeAssistant, flow_id: str, email: str = EMAIL, password: str = PASSWORD):
    return await hass.config_entries.flow.async_configure(
        flow_id, {CONF_EMAIL: email, CONF_PASSWORD: password}
    )


async def _submit_password(hass: HomeAssistant, flow_id: str, password: str = PASSWORD):
    return await hass.config_entries.flow.async_configure(flow_id, {CONF_PASSWORD: password})


async def _start_reauth(hass: HomeAssistant, entry: MockConfigEntry):
    entry.add_to_hass(hass)
    return await entry.start_reauth_flow(hass)


# --- the library's limits, checked here first with translated errors ------------------------


async def test_the_bottle_size_option_is_capped_at_32_fluid_ounces(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    good = {
        CONF_BOTTLE_SIZE: 32,
        CONF_BOTTLE_UNIT: "fl. oz.",
        CONF_BOTTLE_TYPE: "formula",
    }
    await set_options(hass, init_integration, **good)
    result = await hass.config_entries.options.async_init(init_integration.entry_id)
    with pytest.raises(InvalidData):
        await hass.config_entries.options.async_configure(
            result["flow_id"], {**good, CONF_BOTTLE_SIZE: 33}
        )


# --- the options screen ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("units", "expected"),
    [
        (US_CUSTOMARY_SYSTEM, (4, "fl. oz.", "formula")),
        (METRIC_SYSTEM, (120, "mL", "formula")),
    ],
    ids=["us-customary", "metric"],
)
async def test_options_default_the_bottle_to_4_oz_in_us_customary_and_120_ml_in_metric(
    hass: HomeAssistant, init_integration: MockConfigEntry, units: Any, expected: tuple[Any, ...]
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


# The options flow


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


async def test_an_options_change_leaves_a_running_timer_at_one_minute(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    await press(hass, SLEEP_BUTTON)

    result = await hass.config_entries.options.async_init(init_integration.entry_id)
    await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_BOTTLE_SIZE: 5, CONF_BOTTLE_UNIT: "fl. oz.", CONF_BOTTLE_TYPE: "formula"},
    )
    await hass.async_block_till_done()

    assert await sync_interval(hass, server) == timedelta(minutes=1)


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


async def test_shows_the_email_and_password_form(hass: HomeAssistant) -> None:
    result = await _start(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {}
    assert set(result["data_schema"].schema) == {CONF_EMAIL, CONF_PASSWORD}


async def test_full_flow_creates_one_entry(
    hass: HomeAssistant, server: FakeServer, mock_setup_entry: AsyncMock
) -> None:
    result = await _start(hass)
    result = await _submit(hass, result["flow_id"])

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == EMAIL
    assert result["data"][CONF_EMAIL] == EMAIL
    assert result["data"][CONF_PASSWORD] == PASSWORD
    assert result["result"].unique_id == EMAIL
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1
    assert len(mock_setup_entry.mock_calls) == 1


async def test_the_entry_has_a_generated_device_id_the_login_check_used(
    hass: HomeAssistant, server: FakeServer, mock_setup_entry: AsyncMock
) -> None:
    result = await _start(hass)
    result = await _submit(hass, result["flow_id"])

    device_uuid = result["data"][CONF_DEVICE_UUID]
    assert str(uuid.UUID(device_uuid)).upper() == device_uuid.upper()
    first_sign_in = server.sign_ins[0]
    assert first_sign_in["device_uuid"] == device_uuid
    assert first_sign_in["device_name"] == "Home Assistant"


async def test_each_new_entry_gets_its_own_device_id(
    hass: HomeAssistant, server: FakeServer, mock_setup_entry: AsyncMock
) -> None:
    first = await _submit(hass, (await _start(hass))["flow_id"], email="one@example.com")
    second = await _submit(hass, (await _start(hass))["flow_id"], email="two@example.com")
    assert first["data"][CONF_DEVICE_UUID] != second["data"][CONF_DEVICE_UUID]
    assert len(mock_setup_entry.mock_calls) == 2


async def test_the_login_check_client_is_closed(
    hass: HomeAssistant, server: FakeServer, mock_setup_entry: AsyncMock
) -> None:
    result = await _start(hass)
    await _submit(hass, result["flow_id"])
    assert server.clients[0].closed


@pytest.mark.parametrize(
    ("break_login", "error"),
    [
        (lambda server: setattr(server, "password", "test-other"), "invalid_auth"),
        (
            lambda server: setattr(server, "sign_in_error", AuthenticationError("rejected")),
            "invalid_auth",
        ),
        (
            lambda server: setattr(server, "sign_in_error", BabyTrackerConnectionError("down")),
            "cannot_connect",
        ),
        (lambda server: setattr(server, "sign_in_error", ProtocolError("odd")), "unknown"),
        (lambda server: setattr(server, "sign_in_error", RuntimeError("surprise")), "unknown"),
    ],
    ids=["wrong-password", "auth-error", "cannot-connect", "protocol-error", "unexpected-error"],
)
async def test_form_errors_show_on_the_form_and_the_flow_recovers(
    hass: HomeAssistant,
    server: FakeServer,
    mock_setup_entry: AsyncMock,
    break_login,
    error: str,
) -> None:
    break_login(server)
    result = await _start(hass)
    result = await _submit(hass, result["flow_id"])

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {"base": error}
    assert hass.config_entries.async_entries(DOMAIN) == []
    assert not mock_setup_entry.mock_calls
    assert all(client.closed for client in server.clients)

    # The login works again: the same form finishes, so the flow is able to recover.
    server.password = PASSWORD
    server.sign_in_error = None
    result = await _submit(hass, result["flow_id"])

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == EMAIL
    assert result["data"][CONF_EMAIL] == EMAIL
    assert result["data"][CONF_PASSWORD] == PASSWORD
    assert result["result"].unique_id == EMAIL
    assert len(mock_setup_entry.mock_calls) == 1


async def test_the_form_keeps_what_was_typed_after_an_error(
    hass: HomeAssistant, server: FakeServer
) -> None:
    result = await _start(hass)
    result = await _submit(hass, result["flow_id"], password="test-wrong")

    suggested = {str(key): key.description for key in result["data_schema"].schema}
    assert suggested[CONF_EMAIL] == {"suggested_value": EMAIL}


async def test_an_account_already_set_up_aborts(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    mock_config_entry.add_to_hass(hass)
    result = await _start(hass)
    result = await _submit(hass, result["flow_id"])

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_the_same_account_in_a_different_case_aborts(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    mock_config_entry.add_to_hass(hass)
    result = await _start(hass)
    result = await _submit(hass, result["flow_id"], email=EMAIL.upper())

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_the_unique_id_is_the_lowercased_email(
    hass: HomeAssistant, server: FakeServer, mock_setup_entry: AsyncMock
) -> None:
    result = await _start(hass)
    result = await _submit(hass, result["flow_id"], email="Parent@Example.COM")
    entry = result["result"]
    assert entry.unique_id == "parent@example.com"


async def test_a_second_account_can_be_added(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    server: FakeServer,
    mock_setup_entry: AsyncMock,
) -> None:
    mock_config_entry.add_to_hass(hass)
    result = await _start(hass)
    result = await _submit(hass, result["flow_id"], email="other@example.com")
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert len(hass.config_entries.async_entries(DOMAIN)) == 2


# Re-sign-in


async def test_reauth_asks_for_the_password_only(hass: HomeAssistant, mock_config_entry) -> None:
    result = await _start_reauth(hass, mock_config_entry)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"
    assert {str(key) for key in result["data_schema"].schema} == {CONF_PASSWORD}


async def test_reauth_shows_the_email_as_a_placeholder(
    hass: HomeAssistant, mock_config_entry
) -> None:
    result = await _start_reauth(hass, mock_config_entry)

    assert result["description_placeholders"]["email"] == EMAIL


async def test_reauth_refuses_an_email_field(hass: HomeAssistant, mock_config_entry) -> None:
    result = await _start_reauth(hass, mock_config_entry)

    with pytest.raises(InvalidData):
        await _submit(hass, result["flow_id"], password=NEW_PASSWORD)


async def test_reauth_with_a_new_password_restores_the_entry(
    hass: HomeAssistant, mock_config_entry, server: FakeServer
) -> None:
    server.password = NEW_PASSWORD
    result = await _start_reauth(hass, mock_config_entry)
    result = await _submit_password(hass, result["flow_id"], NEW_PASSWORD)
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert mock_config_entry.data[CONF_PASSWORD] == NEW_PASSWORD
    assert mock_config_entry.data[CONF_EMAIL] == EMAIL
    assert mock_config_entry.state is ConfigEntryState.LOADED


async def test_reauth_signs_in_with_the_entrys_email(
    hass: HomeAssistant, mock_config_entry, server: FakeServer
) -> None:
    server.password = NEW_PASSWORD
    result = await _start_reauth(hass, mock_config_entry)
    await _submit_password(hass, result["flow_id"], NEW_PASSWORD)
    await hass.async_block_till_done()

    assert server.sign_ins[0]["email"] == EMAIL
    assert server.sign_ins[0]["password"] == NEW_PASSWORD


async def test_reauth_keeps_the_device_id(hass: HomeAssistant, mock_config_entry, server) -> None:
    original = mock_config_entry.data[CONF_DEVICE_UUID]
    server.password = NEW_PASSWORD
    result = await _start_reauth(hass, mock_config_entry)
    await _submit_password(hass, result["flow_id"], NEW_PASSWORD)
    await hass.async_block_till_done()

    assert mock_config_entry.data[CONF_DEVICE_UUID] == original
    assert {sign_in["device_uuid"] for sign_in in server.sign_ins} == {original}


@pytest.mark.parametrize(
    ("break_login", "error"),
    [
        (lambda server: setattr(server, "password", "test-other"), "invalid_auth"),
        (
            lambda server: setattr(server, "sign_in_error", AuthenticationError("rejected")),
            "invalid_auth",
        ),
        (
            lambda server: setattr(server, "sign_in_error", BabyTrackerConnectionError("down")),
            "cannot_connect",
        ),
        (lambda server: setattr(server, "sign_in_error", ProtocolError("odd")), "unknown"),
        (lambda server: setattr(server, "sign_in_error", RuntimeError("surprise")), "unknown"),
    ],
    ids=["wrong-password", "auth-error", "cannot-connect", "protocol-error", "unexpected-error"],
)
async def test_reauth_errors_show_on_the_form_and_the_flow_recovers(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    server: FakeServer,
    break_login,
    error: str,
) -> None:
    server.password = NEW_PASSWORD
    result = await _start_reauth(hass, mock_config_entry)
    break_login(server)
    result = await _submit_password(hass, result["flow_id"], NEW_PASSWORD)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"
    assert result["errors"] == {"base": error}
    assert result["description_placeholders"]["email"] == EMAIL
    assert mock_config_entry.data[CONF_PASSWORD] == PASSWORD

    # The login works again: the same form finishes, so the flow is able to recover.
    server.password = NEW_PASSWORD
    server.sign_in_error = None
    result = await _submit_password(hass, result["flow_id"], NEW_PASSWORD)
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert mock_config_entry.data[CONF_PASSWORD] == NEW_PASSWORD
    assert mock_config_entry.state is ConfigEntryState.LOADED


async def test_reauth_for_a_different_account_aborts_with_unique_id_mismatch(
    hass: HomeAssistant, server: FakeServer
) -> None:
    # An entry whose saved email isn't the account its unique ID names.
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=EMAIL,
        unique_id="someone-else@example.com",
        data={
            CONF_EMAIL: EMAIL,
            CONF_PASSWORD: PASSWORD,
            CONF_DEVICE_UUID: "EEEEEEEE-0000-4000-8000-0000000000EE",
        },
    )
    result = await _start_reauth(hass, entry)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "unique_id_mismatch"
    assert entry.data[CONF_PASSWORD] == PASSWORD


# Options (the interval is gone; test_polling.py covers that, and the bottle fields live in


async def test_options_show_one_form(hass: HomeAssistant, init_integration) -> None:
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
        {**good, CONF_BOTTLE_SIZE: 100},  # more than any bottle
        {**good, CONF_BOTTLE_UNIT: "cups"},  # not a unit the form offers
        {**good, CONF_BOTTLE_TYPE: "juice"},  # not a bottle kind
    ):
        with pytest.raises(InvalidData):
            await hass.config_entries.options.async_configure(result["flow_id"], bad)

    result = await hass.config_entries.options.async_configure(result["flow_id"], good)

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert init_integration.options == good
