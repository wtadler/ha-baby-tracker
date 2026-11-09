"""Core PR 27: a sync reset in the app stops syncing and raises a repair, and fixing it joins the
new sync group from scratch."""

import asyncio
import json
from typing import Any

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component
from pybabytracker import NoResponseError
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.diagnostics import (
    get_diagnostics_for_config_entry,
)
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

from custom_components.baby_tracker.const import DOMAIN

from . import BABY_THREE, DIAPER, ENTRY_ID, NEW_PASSWORD, NOW, tick
from .factories import BABY_ONE_ID, DEVICE_B, at
from .fake_server import FakeServer
from .helpers import call, log_diaper, reauth_flows

ISSUE_ID = f"sync_group_changed_{ENTRY_ID}"
NEW_GROUP = 200
NEW_GROUP_DIAPER = "sensor.baby_three_last_diaper"
OLD_RECORD = "D0000000-0000-4000-8000-000000000001"
STORAGE_KEY = f"{DOMAIN}.{ENTRY_ID}"


@pytest.fixture(autouse=True)
def _start_at_now(freezer: FrozenDateTimeFactory) -> None:
    """Freeze the clock at NOW before anything is set up, so ticks reach the timers."""
    freezer.move_to(NOW)


def reset_sync(server: FakeServer, *, with_new_records: bool = False) -> None:
    """Reset the sync group in the app: the service now answers with another AccountID."""
    server.account_id = NEW_GROUP
    if with_new_records:
        server.logs.clear()
        server.names.clear()
        server.names[DEVICE_B] = "Other Phone"
        server.add_record(
            DEVICE_B,
            "Diaper",
            "D0000000-0000-4000-8000-0000000000B1",
            when=at(3),
            baby=BABY_THREE,
            status=2,
        )


def saved_records(hass_storage: dict[str, Any]) -> str:
    """The saved sync state's records as text, or an empty string when nothing is saved."""
    saved = hass_storage.get(STORAGE_KEY)
    return json.dumps(saved["data"]["records"]) if saved else ""


def issue(hass: HomeAssistant) -> ir.IssueEntry | None:
    return ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_ID)


async def fix(hass: HomeAssistant) -> Any:
    """Open the issue's repair flow and confirm it, as the repairs page does."""
    assert await async_setup_component(hass, "repairs", {})
    manager = hass.data["repairs"]["flow_manager"]
    result = await manager.async_init(DOMAIN, data={"issue_id": ISSUE_ID})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "confirm"
    result = await manager.async_configure(result["flow_id"], {})
    await hass.async_block_till_done()
    return result


