"""Diaper buttons, the log_diaper action, and undo."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import voluptuous as vol
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.storage import Store
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.nighp_baby_tracker.const import (
    CONF_DEVICE_UUID,
    CONF_EMAIL,
    CONF_PASSWORD,
    DOMAIN,
)
from custom_components.nighp_baby_tracker.pybabytracker import (
    OP_ADD,
    OP_DELETE,
    OP_UPDATE,
    AuthenticationError,
    BabyTrackerConnectionError,
    Diaper,
    NoResponseError,
    SyncEngine,
    decode_transaction,
)

from .conftest import EMAIL, PASSWORD, FakeServer
from .factories import BABY_ONE_ID, BABY_THREE_ID, BABY_TWO_ID, DEVICE_A, DEVICE_B, at, baby_payload
from .write_fakes import WriteClient, add_write_state

OWN_DEVICE = "EEEEEEEE-0000-4000-8000-0000000000EE"
EMAIL_TWO = "second-parent@example.com"
OWN_DEVICE_TWO = "FFFFFFFF-0000-4000-8000-0000000000FF"
BABY_THREE = baby_payload("Baby Three", BABY_THREE_ID)
NOW = datetime(2026, 10, 26, 15, 0, 0, tzinfo=UTC)
INTEGRATION = Path(__file__).resolve().parent.parent / "custom_components" / "nighp_baby_tracker"

LAST_DIAPER = "sensor.baby_one_last_diaper"
OTHER_DEVICES_RECORD = "D0000000-0000-4000-8000-000000000001"  # baby one's diaper from device A


# Fixtures: a fake that can post, and a second account on its own server


@pytest.fixture
def server(server: FakeServer) -> FakeServer:
    return add_write_state(server)


@pytest.fixture
def second_server() -> FakeServer:
    server = add_write_state(FakeServer())
    server.names[DEVICE_B] = "Other Phone"
    server.add_record(
        DEVICE_B,
        "Diaper",
        "D0000000-0000-4000-8000-0000000000B1",
        when=at(3),
        baby=BABY_THREE,
        status=2,
    )
    return server


@pytest.fixture(autouse=True)
def fake_client(server: FakeServer, second_server: FakeServer) -> Iterator[FakeServer]:
    def factory(email: str, *args: Any, **kwargs: Any) -> WriteClient:
        return WriteClient(second_server if email == EMAIL_TWO else server, email, *args, **kwargs)

    with (
        patch("custom_components.nighp_baby_tracker.BabyTrackerClient", factory),
        patch("custom_components.nighp_baby_tracker.config_flow.BabyTrackerClient", factory),
    ):
        yield server


@pytest.fixture
async def second_entry(hass: HomeAssistant, setup_entry: MockConfigEntry) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=EMAIL_TWO,
        unique_id=EMAIL_TWO,
        data={CONF_EMAIL: EMAIL_TWO, CONF_PASSWORD: PASSWORD, CONF_DEVICE_UUID: OWN_DEVICE_TWO},
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    return entry


# Helpers


def button(baby: str, kind: str) -> str:
    return f"button.{baby}_{kind}_diaper"


async def press(hass: HomeAssistant, entity_id: str) -> None:
    await hass.services.async_call("button", "press", {"entity_id": entity_id}, blocking=True)


def device_id(hass: HomeAssistant, baby_id: str) -> str:
    device = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, baby_id)})
    assert device is not None
    return device.id


async def log_diaper(hass: HomeAssistant, baby_id: str, kind: str = "wet", **extra: Any) -> None:
    data = {"baby": device_id(hass, baby_id), "type": kind, **extra}
    await hass.services.async_call(DOMAIN, "log_diaper", data, blocking=True)


async def undo(hass: HomeAssistant, **data: Any) -> None:
    await hass.services.async_call(DOMAIN, "undo", data, blocking=True)


def posted(server: FakeServer) -> list[tuple[int, int, Any]]:
    """Everything the fake service received: (number, opcode, decoded record)."""
    return [(n, op, decode_transaction(t)) for n, op, t in server.posted]  # type: ignore[attr-defined]


def snapshot(hass: HomeAssistant) -> dict[str, tuple[str, dict[str, Any]]]:
    return {
        state.entity_id: (state.state, dict(state.attributes))
        for state in hass.states.async_all("sensor")
    }


def reauth_flows(hass: HomeAssistant) -> list[str]:
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    return [flow["context"]["source"] for flow in flows]


# Buttons


async def test_each_baby_gets_wet_dirty_mixed_and_bottle_buttons(
    hass: HomeAssistant, setup_entry: MockConfigEntry
) -> None:
    devices = dr.async_get(hass)
    entities = er.async_get(hass)
    for baby_id in (BABY_ONE_ID, BABY_TWO_ID):
        device = devices.async_get_device(identifiers={(DOMAIN, baby_id)})
        assert device is not None
        buttons = [
            e for e in er.async_entries_for_device(entities, device.id) if e.domain == "button"
        ]
        assert {b.translation_key for b in buttons} == {
            "wet_diaper",
            "dirty_diaper",
            "mixed_diaper",
            "bottle",
        }
        assert len({b.unique_id for b in buttons}) == 4
    assert hass.states.get(button("baby_one", "wet")) is not None
    assert hass.states.get(button("baby_two", "mixed")) is not None


@pytest.mark.parametrize(
    ("kind", "status"), [("wet", 0), ("dirty", 1), ("mixed", 2)], ids=["wet", "dirty", "mixed"]
)
async def test_a_button_press_adds_that_kind_of_diaper_for_that_baby_now(
    hass: HomeAssistant,
    setup_entry: MockConfigEntry,
    server: FakeServer,
    freezer: Any,
    kind: str,
    status: int,
) -> None:
    await hass.config.async_set_time_zone("America/New_York")
    freezer.move_to(NOW)

    await press(hass, button("baby_two", kind))

    [(number, op, record)] = posted(server)
    assert (number, op) == (1, OP_ADD)
    assert isinstance(record, Diaper)
    assert record.status == status
    assert record.baby.object_id == BABY_TWO_ID
    assert record.time == NOW
    assert record.timezone == -14400  # New York in late October
    assert not record.note
    assert server.logs[OWN_DEVICE][0]["SyncID"] == 1


async def test_two_presses_make_two_records_with_different_ids(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer, freezer: Any
) -> None:
    freezer.move_to(NOW)
    await press(hass, button("baby_one", "wet"))
    await press(hass, button("baby_one", "wet"))

    records = [record for _, _, record in posted(server)]
    assert len({record.object_id for record in records}) == 2
    assert [n for n, _, _ in posted(server)] == [1, 2]


# log_diaper


async def test_log_diaper_with_a_past_time_and_a_note(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer
) -> None:
    await log_diaper(
        hass, BABY_ONE_ID, "dirty", time="2026-10-26T10:30:00+00:00", note="Rash cream applied"
    )

    [(_, op, record)] = posted(server)
    assert op == OP_ADD
    assert record.status == 1
    assert record.time == datetime(2026, 10, 26, 10, 30, tzinfo=UTC)
    assert record.note == "Rash cream applied"
    assert record.baby.object_id == BABY_ONE_ID


async def test_log_diaper_reads_a_time_without_an_offset_in_the_home_assistant_time_zone(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer
) -> None:
    await hass.config.async_set_time_zone("America/New_York")

    await log_diaper(hass, BABY_ONE_ID, "wet", time="2026-10-26 10:30:00")

    [(_, _, record)] = posted(server)
    assert record.time == datetime(2026, 10, 26, 14, 30, tzinfo=UTC)
    assert record.timezone == -14400


async def test_log_diaper_without_a_time_uses_now_in_the_home_assistant_time_zone(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer, freezer: Any
) -> None:
    await hass.config.async_set_time_zone("Asia/Kolkata")
    freezer.move_to(NOW)

    await log_diaper(hass, BABY_ONE_ID, "mixed")

    [(_, _, record)] = posted(server)
    assert record.time == NOW
    assert record.timezone == 19800
    assert record.status == 2


async def test_log_diaper_for_an_unknown_baby_device_is_a_validation_error(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer
) -> None:
    with pytest.raises(ServiceValidationError) as raised:
        await hass.services.async_call(
            DOMAIN, "log_diaper", {"baby": "0" * 32, "type": "wet"}, blocking=True
        )
    assert raised.value.translation_domain == DOMAIN
    assert raised.value.translation_key == "baby_not_found"
    assert server.posted == []


async def test_log_diaper_for_another_integrations_device_is_a_validation_error(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer
) -> None:
    other = MockConfigEntry(domain="other_integration")
    other.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=other.entry_id, identifiers={("other_integration", "lamp")}
    )
    with pytest.raises(ServiceValidationError) as raised:
        await hass.services.async_call(
            DOMAIN, "log_diaper", {"baby": device.id, "type": "wet"}, blocking=True
        )
    assert raised.value.translation_key == "baby_not_found"
    assert server.posted == []


async def test_a_log_diaper_retry_with_the_same_object_id_does_not_post_twice(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer
) -> None:
    retry_id = "7E57ED00-0000-4000-8000-000000000001"
    await log_diaper(hass, BABY_ONE_ID, "wet", object_id=retry_id)
    # The caller never saw the answer, so it sends the same logical write again (any case).
    await log_diaper(hass, BABY_ONE_ID, "wet", object_id=retry_id.lower())

    [(_, _, record)] = posted(server)
    assert record.object_id == retry_id
    assert [n for n, _, _ in posted(server)] == [1]


@pytest.mark.parametrize("bad", ["retry-1", "", "1234", "7E57ED00-0000-4000-8000-00000000000Z"])
async def test_log_diaper_rejects_an_object_id_that_is_not_a_uuid(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer, bad: str
) -> None:
    with pytest.raises(vol.Invalid):
        await log_diaper(hass, BABY_ONE_ID, "wet", object_id=bad)
    assert server.posted == []


async def test_log_diaper_accepts_a_lowercase_uuid_and_posts_it_in_uppercase(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer
) -> None:
    await log_diaper(hass, BABY_ONE_ID, "wet", object_id="7e57ed00-0000-4000-8000-00000000000a")

    [(_, _, record)] = posted(server)
    assert record.object_id == "7E57ED00-0000-4000-8000-0000000000" + "0A"


async def test_other_value_errors_are_not_reported_as_an_id_in_use(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer
) -> None:
    with (
        patch.object(SyncEngine, "add_diaper", side_effect=ValueError("some other refusal")),
        pytest.raises(ValueError, match="some other refusal"),
    ):
        await log_diaper(hass, BABY_ONE_ID, "wet")


async def test_a_plain_value_error_that_mentions_belongs_to_is_not_reported_as_an_id_in_use(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer
) -> None:
    lookalike = "object_id 7E57ED00-0000-4000-8000-00000000000A belongs to another record"
    with (
        patch.object(SyncEngine, "add_diaper", side_effect=ValueError(lookalike)),
        pytest.raises(ValueError, match="belongs to another record"),
    ):
        await log_diaper(hass, BABY_ONE_ID, "wet", object_id="7e57ed00-0000-4000-8000-00000000000a")


async def test_a_log_diaper_object_id_belonging_to_another_record_is_an_error(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer
) -> None:
    with pytest.raises(ServiceValidationError) as raised:
        await log_diaper(hass, BABY_ONE_ID, "wet", object_id=OTHER_DEVICES_RECORD)
    assert raised.value.translation_key == "object_id_in_use"
    assert server.posted == []


async def test_log_diaper_rejects_an_unknown_type(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer
) -> None:
    with pytest.raises(vol.Invalid):
        await log_diaper(hass, BABY_ONE_ID, "purple")
    assert server.posted == []


async def test_log_diaper_for_a_baby_whose_entry_is_not_loaded_is_a_validation_error(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer
) -> None:
    baby_device = device_id(hass, BABY_ONE_ID)
    await hass.config_entries.async_unload(setup_entry.entry_id)
    with pytest.raises(ServiceValidationError) as raised:
        await hass.services.async_call(
            DOMAIN, "log_diaper", {"baby": baby_device, "type": "wet"}, blocking=True
        )
    assert raised.value.translation_key == "entry_not_loaded"


async def test_the_actions_exist_even_before_any_entry_is_set_up(hass: HomeAssistant) -> None:
    from homeassistant.setup import async_setup_component

    assert await async_setup_component(hass, DOMAIN, {})
    assert hass.services.has_service(DOMAIN, "log_diaper")
    assert hass.services.has_service(DOMAIN, "undo")


# Writes sync first and update the sensor at once


async def test_a_successful_write_updates_the_sensor_without_waiting_for_the_next_sync(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer
) -> None:
    assert hass.states.get(LAST_DIAPER).state == at(0).isoformat()

    await log_diaper(hass, BABY_ONE_ID, "dirty", time="2026-10-26T09:15:00+00:00", note="Big one")
    # No clock tick and no refresh: the new record is already on the sensor.
    state = hass.states.get(LAST_DIAPER)
    assert state.state == "2026-10-26T09:15:00+00:00"
    assert state.attributes["type"] == "dirty"
    assert state.attributes["note"] == "Big one"


async def test_a_write_syncs_first_so_it_sees_records_other_devices_just_added(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer
) -> None:
    # A phone logs a diaper; HA hasn't polled yet. The write's own sync picks it up.
    server.add_record(
        DEVICE_A, "Diaper", "D0000000-0000-4000-8000-000000000003", when=at(30), status=1
    )
    server.calls.clear()  # type: ignore[attr-defined]

    await log_diaper(hass, BABY_ONE_ID, "wet", time="2026-10-24T00:00:00+00:00")

    assert server.calls[0] == "devices"  # type: ignore[attr-defined]
    assert server.transaction_reads  # the phone's new entry was read
    assert hass.states.get(LAST_DIAPER).state == at(30).isoformat()  # the newest by time


async def test_a_write_before_the_first_sync_completes_syncs_first_and_then_writes(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer
) -> None:
    # An engine that has never synced knows no babies; the write must sync before it posts.
    setup_entry.runtime_data.engine = SyncEngine(OWN_DEVICE)

    await press(hass, button("baby_one", "wet"))

    [(number, op, record)] = posted(server)
    assert (number, op) == (1, OP_ADD)
    assert record.baby.object_id == BABY_ONE_ID


async def test_a_failed_sync_before_a_write_posts_nothing(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer
) -> None:
    server.request_error = BabyTrackerConnectionError("down")
    before = snapshot(hass)

    with pytest.raises(HomeAssistantError) as raised:
        await press(hass, button("baby_one", "wet"))

    assert raised.value.translation_key == "sync_failed"
    assert server.posted == []
    assert snapshot(hass) == before


# Failed writes


async def test_a_failed_write_raises_a_translated_error_and_changes_no_sensor(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer
) -> None:
    before = snapshot(hass)
    server.post_errors.append((BabyTrackerConnectionError("HTTP 500"), False))  # type: ignore[attr-defined]

    with pytest.raises(HomeAssistantError) as raised:
        await press(hass, button("baby_one", "wet"))

    assert type(raised.value) is HomeAssistantError
    assert raised.value.translation_domain == DOMAIN
    assert raised.value.translation_key == "write_failed"
    assert snapshot(hass) == before


async def test_a_failed_log_diaper_action_raises_the_same_error(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer
) -> None:
    server.post_errors.append((BabyTrackerConnectionError("HTTP 500"), False))  # type: ignore[attr-defined]
    with pytest.raises(HomeAssistantError) as raised:
        await log_diaper(hass, BABY_ONE_ID, "wet")
    assert raised.value.translation_key == "write_failed"


async def test_a_press_that_got_no_answer_but_landed_succeeds(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer
) -> None:
    server.post_errors.append((NoResponseError("timed out"), True))  # type: ignore[attr-defined]

    await press(hass, button("baby_one", "dirty"))

    assert len(posted(server)) == 1
    assert hass.states.get(LAST_DIAPER).attributes["type"] == "dirty"


async def test_a_write_right_after_an_unanswered_one_says_it_is_still_being_confirmed(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer, freezer: Any
) -> None:
    freezer.move_to(NOW)
    server.post_errors.append((NoResponseError("timed out"), False))  # type: ignore[attr-defined]
    with pytest.raises(HomeAssistantError) as first:
        await press(hass, button("baby_one", "wet"))
    assert first.value.translation_key == "write_failed"

    with pytest.raises(HomeAssistantError) as second:
        await press(hass, button("baby_one", "wet"))

    assert type(second.value) is HomeAssistantError
    assert second.value.translation_domain == DOMAIN
    assert second.value.translation_key == "write_outcome_unknown"
    assert server.posted == []  # nothing was resent


async def test_the_unknown_outcome_message_asks_the_user_to_try_again_in_a_minute() -> None:
    strings = json.loads((INTEGRATION / "strings.json").read_text())
    message = strings["exceptions"]["write_outcome_unknown"]["message"]
    assert "still being confirmed" in message
    assert "try again in a minute" in message.lower()


async def test_the_unknown_outcome_clears_once_the_grace_window_passes(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer, freezer: Any
) -> None:
    freezer.move_to(NOW)
    server.post_errors.append((NoResponseError("timed out"), False))  # type: ignore[attr-defined]
    with pytest.raises(HomeAssistantError):
        await press(hass, button("baby_one", "wet"))

    freezer.move_to(datetime(2026, 10, 26, 15, 5, 0, tzinfo=UTC))
    await press(hass, button("baby_one", "wet"))

    assert [n for n, _, _ in posted(server)] == [1]  # the free number was reused


async def test_a_failed_write_still_saves_the_engine_state(
    hass: HomeAssistant,
    setup_entry: MockConfigEntry,
    server: FakeServer,
    hass_storage: dict[str, Any],
    freezer: Any,
) -> None:
    freezer.move_to(NOW)
    server.post_errors.append((NoResponseError("timed out"), False))  # type: ignore[attr-defined]
    with pytest.raises(HomeAssistantError):
        await press(hass, button("baby_one", "wet"))

    saved = hass_storage[f"{DOMAIN}.{setup_entry.entry_id}"]["data"]
    assert saved["pending"]["sync_id"] == 1
    assert saved["pending"]["unknown_since"] is not None


async def test_the_pending_post_and_its_grace_window_survive_a_restart(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer, freezer: Any
) -> None:
    freezer.move_to(NOW)
    server.post_errors.append((NoResponseError("timed out"), False))  # type: ignore[attr-defined]
    with pytest.raises(HomeAssistantError):
        await press(hass, button("baby_one", "wet"))

    assert await hass.config_entries.async_reload(setup_entry.entry_id)
    await hass.async_block_till_done()
    assert setup_entry.state is ConfigEntryState.LOADED
    with pytest.raises(HomeAssistantError) as raised:
        await press(hass, button("baby_one", "wet"))

    assert raised.value.translation_key == "write_outcome_unknown"


async def test_a_successful_write_saves_the_new_record(
    hass: HomeAssistant,
    setup_entry: MockConfigEntry,
    server: FakeServer,
    hass_storage: dict[str, Any],
) -> None:
    await press(hass, button("baby_one", "wet"))

    saved = hass_storage[f"{DOMAIN}.{setup_entry.entry_id}"]["data"]
    [(_, _, record)] = posted(server)
    assert saved["created"] == [record.object_id]
    assert saved["last_posted"] == 1
    assert saved["pending"] is None


async def test_a_gap_in_this_devices_own_log_creates_a_repair_issue_and_raises(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer
) -> None:
    # The service says this device's log is at 5, but nothing can be read back.
    server.last_sync_override[OWN_DEVICE] = 5  # type: ignore[attr-defined]

    with pytest.raises(HomeAssistantError) as raised:
        await press(hass, button("baby_one", "wet"))

    assert raised.value.translation_key == "own_log_gap"
    assert server.posted == []
    issue = ir.async_get(hass).async_get_issue(DOMAIN, f"own_log_gap_{setup_entry.entry_id}")
    assert issue is not None
    assert issue.translation_key == "own_log_gap"
    assert issue.severity is ir.IssueSeverity.ERROR
    assert issue.is_fixable is False
    assert issue.translation_placeholders == {"account": EMAIL}


async def test_the_repair_issue_goes_away_after_a_write_succeeds(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer
) -> None:
    server.last_sync_override[OWN_DEVICE] = 5  # type: ignore[attr-defined]
    with pytest.raises(HomeAssistantError):
        await press(hass, button("baby_one", "wet"))
    server.last_sync_override.clear()  # type: ignore[attr-defined]

    await press(hass, button("baby_one", "wet"))

    assert ir.async_get(hass).async_get_issue(DOMAIN, f"own_log_gap_{setup_entry.entry_id}") is None


async def test_the_repair_issue_is_cleared_when_the_entry_is_removed(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer
) -> None:
    server.last_sync_override[OWN_DEVICE] = 5  # type: ignore[attr-defined]
    with pytest.raises(HomeAssistantError):
        await press(hass, button("baby_one", "wet"))
    issue_id = f"own_log_gap_{setup_entry.entry_id}"
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None

    await hass.config_entries.async_remove(setup_entry.entry_id)

    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None


def test_the_repair_text_says_it_may_be_temporary_and_names_the_cost() -> None:
    issue = json.loads((INTEGRATION / "strings.json").read_text())["issues"]["own_log_gap"]
    text = issue["description"]
    assert "more than an hour" in text
    assert "history" in text
    assert "new device" in text


def test_writes_use_the_coordinators_public_write_method_not_its_private_members() -> None:
    source = (INTEGRATION / "writes.py").read_text()
    assert "SLF001" not in source
    assert "_async_save" not in source
    assert "_snapshot" not in source
    assert "coordinator.lock" not in source


async def test_the_coordinators_write_method_locks_syncs_saves_and_publishes(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer
) -> None:
    coordinator = setup_entry.runtime_data
    seen: dict[str, Any] = {}

    async def operation(engine: SyncEngine, client: Any) -> str:
        seen["locked"] = coordinator.lock.locked()
        seen["engine"] = engine is coordinator.engine
        seen["client"] = client is coordinator.client
        return "result"

    updates: list[object] = []
    coordinator.async_add_listener(lambda: updates.append(1))
    with patch.object(Store, "async_save", autospec=True) as save:
        result = await coordinator.async_write(operation)

    assert result == "result"
    assert seen == {"locked": True, "engine": True, "client": True}
    assert server.calls[0] == "devices"  # type: ignore[attr-defined]  # it synced first
    assert save.call_count == 1
    assert updates


async def test_a_gap_in_the_own_log_still_saves_state(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer
) -> None:
    server.last_sync_override[OWN_DEVICE] = 5  # type: ignore[attr-defined]
    with (
        patch.object(Store, "async_save", autospec=True) as save,
        pytest.raises(HomeAssistantError),
    ):
        await press(hass, button("baby_one", "wet"))
    assert save.call_count == 1


@pytest.mark.parametrize("where", ["post", "sync"])
async def test_an_auth_error_during_a_write_starts_reauth(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer, where: str
) -> None:
    if where == "post":
        server.post_errors.append((AuthenticationError("session rejected"), False))  # type: ignore[attr-defined]
    else:
        server.request_error = AuthenticationError("session rejected")

    with pytest.raises(HomeAssistantError) as raised:
        await press(hass, button("baby_one", "wet"))

    assert raised.value.translation_key == "auth_failed"
    assert reauth_flows(hass) == ["reauth"]
    assert server.posted == []


# undo


async def test_undo_deletes_the_record_a_button_press_created(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer, freezer: Any
) -> None:
    freezer.move_to(NOW)
    await press(hass, button("baby_one", "dirty"))
    [(_, _, created)] = posted(server)
    assert hass.states.get(LAST_DIAPER).state == NOW.isoformat()

    await undo(hass, baby=device_id(hass, BABY_ONE_ID))

    deleted = posted(server)[1]
    assert deleted[1] == OP_DELETE
    assert deleted[0] == 2
    assert deleted[2].object_id == created.object_id
    # The sensor falls back to the phone's earlier diaper, again without waiting for a sync.
    assert hass.states.get(LAST_DIAPER).state == at(0).isoformat()


async def test_undo_by_config_entry_id(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer
) -> None:
    await press(hass, button("baby_one", "wet"))

    await undo(hass, config_entry_id=setup_entry.entry_id)

    assert [op for _, op, _ in posted(server)] == [OP_ADD, OP_DELETE]


async def test_undo_without_an_object_id_removes_the_most_recent_record_ha_created(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer, freezer: Any
) -> None:
    freezer.move_to(NOW)
    await press(hass, button("baby_one", "wet"))
    freezer.move_to(datetime(2026, 10, 26, 15, 0, 30, tzinfo=UTC))
    await press(hass, button("baby_one", "dirty"))
    newest = posted(server)[1][2].object_id

    await undo(hass, baby=device_id(hass, BABY_ONE_ID))

    assert posted(server)[2][2].object_id == newest


async def test_undo_with_a_baby_only_considers_that_babys_records(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer, freezer: Any
) -> None:
    freezer.move_to(NOW)
    await press(hass, button("baby_one", "wet"))
    first = posted(server)[0][2].object_id
    freezer.move_to(datetime(2026, 10, 26, 15, 0, 30, tzinfo=UTC))
    await press(hass, button("baby_two", "wet"))

    await undo(hass, baby=device_id(hass, BABY_ONE_ID))
    assert posted(server)[2][2].object_id == first

    await undo(hass, config_entry_id=setup_entry.entry_id)  # any baby: baby two's is left
    assert posted(server)[3][2].baby.object_id == BABY_TWO_ID


async def test_undo_removes_the_last_write_even_when_all_presses_share_a_second(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer, freezer: Any
) -> None:
    freezer.move_to(NOW)  # the clock never moves: every record has the same timestamp
    for _ in range(6):
        await press(hass, button("baby_one", "wet"))
    ids = [record.object_id for _, _, record in posted(server)]

    await undo(hass, baby=device_id(hass, BABY_ONE_ID))
    await undo(hass, config_entry_id=setup_entry.entry_id)

    deleted = [record.object_id for _, op, record in posted(server) if op == OP_DELETE]
    assert deleted == [ids[5], ids[4]]


@pytest.mark.parametrize("scope", ["baby", "account"])
async def test_undo_deletes_the_newest_ha_write_even_after_a_phone_edited_it(
    hass: HomeAssistant,
    setup_entry: MockConfigEntry,
    server: FakeServer,
    freezer: Any,
    scope: str,
) -> None:
    freezer.move_to(NOW)
    await press(hass, button("baby_one", "wet"))  # A
    freezer.move_to(datetime(2026, 10, 26, 15, 5, 0, tzinfo=UTC))
    await press(hass, button("baby_one", "wet"))  # B
    first, second = (record.object_id for _, _, record in posted(server))
    # A phone edits B (wet to dirty), so B's current version came from the phone.
    server.add_record(
        DEVICE_A,
        "Diaper",
        second,
        when=datetime(2026, 10, 26, 15, 5, 0, tzinfo=UTC),
        changed=datetime(2026, 10, 26, 15, 6, 0, tzinfo=UTC),
        op=OP_UPDATE,
        status=1,
    )

    target = (
        {"baby": device_id(hass, BABY_ONE_ID)}
        if scope == "baby"
        else {"config_entry_id": setup_entry.entry_id}
    )
    await undo(hass, **target)

    deleted = [record.object_id for _, op, record in posted(server) if op == OP_DELETE]
    assert deleted == [second]
    assert first != second


async def test_undo_follows_write_order_not_the_time_of_the_diaper(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer, freezer: Any
) -> None:
    freezer.move_to(NOW)
    await press(hass, button("baby_one", "wet"))
    # Written last, but its diaper happened days ago.
    await log_diaper(hass, BABY_ONE_ID, "dirty", time="2026-10-22T08:00:00+00:00")
    written_last = posted(server)[1][2].object_id

    await undo(hass, baby=device_id(hass, BABY_ONE_ID))

    assert posted(server)[2][2].object_id == written_last


async def test_a_wet_press_bumps_todays_count_with_no_poll(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer, freezer: Any
) -> None:
    await hass.config.async_set_time_zone("America/New_York")
    freezer.move_to(NOW)
    assert hass.states.get("sensor.baby_one_diapers_today").state == "0"

    await press(hass, button("baby_one", "wet"))
    state = hass.states.get("sensor.baby_one_diapers_today")
    assert state.state == "1"
    assert (state.attributes["wet"], state.attributes["dirty"]) == (1, 0)

    await press(hass, button("baby_one", "dirty"))
    state = hass.states.get("sensor.baby_one_diapers_today")
    assert state.state == "2"
    assert (state.attributes["wet"], state.attributes["dirty"]) == (1, 1)
    assert hass.states.get("sensor.baby_two_diapers_today").state == "0"

    await undo(hass, baby=device_id(hass, BABY_ONE_ID))
    assert hass.states.get("sensor.baby_one_diapers_today").state == "1"


async def test_undo_accepts_an_object_id_in_any_case(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer
) -> None:
    await press(hass, button("baby_one", "wet"))
    created = posted(server)[0][2].object_id

    await undo(hass, baby=device_id(hass, BABY_ONE_ID), object_id=created.lower())

    assert posted(server)[1][1] == OP_DELETE
    assert posted(server)[1][2].object_id == created


async def test_undo_with_a_baby_and_another_babys_record_id_is_a_validation_error(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer
) -> None:
    await press(hass, button("baby_two", "wet"))
    babys_two = posted(server)[0][2].object_id

    with pytest.raises(ServiceValidationError) as raised:
        await undo(hass, baby=device_id(hass, BABY_ONE_ID), object_id=babys_two)

    assert raised.value.translation_key == "record_not_for_baby"
    assert len(posted(server)) == 1  # nothing deleted


async def test_a_baby_device_with_an_unloaded_and_a_loaded_entry_uses_the_loaded_one(
    hass: HomeAssistant,
    setup_entry: MockConfigEntry,
    second_entry: MockConfigEntry,
    server: FakeServer,
    second_server: FakeServer,
) -> None:
    baby_device = device_id(hass, BABY_ONE_ID)
    dr.async_get(hass).async_update_device(baby_device, add_config_entry_id=second_entry.entry_id)
    await hass.config_entries.async_unload(setup_entry.entry_id)

    # Routed to the loaded second entry, which doesn't know this baby: not "not loaded".
    with pytest.raises(ServiceValidationError) as raised:
        await hass.services.async_call(
            DOMAIN, "log_diaper", {"baby": baby_device, "type": "wet"}, blocking=True
        )
    assert raised.value.translation_key == "baby_not_found"


async def test_undo_with_an_object_id_removes_that_record(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer, freezer: Any
) -> None:
    freezer.move_to(NOW)
    await press(hass, button("baby_one", "wet"))
    first = posted(server)[0][2].object_id
    freezer.move_to(datetime(2026, 10, 26, 15, 0, 30, tzinfo=UTC))
    await press(hass, button("baby_one", "dirty"))

    await undo(hass, baby=device_id(hass, BABY_ONE_ID), object_id=first)

    assert posted(server)[2][2].object_id == first


async def test_undo_with_nothing_to_undo_is_a_validation_error(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer
) -> None:
    with pytest.raises(ServiceValidationError) as raised:
        await undo(hass, baby=device_id(hass, BABY_ONE_ID))

    assert raised.value.translation_domain == DOMAIN
    assert raised.value.translation_key == "nothing_to_undo"
    assert server.posted == []


async def test_undo_of_a_record_another_device_created_is_refused(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer
) -> None:
    before = snapshot(hass)

    with pytest.raises(ServiceValidationError) as raised:
        await undo(hass, baby=device_id(hass, BABY_ONE_ID), object_id=OTHER_DEVICES_RECORD)

    assert raised.value.translation_key == "not_own_record"
    assert server.posted == []
    assert snapshot(hass) == before


async def test_undo_never_picks_another_devices_record_when_ha_created_none(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer
) -> None:
    # The phone's diaper is the newest record, but it isn't HA's to delete.
    with pytest.raises(ServiceValidationError) as raised:
        await undo(hass, config_entry_id=setup_entry.entry_id)
    assert raised.value.translation_key == "nothing_to_undo"


async def test_undo_twice_deletes_once_and_then_refuses(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer
) -> None:
    await press(hass, button("baby_one", "wet"))
    created = posted(server)[0][2].object_id
    await undo(hass, baby=device_id(hass, BABY_ONE_ID))

    with pytest.raises(ServiceValidationError) as no_id:
        await undo(hass, baby=device_id(hass, BABY_ONE_ID))
    with pytest.raises(ServiceValidationError) as by_id:
        await undo(hass, baby=device_id(hass, BABY_ONE_ID), object_id=created)

    assert no_id.value.translation_key == "nothing_to_undo"
    assert by_id.value.translation_key == "record_not_found"
    assert [op for _, op, _ in posted(server)] == [OP_ADD, OP_DELETE]


async def test_undo_needs_a_baby_or_a_config_entry(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer
) -> None:
    with pytest.raises(vol.Invalid):
        await undo(hass)


async def test_undo_with_an_unknown_config_entry_is_a_validation_error(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer
) -> None:
    with pytest.raises(ServiceValidationError) as raised:
        await undo(hass, config_entry_id="no-such-entry")
    assert raised.value.translation_key == "entry_not_found"


async def test_a_failed_undo_raises_a_translated_error_and_keeps_the_record(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer
) -> None:
    await press(hass, button("baby_one", "wet"))
    before = snapshot(hass)
    server.post_errors.append((BabyTrackerConnectionError("HTTP 500"), False))  # type: ignore[attr-defined]

    with pytest.raises(HomeAssistantError) as raised:
        await undo(hass, baby=device_id(hass, BABY_ONE_ID))

    assert raised.value.translation_key == "write_failed"
    assert snapshot(hass) == before


# Concurrency and several accounts


async def test_concurrent_presses_are_serialized(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer
) -> None:
    server.calls.clear()  # type: ignore[attr-defined]

    await asyncio.gather(
        press(hass, button("baby_one", "wet")),
        press(hass, button("baby_one", "dirty")),
        press(hass, button("baby_two", "mixed")),
    )

    assert [n for n, _, _ in posted(server)] == [1, 2, 3]
    assert server.max_active_posts == 1  # type: ignore[attr-defined]
    # Each write finishes (sync, prepare, post) before the next starts.
    assert server.calls == ["devices", "devices", "post"] * 3  # type: ignore[attr-defined]


async def test_both_accounts_work_with_two_config_entries(
    hass: HomeAssistant,
    setup_entry: MockConfigEntry,
    second_entry: MockConfigEntry,
    server: FakeServer,
    second_server: FakeServer,
) -> None:
    assert hass.services.has_service(DOMAIN, "log_diaper")
    assert hass.services.has_service(DOMAIN, "undo")

    await log_diaper(hass, BABY_ONE_ID, "wet", note="first account")
    await log_diaper(hass, BABY_THREE_ID, "mixed", note="second account")

    [(_, _, one)] = posted(server)
    [(_, _, three)] = posted(second_server)
    assert (one.baby.object_id, one.note) == (BABY_ONE_ID, "first account")
    assert (three.baby.object_id, three.note) == (BABY_THREE_ID, "second account")
    assert OWN_DEVICE in server.logs and OWN_DEVICE_TWO not in server.logs
    assert OWN_DEVICE_TWO in second_server.logs and OWN_DEVICE not in second_server.logs


async def test_undo_with_two_config_entries_only_touches_the_named_one(
    hass: HomeAssistant,
    setup_entry: MockConfigEntry,
    second_entry: MockConfigEntry,
    server: FakeServer,
    second_server: FakeServer,
) -> None:
    await log_diaper(hass, BABY_ONE_ID, "wet")
    await log_diaper(hass, BABY_THREE_ID, "wet")

    await undo(hass, config_entry_id=second_entry.entry_id)

    assert [op for _, op, _ in posted(server)] == [OP_ADD]
    assert [op for _, op, _ in posted(second_server)] == [OP_ADD, OP_DELETE]
    await undo(hass, baby=device_id(hass, BABY_ONE_ID))
    assert [op for _, op, _ in posted(server)] == [OP_ADD, OP_DELETE]


async def test_buttons_for_each_account_post_to_their_own_account(
    hass: HomeAssistant,
    setup_entry: MockConfigEntry,
    second_entry: MockConfigEntry,
    server: FakeServer,
    second_server: FakeServer,
) -> None:
    await press(hass, button("baby_three", "dirty"))
    assert server.posted == []  # type: ignore[attr-defined]
    assert len(second_server.posted) == 1  # type: ignore[attr-defined]


# Metadata


def test_services_yaml_describes_the_diaper_and_undo_actions_and_their_fields() -> None:
    import yaml

    services = yaml.safe_load((INTEGRATION / "services.yaml").read_text())
    assert set(services) == {
        "log_diaper",
        "undo",
        "log_bottle",
        "log_nursing",
        "log_sleep",
        "log_pump",
    }
    assert set(services["log_diaper"]["fields"]) == {"baby", "type", "time", "note", "object_id"}
    assert set(services["undo"]["fields"]) == {"config_entry_id", "baby", "object_id"}
    baby = services["log_diaper"]["fields"]["baby"]
    assert baby["required"] is True
    assert baby["selector"]["device"]["filter"] == [{"integration": DOMAIN}]
    assert services["log_diaper"]["fields"]["type"]["selector"]["select"]["options"] == [
        "wet",
        "dirty",
        "mixed",
    ]


def test_every_action_field_has_a_translated_name_and_description() -> None:
    import yaml

    services = yaml.safe_load((INTEGRATION / "services.yaml").read_text())
    strings = json.loads((INTEGRATION / "strings.json").read_text())["services"]
    assert set(strings) == set(services)
    for action, spec in services.items():
        assert strings[action]["name"] and strings[action]["description"]
        assert set(strings[action]["fields"]) == set(spec["fields"])
        for field in strings[action]["fields"].values():
            assert field["name"] and field["description"]


def test_every_error_and_issue_the_integration_raises_has_a_translation() -> None:
    strings = json.loads((INTEGRATION / "strings.json").read_text())
    assert set(strings["exceptions"]) >= {
        "baby_not_found",
        "entry_not_found",
        "entry_not_loaded",
        "nothing_to_undo",
        "record_not_for_baby",
        "not_own_record",
        "record_not_found",
        "write_outcome_unknown",
        "own_log_gap",
        "auth_failed",
        "sync_failed",
        "write_failed",
    }
    assert all(item["message"] for item in strings["exceptions"].values())
    assert strings["issues"]["own_log_gap"]["title"]
    assert "{account}" in strings["issues"]["own_log_gap"]["description"]
    assert {"wet_diaper", "dirty_diaper", "mixed_diaper"} <= set(strings["entity"]["button"])


def test_the_action_setup_rule_is_done() -> None:
    import yaml

    scale = yaml.safe_load((INTEGRATION / "quality_scale.yaml").read_text())["rules"]
    assert scale["action-setup"] == "done"
    assert scale["docs-actions"] == "done"
