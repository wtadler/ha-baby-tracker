"""Core PR 2: the reauthentication flow."""

from collections.abc import Callable

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType, InvalidData
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.baby_tracker.const import (
    CONF_DEVICE_UUID,
    DOMAIN,
)
from custom_components.baby_tracker.pybabytracker import (
    AuthenticationError,
    BabyTrackerConnectionError,
    ProtocolError,
)

from . import EMAIL, NEW_PASSWORD, PASSWORD
from .fake_server import FakeServer


async def _submit(hass: HomeAssistant, flow_id: str, email: str = EMAIL, password: str = PASSWORD):
    return await hass.config_entries.flow.async_configure(
        flow_id, {CONF_EMAIL: email, CONF_PASSWORD: password}
    )


async def _submit_password(hass: HomeAssistant, flow_id: str, password: str = PASSWORD):
    return await hass.config_entries.flow.async_configure(flow_id, {CONF_PASSWORD: password})


async def _start_reauth(hass: HomeAssistant, entry: MockConfigEntry):
    entry.add_to_hass(hass)
    return await entry.start_reauth_flow(hass)


async def test_reauth_asks_for_the_password_only(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    result = await _start_reauth(hass, mock_config_entry)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"
    assert {str(key) for key in result["data_schema"].schema} == {CONF_PASSWORD}


async def test_reauth_shows_the_email_as_a_placeholder(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    result = await _start_reauth(hass, mock_config_entry)

    assert result["description_placeholders"]["email"] == EMAIL


async def test_reauth_refuses_an_email_field(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    result = await _start_reauth(hass, mock_config_entry)

    with pytest.raises(InvalidData):
        await _submit(hass, result["flow_id"], password=NEW_PASSWORD)


async def test_reauth_with_a_new_password_restores_the_entry(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, server: FakeServer
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
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, server: FakeServer
) -> None:
    server.password = NEW_PASSWORD
    result = await _start_reauth(hass, mock_config_entry)
    await _submit_password(hass, result["flow_id"], NEW_PASSWORD)
    await hass.async_block_till_done()

    assert server.sign_ins[0]["email"] == EMAIL
    assert server.sign_ins[0]["password"] == NEW_PASSWORD


async def test_reauth_keeps_the_device_id(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, server: FakeServer
) -> None:
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
    break_login: Callable[[FakeServer], None],
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
