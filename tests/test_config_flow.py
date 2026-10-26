"""The setup screen, re-sign-in, and the options screen."""

from __future__ import annotations

import uuid

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType, InvalidData
from homeassistant.config_entries import SOURCE_USER
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.nighp_baby_tracker.const import (
    CONF_DEVICE_UUID,
    CONF_EMAIL,
    CONF_PASSWORD,
    CONF_SCAN_INTERVAL,
    DOMAIN,
)
from custom_components.nighp_baby_tracker.pybabytracker import (
    BabyTrackerConnectionError,
    ProtocolError,
)

from .conftest import EMAIL, NEW_PASSWORD, PASSWORD, FakeServer


async def _start(hass: HomeAssistant):
    return await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})


async def _submit(hass: HomeAssistant, flow_id: str, email: str = EMAIL, password: str = PASSWORD):
    return await hass.config_entries.flow.async_configure(
        flow_id, {CONF_EMAIL: email, CONF_PASSWORD: password}
    )


async def test_shows_the_email_and_password_form(hass: HomeAssistant) -> None:
    result = await _start(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert set(result["data_schema"].schema) == {CONF_EMAIL, CONF_PASSWORD}


async def test_valid_credentials_create_one_entry(hass: HomeAssistant, server: FakeServer) -> None:
    result = await _start(hass)
    result = await _submit(hass, result["flow_id"])

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == EMAIL
    assert result["data"][CONF_EMAIL] == EMAIL
    assert result["data"][CONF_PASSWORD] == PASSWORD
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1


async def test_the_entry_has_a_generated_device_id_the_login_check_used(
    hass: HomeAssistant, server: FakeServer
) -> None:
    result = await _start(hass)
    result = await _submit(hass, result["flow_id"])

    device_uuid = result["data"][CONF_DEVICE_UUID]
    assert str(uuid.UUID(device_uuid)).upper() == device_uuid.upper()
    first_sign_in = server.sign_ins[0]
    assert first_sign_in["device_uuid"] == device_uuid
    assert first_sign_in["device_name"] == "Home Assistant"


async def test_each_new_entry_gets_its_own_device_id(
    hass: HomeAssistant, server: FakeServer
) -> None:
    first = await _submit(hass, (await _start(hass))["flow_id"], email="one@example.com")
    second = await _submit(hass, (await _start(hass))["flow_id"], email="two@example.com")
    assert first["data"][CONF_DEVICE_UUID] != second["data"][CONF_DEVICE_UUID]


async def test_the_login_check_client_is_closed(hass: HomeAssistant, server: FakeServer) -> None:
    result = await _start(hass)
    await _submit(hass, result["flow_id"])
    assert server.clients[0].closed


async def test_a_bad_password_shows_invalid_auth(hass: HomeAssistant, server: FakeServer) -> None:
    result = await _start(hass)
    result = await _submit(hass, result["flow_id"], password="test-wrong")

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}
    assert hass.config_entries.async_entries(DOMAIN) == []
    assert server.clients[0].closed


async def test_an_unreachable_server_shows_cannot_connect(
    hass: HomeAssistant, server: FakeServer
) -> None:
    server.sign_in_error = BabyTrackerConnectionError("down")
    result = await _start(hass)
    result = await _submit(hass, result["flow_id"])

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}
    assert hass.config_entries.async_entries(DOMAIN) == []


async def test_a_reply_in_the_wrong_shape_shows_unknown(
    hass: HomeAssistant, server: FakeServer
) -> None:
    server.sign_in_error = ProtocolError("odd")
    result = await _start(hass)
    result = await _submit(hass, result["flow_id"])

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "unknown"}


