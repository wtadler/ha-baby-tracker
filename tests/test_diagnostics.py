"""Core PR 3: the diagnostics download, useful for a bug report, with nothing that identifies
the account."""

import json
from typing import Any

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.diagnostics import (
    get_diagnostics_for_config_entry,
)
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator
from syrupy.assertion import SnapshotAssertion

from custom_components.baby_tracker.const import CONF_DEVICE_UUID

from . import BABY_ONE, BABY_TWO, EMAIL, NEW_PASSWORD, PASSWORD, tick
from .factories import BABY_ONE_ID, BABY_TWO_ID, DEVICE_A, at
from .fake_server import FakeServer


async def _diagnostics(
    hass: HomeAssistant, hass_client: ClientSessionGenerator, entry: MockConfigEntry
) -> dict[str, Any]:
    return await get_diagnostics_for_config_entry(hass, hass_client, entry)


async def test_diagnostics_match_the_snapshot(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    init_integration: MockConfigEntry,
    snapshot: SnapshotAssertion,
) -> None:
    assert await _diagnostics(hass, hass_client, init_integration) == snapshot


async def test_diagnostics_hold_no_email_or_password(
    hass: HomeAssistant, hass_client: ClientSessionGenerator, init_integration: MockConfigEntry
) -> None:
    diagnostics = await _diagnostics(hass, hass_client, init_integration)
    text = json.dumps(diagnostics)
    assert EMAIL not in text
    assert PASSWORD not in text
    assert "parent" not in text.lower()
    assert diagnostics["entry"]["data"]["email"] == "**REDACTED**"
    assert diagnostics["entry"]["data"]["password"] == "**REDACTED**"


async def test_diagnostics_hold_no_baby_name_or_id_and_no_device_id(
    hass: HomeAssistant, hass_client: ClientSessionGenerator, init_integration: MockConfigEntry
) -> None:
    text = json.dumps(await _diagnostics(hass, hass_client, init_integration))
    for secret in (
        BABY_ONE["name"],
        BABY_TWO["name"],
        BABY_ONE_ID,
        BABY_TWO_ID,
        DEVICE_A,
        init_integration.data[CONF_DEVICE_UUID],
        init_integration.entry_id,
    ):
        assert secret not in text, secret


async def test_diagnostics_hold_no_notes(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    mock_config_entry: MockConfigEntry,
    server: FakeServer,
) -> None:
    server.add_record(
        DEVICE_A,
        "Diaper",
        "D0000000-0000-4000-8000-000000000003",
        when=at(20),
        note="a private note about the baby",
        status=0,
    )
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    text = json.dumps(await _diagnostics(hass, hass_client, mock_config_entry))
    assert "private note" not in text


async def test_diagnostics_count_the_records_and_babies(
    hass: HomeAssistant, hass_client: ClientSessionGenerator, init_integration: MockConfigEntry
) -> None:
    diagnostics = await _diagnostics(hass, hass_client, init_integration)
    assert diagnostics["coordinator"]["babies"] == 2
    assert diagnostics["coordinator"]["last_update_success"] is True
    assert "update_interval_seconds" not in diagnostics["coordinator"]
    assert diagnostics["engine"]["records_by_type"]["Diaper"] == 2


async def test_diagnostics_report_a_failed_sync(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    init_integration: MockConfigEntry,
    server: FakeServer,
) -> None:
    server.password = NEW_PASSWORD
    await tick(hass)
    diagnostics = await _diagnostics(hass, hass_client, init_integration)
    assert diagnostics["coordinator"]["last_update_success"] is False