async def test_a_changed_sync_group_raises_a_fixable_repair_and_entities_go_unavailable(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    assert hass.states.get(DIAPER).state != "unavailable"
    reset_sync(server)
    await tick(hass)

    raised = issue(hass)
    assert raised is not None
    assert raised.is_fixable
    assert raised.is_persistent
    assert raised.severity is ir.IssueSeverity.ERROR
    assert raised.translation_key == "sync_group_changed"
    assert raised.translation_placeholders == {"account": init_integration.title}
    assert raised.data == {"entry_id": ENTRY_ID}
    assert hass.states.get(DIAPER).state == "unavailable"


async def test_the_poll_stops_instead_of_retrying_into_the_same_error(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    reset_sync(server)
    await tick(hass)
    reads = server.device_list_calls
    signed_in = len(server.sign_ins)

    for _ in range(3):
        await tick(hass)
    await init_integration.runtime_data.async_request_refresh()
    await hass.async_block_till_done()

    assert init_integration.runtime_data.update_interval is None
    assert server.device_list_calls == reads
    assert len(server.sign_ins) == signed_in
    assert hass.states.get(DIAPER).state == "unavailable"


async def test_a_write_that_finds_the_group_changed_points_at_the_repair(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    reset_sync(server)

    with pytest.raises(HomeAssistantError) as raised:
        await log_diaper(hass, BABY_ONE_ID)

    assert raised.value.translation_domain == DOMAIN
    assert raised.value.translation_key == "account_changed"
    assert server.posted == []
    assert issue(hass) is not None
    assert init_integration.runtime_data.update_interval is None
    assert hass.states.get(DIAPER).state == "unavailable"


async def test_a_write_after_the_poll_found_the_change_gets_the_same_error(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    reset_sync(server)
    await tick(hass)

    with pytest.raises(HomeAssistantError) as raised:
        await call(hass, "log_diaper", type="wet")

    assert raised.value.translation_key == "account_changed"
    assert server.posted == []


async def test_a_write_does_not_start_reauth_or_say_the_sign_in_failed(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    reset_sync(server)
    with pytest.raises(HomeAssistantError):
        await log_diaper(hass, BABY_ONE_ID)
    assert reauth_flows(hass) == []


async def test_confirming_the_repair_clears_the_state_and_joins_the_new_group(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    server: FakeServer,
    hass_storage: dict[str, Any],
) -> None:
    old = init_integration.runtime_data
    assert OLD_RECORD in saved_records(hass_storage)
    reset_sync(server, with_new_records=True)
    await tick(hass)
    assert issue(hass) is not None

    result = await fix(hass)

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert init_integration.state is ConfigEntryState.LOADED
    new = init_integration.runtime_data
    assert new is not old
    assert new.client is not old.client
    assert new.engine is not old.engine
    assert new.engine.account_id == NEW_GROUP
    assert OLD_RECORD not in new.engine.records
    assert OLD_RECORD not in saved_records(hass_storage)
    assert hass_storage[STORAGE_KEY]["data"]["account_id"] == NEW_GROUP
    assert hass.states.get(NEW_GROUP_DIAPER).state != "unavailable"
    assert new.update_interval is not None
    assert issue(hass) is None
    assert old.client.closed


async def test_the_new_group_keeps_syncing_after_the_fix(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    reset_sync(server, with_new_records=True)
    await tick(hass)
    await fix(hass)
    reads = server.device_list_calls

    await tick(hass)

    assert server.device_list_calls > reads
    assert issue(hass) is None


async def test_the_repair_fixes_an_entry_that_failed_to_set_up_after_a_restart(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    reset_sync(server, with_new_records=True)
    await tick(hass)
    await hass.config_entries.async_reload(ENTRY_ID)
    await hass.async_block_till_done()
    assert init_integration.state is ConfigEntryState.SETUP_ERROR
    assert issue(hass) is not None

    result = await fix(hass)

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert init_integration.state is ConfigEntryState.LOADED
    assert hass.states.get(NEW_GROUP_DIAPER).state != "unavailable"
    assert issue(hass) is None


async def test_the_repair_keeps_the_issue_when_the_reload_cannot_finish(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    server: FakeServer,
    hass_storage: dict[str, Any],
) -> None:
    reset_sync(server, with_new_records=True)
    await tick(hass)
    server.request_error = NoResponseError("no answer")

    result = await fix(hass)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reload_failed"
    assert init_integration.state is ConfigEntryState.SETUP_RETRY
    assert issue(hass) is not None
    assert OLD_RECORD not in saved_records(hass_storage)


async def test_a_successful_sync_clears_the_issue_without_the_flow(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    reset_sync(server)
    await tick(hass)
    assert issue(hass) is not None
    server.account_id = 100

    await hass.config_entries.async_reload(ENTRY_ID)
    await hass.async_block_till_done()

    assert init_integration.state is ConfigEntryState.LOADED
    assert issue(hass) is None


async def test_a_rejected_password_still_starts_reauth_not_this_repair(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    server.password = NEW_PASSWORD
    await tick(hass)

    assert reauth_flows(hass) == ["reauth"]
    assert issue(hass) is None


async def test_removing_the_entry_deletes_the_issue(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    reset_sync(server)
    await tick(hass)
    assert issue(hass) is not None

    await hass.config_entries.async_remove(ENTRY_ID)

    assert issue(hass) is None


async def test_diagnostics_still_work_while_the_group_is_changed(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    init_integration: MockConfigEntry,
    server: FakeServer,
) -> None:
    reset_sync(server)
    await tick(hass)

    diagnostics = await get_diagnostics_for_config_entry(hass, hass_client, init_integration)

    assert diagnostics["coordinator"]["last_update_success"] is False
    assert diagnostics["coordinator"]["babies"] == 2
    assert set(diagnostics) == {"entry", "coordinator", "engine", "babies"}


@pytest.mark.parametrize(
    ("action", "data", "running_first"),
    [
        ("start_nursing", {"side": "left"}, False),
        ("switch_nursing_side", {}, True),
        ("discard_nursing", {}, True),
        ("start_pump", {}, False),
    ],
)
async def test_a_timer_change_after_the_stop_is_refused_and_entities_stay_unavailable(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    server: FakeServer,
    action: str,
    data: dict[str, Any],
    running_first: bool,
) -> None:
    if running_first:
        await call(hass, "start_nursing", side="left")
    reset_sync(server)
    await tick(hass)
    assert hass.states.get(DIAPER).state == "unavailable"

    with pytest.raises(HomeAssistantError) as raised:
        await call(hass, action, **data)
    await hass.async_block_till_done()

    assert raised.value.translation_key == "account_changed"
    assert hass.states.get(DIAPER).state == "unavailable"
    assert init_integration.runtime_data.last_update_success is False


async def test_a_write_after_the_stop_sends_nothing_and_does_not_sign_in(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    reset_sync(server)
    await tick(hass)
    signed_in = len(server.sign_ins)
    reads = server.device_list_calls

    with pytest.raises(HomeAssistantError) as raised:
        await log_diaper(hass, BABY_ONE_ID)

    assert raised.value.translation_key == "account_changed"
    assert len(server.sign_ins) == signed_in
    assert server.device_list_calls == reads
    assert server.posted == []


async def test_the_fix_waits_for_a_write_in_flight_before_it_deletes_the_state(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    server: FakeServer,
    hass_storage: dict[str, Any],
) -> None:
    reset_sync(server, with_new_records=True)
    await tick(hass)
    old = init_integration.runtime_data
    assert STORAGE_KEY in hass_storage
    assert await async_setup_component(hass, "repairs", {})
    manager = hass.data["repairs"]["flow_manager"]
    opened = await manager.async_init(DOMAIN, data={"issue_id": ISSUE_ID})

    async with old.exclusive():  # a write that started earlier, still holding the lock
        confirming = hass.async_create_task(manager.async_configure(opened["flow_id"], {}))
        for _ in range(10):
            await asyncio.sleep(0)
        assert not confirming.done()
        assert STORAGE_KEY in hass_storage
        assert init_integration.runtime_data is old
        # The write finishes by saving the old group's state, as every write does.
        await old._async_save()  # noqa: SLF001

    result = await confirming
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert OLD_RECORD not in saved_records(hass_storage)
    assert hass_storage[STORAGE_KEY]["data"]["account_id"] == NEW_GROUP
    assert init_integration.state is ConfigEntryState.LOADED