async def test_the_form_recovers_after_an_error(hass: HomeAssistant, server: FakeServer) -> None:
    result = await _start(hass)
    result = await _submit(hass, result["flow_id"], password="test-wrong")
    assert result["errors"] == {"base": "invalid_auth"}

    result = await _submit(hass, result["flow_id"])
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_an_account_already_set_up_aborts(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    config_entry.add_to_hass(hass)
    result = await _start(hass)
    result = await _submit(hass, result["flow_id"])

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_the_same_account_in_a_different_case_aborts(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    config_entry.add_to_hass(hass)
    result = await _start(hass)
    result = await _submit(hass, result["flow_id"], email=EMAIL.upper())

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_the_unique_id_is_the_lowercased_email(
    hass: HomeAssistant, server: FakeServer
) -> None:
    result = await _start(hass)
    result = await _submit(hass, result["flow_id"], email="Parent@Example.COM")
    entry = result["result"]
    assert entry.unique_id == "parent@example.com"


async def test_a_second_account_can_be_added(
    hass: HomeAssistant, config_entry: MockConfigEntry, server: FakeServer
) -> None:
    config_entry.add_to_hass(hass)
    result = await _start(hass)
    result = await _submit(hass, result["flow_id"], email="other@example.com")
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert len(hass.config_entries.async_entries(DOMAIN)) == 2


# Re-sign-in


async def _start_reauth(hass: HomeAssistant, entry: MockConfigEntry):
    entry.add_to_hass(hass)
    return await entry.start_reauth_flow(hass)


async def test_reauth_asks_for_the_account(hass: HomeAssistant, config_entry) -> None:
    result = await _start_reauth(hass, config_entry)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"


async def test_reauth_with_new_credentials_restores_the_entry(
    hass: HomeAssistant, config_entry, server: FakeServer
) -> None:
    server.password = NEW_PASSWORD
    result = await _start_reauth(hass, config_entry)
    result = await _submit(hass, result["flow_id"], password=NEW_PASSWORD)
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert config_entry.data[CONF_PASSWORD] == NEW_PASSWORD
    assert config_entry.state is ConfigEntryState.LOADED


async def test_reauth_keeps_the_device_id(hass: HomeAssistant, config_entry, server) -> None:
    original = config_entry.data[CONF_DEVICE_UUID]
    server.password = NEW_PASSWORD
    result = await _start_reauth(hass, config_entry)
    await _submit(hass, result["flow_id"], password=NEW_PASSWORD)
    await hass.async_block_till_done()

    assert config_entry.data[CONF_DEVICE_UUID] == original
    assert {sign_in["device_uuid"] for sign_in in server.sign_ins} == {original}


async def test_reauth_with_the_wrong_password_shows_invalid_auth(
    hass: HomeAssistant, config_entry, server: FakeServer
) -> None:
    server.password = NEW_PASSWORD
    result = await _start_reauth(hass, config_entry)
    result = await _submit(hass, result["flow_id"], password="test-still-wrong")

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}
    assert config_entry.data[CONF_PASSWORD] == PASSWORD


async def test_reauth_with_a_different_account_aborts(
    hass: HomeAssistant, config_entry, server: FakeServer
) -> None:
    result = await _start_reauth(hass, config_entry)
    result = await _submit(hass, result["flow_id"], email="someone-else@example.com")

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "wrong_account"
    assert config_entry.data[CONF_EMAIL] == EMAIL
    assert config_entry.data[CONF_PASSWORD] == PASSWORD


async def test_reauth_with_the_same_account_in_another_case_succeeds(
    hass: HomeAssistant, config_entry, server: FakeServer
) -> None:
    server.password = NEW_PASSWORD
    result = await _start_reauth(hass, config_entry)
    result = await _submit(hass, result["flow_id"], email=EMAIL.upper(), password=NEW_PASSWORD)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"


async def test_reauth_with_an_unreachable_server_shows_cannot_connect(
    hass: HomeAssistant, config_entry, server: FakeServer
) -> None:
    result = await _start_reauth(hass, config_entry)
    server.sign_in_error = BabyTrackerConnectionError("down")
    result = await _submit(hass, result["flow_id"])

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


# Options


async def test_options_default_to_five_minutes(hass: HomeAssistant, setup_entry) -> None:
    result = await hass.config_entries.options.async_init(setup_entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"
    (key,) = result["data_schema"].schema
    assert key.default() == 5


async def test_options_save_a_new_interval(hass: HomeAssistant, setup_entry) -> None:
    result = await hass.config_entries.options.async_init(setup_entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_SCAN_INTERVAL: 10}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert setup_entry.options[CONF_SCAN_INTERVAL] == 10


async def test_options_accept_the_one_minute_minimum(hass: HomeAssistant, setup_entry) -> None:
    result = await hass.config_entries.options.async_init(setup_entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_SCAN_INTERVAL: 1}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY


@pytest.mark.parametrize("interval", [0, -5])
async def test_options_reject_less_than_a_minute(
    hass: HomeAssistant, setup_entry, interval: int
) -> None:
    result = await hass.config_entries.options.async_init(setup_entry.entry_id)
    with pytest.raises(InvalidData):
        await hass.config_entries.options.async_configure(
            result["flow_id"], {CONF_SCAN_INTERVAL: interval}
        )


async def test_options_show_the_saved_interval(hass: HomeAssistant, config_entry) -> None:
    config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(config_entry, options={CONF_SCAN_INTERVAL: 15})
    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    (key,) = result["data_schema"].schema
    assert key.default() == 15
