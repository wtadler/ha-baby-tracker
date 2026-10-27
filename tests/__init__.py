"""Tests for Baby Tracker (Nighp). Everything here is invented fixture data."""

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from .factories import BABY_ONE_ID, BABY_THREE_ID, BABY_TWO_ID, baby_payload

EMAIL = "parent@example.com"
PASSWORD = "test-password"
NEW_PASSWORD = "test-new-password"
EMAIL_TWO = "second-parent@example.com"
ENTRY_ID = "01TESTENTRY0000000000000000"
OWN_DEVICE = "EEEEEEEE-0000-4000-8000-0000000000EE"
OWN_DEVICE_TWO = "FFFFFFFF-0000-4000-8000-0000000000FF"

BABY_ONE = baby_payload("Baby One", BABY_ONE_ID)
BABY_TWO = baby_payload("Baby Two", BABY_TWO_ID)
BABY_THREE = baby_payload("Baby Three", BABY_THREE_ID)


async def setup_integration(hass: HomeAssistant, mock_config_entry: MockConfigEntry) -> None:
    """Add the entry to Home Assistant and set it up, as core's integration tests do."""
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
