"""Core PR 2: the reauthentication flow starts when sign-in is rejected."""

from unittest.mock import patch

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.baby_tracker.const import (
    DOMAIN,
)
from custom_components.baby_tracker.pybabytracker import (
    AuthenticationError,
)

from . import (
    DIAPER,
    NEW_PASSWORD,
    NOW,
    tick,
)
from .factories import (
    DEVICE_B,
    at,
)
from .fake_server import FakeServer


@pytest.fixture(autouse=True)
def _start_at_now(freezer: FrozenDateTimeFactory) -> None:
    """Freeze the clock at NOW before anything is set up.

    Timers scheduled at setup then share the clock the tests move, so moving it forward and
    ticking reaches them the way it does in core's tests.
    """
    freezer.move_to(NOW)


async def test_a_rejected_login_at_setup_starts_reauth(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, server: FakeServer
) -> None:
    server.password = NEW_PASSWORD
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [flow["context"]["source"] for flow in flows] == ["reauth"]
    assert server.open_clients == []


async def test_a_password_that_stops_working_starts_reauth(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    server.password = NEW_PASSWORD
    await tick(hass)

    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [flow["context"]["source"] for flow in flows] == ["reauth"]
    assert hass.states.get(DIAPER).state == "unavailable"


async def test_an_auth_error_raised_mid_update_starts_reauth(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    server.request_error = AuthenticationError("session rejected")
    await tick(hass)
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [flow["context"]["source"] for flow in flows] == ["reauth"]


@pytest.mark.parametrize(
    ("error", "expected_state"),
    [(AuthenticationError("rejected"), ConfigEntryState.SETUP_ERROR)],
    ids=["auth"],
)
async def test_a_failing_save_does_not_replace_the_sync_error(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    server: FakeServer,
    caplog: pytest.LogCaptureFixture,
    error: Exception,
    expected_state: ConfigEntryState,
) -> None:
    # Device A syncs (so there is progress to save), then device B fails.
    server.add_record(DEVICE_B, "Diaper", "D9000000-0000-4000-8000-000000000001", when=at(5))
    server.read_errors[DEVICE_B] = error
    mock_config_entry.add_to_hass(hass)

    with patch("homeassistant.helpers.storage.Store.async_save", side_effect=OSError("disk full")):
        await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

    assert mock_config_entry.state is expected_state
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert flows
    assert "disk full" in caplog.text
