"""Core PR 13: the undo action."""

from datetime import UTC, datetime, timedelta
from typing import Any

import probatio
import pytest
import voluptuous as vol
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.nighp_baby_tracker.const import (
    DOMAIN,
)
from custom_components.nighp_baby_tracker.pybabytracker import (
    OP_ADD,
    OP_DELETE,
    OP_UPDATE,
    BabyTrackerConnectionError,
)

from .factories import (
    BABY_ONE_ID,
    BABY_THREE_ID,
    BABY_TWO_ID,
    DEVICE_A,
    at,
)
from .fake_server import FakeServer
from .helpers import (
    LAST_DIAPER,
    LAST_FEED,
    NOW,
    OTHER_DEVICES_RECORD,
    button,
    call,
    device_id,
    log_diaper,
    log_pump,
    only_record,
    posted,
    press,
    sensor_states,
    state_of,
    undo,
)


@pytest.fixture(autouse=True)
def _start_at_now(freezer: Any) -> None:
    """Freeze the clock at NOW before anything is set up.

    Timers scheduled at setup then share the clock the tests move, so moving it forward and
    ticking reaches them the way it does in core's tests.
    """
    freezer.move_to(NOW)


RETRY_ID = "7E57ED00-0000-4000-8000-000000000001"


VALID: dict[str, tuple[dict[str, Any], str, dict[str, Any]]] = {
    "log_bottle": (
        {"kind": "formula", "amount": 4, "unit": "fl. oz.", "time": "2026-10-26T16:54:35+00:00"},
        "C1 formula add",
        {},
    ),
    "log_bottle_pumped": (
        {"kind": "pumped", "amount": 3, "unit": "fl. oz.", "time": "2026-10-26T16:54:55+00:00"},
        "C2 pumped bottle add",
        {},
    ),
    "log_pump": (
        {
            "left_amount": 2,
            "right_amount": 5,
            "unit": "fl. oz.",
            "left_minutes": 1,
            "right_minutes": 1,
            "finish_side": "left",
            "time": "2026-10-26T16:55:15+00:00",
        },
        "C3 pump add",
        {},
    ),
    "log_nursing": (
        {
            "left_minutes": 1,
            "right_minutes": 1,
            "finish_side": "right",
            "end_time": "2026-10-26T16:57:52+00:00",
        },
        "C4 nursing add",
        {},  # the end time minus its two minutes is the capture's start
    ),
    "log_sleep": (
        {"start": "2026-10-26T16:57:39+00:00", "end": "2026-10-26T16:58:48+00:00"},
        "C5 sleep add",
        {"duration": 1},
    ),
}


ACTION_OF = {"log_bottle_pumped": "log_bottle"}


ACTIONS = list(VALID)


async def run_valid(hass: HomeAssistant, name: str, **extra: Any) -> None:
    data, _, _ = VALID[name]
    await call(hass, ACTION_OF.get(name, name), **{**data, **extra})


def _issues(hass: HomeAssistant) -> list[ir.IssueEntry]:
    registry = ir.async_get(hass)
    return [issue for (domain, _), issue in registry.issues.items() if domain == DOMAIN]


async def _call(hass: HomeAssistant, action: str, data: dict[str, Any]) -> None:
    await hass.services.async_call(DOMAIN, action, data, blocking=True)


async def _two_diapers(hass: HomeAssistant, freezer: Any, server: Any) -> tuple[str, str]:
    """Write baby one's diaper, then baby two's. Returns their record IDs, oldest first."""
    freezer.move_to(NOW)
    await press(hass, "button.baby_one_wet_diaper")
    freezer.tick(timedelta(minutes=1))
    await press(hass, "button.baby_two_wet_diaper")
    first, second = (record.object_id for _, _, record in posted(server))
    return first, second


def _deleted(server: Any) -> list[str]:
    from custom_components.nighp_baby_tracker.pybabytracker import OP_DELETE

    return [record.object_id for _, op, record in posted(server) if op == OP_DELETE]


async def test_undo_removes_a_pump_by_account(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    await log_pump(hass, config_entry_id=init_integration.entry_id, left_amount=3, unit="fl. oz.")

    await undo(hass, config_entry_id=init_integration.entry_id)

    assert [op for _, op, _ in posted(server)] == [0, 2]
    assert state_of(hass, init_integration).state == "unknown"


async def test_undo_for_a_baby_never_picks_a_pump(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    await log_pump(hass, config_entry_id=init_integration.entry_id, left_amount=3, unit="fl. oz.")
    pump_id = posted(server)[0][2].object_id

    with pytest.raises(ServiceValidationError) as raised:
        await undo(hass, device_id=device_id(hass, BABY_ONE_ID))
    assert raised.value.translation_key == "nothing_to_undo"
    with pytest.raises(ServiceValidationError) as raised:
        await undo(hass, device_id=device_id(hass, BABY_ONE_ID), object_id=pump_id)
    assert raised.value.translation_key == "record_not_for_baby"
    assert len(posted(server)) == 1


async def test_undo_deletes_the_record_a_button_press_created(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, freezer: Any
) -> None:
    freezer.move_to(NOW)
    await press(hass, button("baby_one", "dirty"))
    [(_, _, created)] = posted(server)
    assert hass.states.get(LAST_DIAPER).state == NOW.isoformat()

    await undo(hass, device_id=device_id(hass, BABY_ONE_ID))

    deleted = posted(server)[1]
    assert deleted[1] == OP_DELETE
    assert deleted[0] == 2
    assert deleted[2].object_id == created.object_id
    # The sensor falls back to the phone's earlier diaper, again without waiting for a sync.
    assert hass.states.get(LAST_DIAPER).state == at(0).isoformat()


async def test_undo_by_config_entry_id(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    await press(hass, button("baby_one", "wet"))

    await undo(hass, config_entry_id=init_integration.entry_id)

    assert [op for _, op, _ in posted(server)] == [OP_ADD, OP_DELETE]


async def test_undo_without_an_object_id_removes_the_most_recent_record_ha_created(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, freezer: Any
) -> None:
    freezer.move_to(NOW)
    await press(hass, button("baby_one", "wet"))
    freezer.move_to(datetime(2026, 10, 26, 15, 0, 30, tzinfo=UTC))
    await press(hass, button("baby_one", "dirty"))
    newest = posted(server)[1][2].object_id

    await undo(hass, device_id=device_id(hass, BABY_ONE_ID))

    assert posted(server)[2][2].object_id == newest


async def test_undo_with_a_baby_only_considers_that_babys_records(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, freezer: Any
) -> None:
    freezer.move_to(NOW)
    await press(hass, button("baby_one", "wet"))
    first = posted(server)[0][2].object_id
    freezer.move_to(datetime(2026, 10, 26, 15, 0, 30, tzinfo=UTC))
    await press(hass, button("baby_two", "wet"))

    await undo(hass, device_id=device_id(hass, BABY_ONE_ID))
    assert posted(server)[2][2].object_id == first

    await undo(hass, config_entry_id=init_integration.entry_id)  # any baby: baby two's is left
    assert posted(server)[3][2].baby.object_id == BABY_TWO_ID


async def test_undo_removes_the_last_write_even_when_all_presses_share_a_second(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, freezer: Any
) -> None:
    freezer.move_to(NOW)  # the clock never moves: every record has the same timestamp
    for _ in range(6):
        await press(hass, button("baby_one", "wet"))
    ids = [record.object_id for _, _, record in posted(server)]

    await undo(hass, device_id=device_id(hass, BABY_ONE_ID))
    await undo(hass, config_entry_id=init_integration.entry_id)

    deleted = [record.object_id for _, op, record in posted(server) if op == OP_DELETE]
    assert deleted == [ids[5], ids[4]]


@pytest.mark.parametrize("scope", ["baby", "account"])
async def test_undo_deletes_the_newest_ha_write_even_after_a_phone_edited_it(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
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
        {"device_id": device_id(hass, BABY_ONE_ID)}
        if scope == "baby"
        else {"config_entry_id": init_integration.entry_id}
    )
    await undo(hass, **target)

    deleted = [record.object_id for _, op, record in posted(server) if op == OP_DELETE]
    assert deleted == [second]
    assert first != second


async def test_undo_follows_write_order_not_the_time_of_the_diaper(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, freezer: Any
) -> None:
    freezer.move_to(NOW)
    await press(hass, button("baby_one", "wet"))
    # Written last, but its diaper happened days ago.
    await log_diaper(hass, BABY_ONE_ID, "dirty", time="2026-10-22T08:00:00+00:00")
    written_last = posted(server)[1][2].object_id

    await undo(hass, device_id=device_id(hass, BABY_ONE_ID))

    assert posted(server)[2][2].object_id == written_last


async def test_undo_accepts_an_object_id_in_any_case(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    await press(hass, button("baby_one", "wet"))
    created = posted(server)[0][2].object_id

    await undo(hass, device_id=device_id(hass, BABY_ONE_ID), object_id=created.lower())

    assert posted(server)[1][1] == OP_DELETE
    assert posted(server)[1][2].object_id == created


async def test_undo_with_a_baby_and_another_babys_record_id_is_a_validation_error(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    await press(hass, button("baby_two", "wet"))
    babys_two = posted(server)[0][2].object_id

    with pytest.raises(ServiceValidationError) as raised:
        await undo(hass, device_id=device_id(hass, BABY_ONE_ID), object_id=babys_two)

    assert raised.value.translation_key == "record_not_for_baby"
    assert len(posted(server)) == 1  # nothing deleted


async def test_undo_with_an_object_id_removes_that_record(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, freezer: Any
) -> None:
    freezer.move_to(NOW)
    await press(hass, button("baby_one", "wet"))
    first = posted(server)[0][2].object_id
    freezer.move_to(datetime(2026, 10, 26, 15, 0, 30, tzinfo=UTC))
    await press(hass, button("baby_one", "dirty"))

    await undo(hass, device_id=device_id(hass, BABY_ONE_ID), object_id=first)

    assert posted(server)[2][2].object_id == first


async def test_undo_with_nothing_to_undo_is_a_validation_error(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    with pytest.raises(ServiceValidationError) as raised:
        await undo(hass, device_id=device_id(hass, BABY_ONE_ID))

    assert raised.value.translation_domain == DOMAIN
    assert raised.value.translation_key == "nothing_to_undo"
    assert server.posted == []


async def test_undo_of_a_record_another_device_created_is_refused(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    before = sensor_states(hass)

    with pytest.raises(ServiceValidationError) as raised:
        await undo(hass, device_id=device_id(hass, BABY_ONE_ID), object_id=OTHER_DEVICES_RECORD)

    assert raised.value.translation_key == "not_own_record"
    assert server.posted == []
    assert sensor_states(hass) == before


async def test_undo_never_picks_another_devices_record_when_ha_created_none(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    # The phone's diaper is the newest record, but it isn't HA's to delete.
    with pytest.raises(ServiceValidationError) as raised:
        await undo(hass, config_entry_id=init_integration.entry_id)
    assert raised.value.translation_key == "nothing_to_undo"


async def test_undo_twice_deletes_once_and_then_refuses(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    await press(hass, button("baby_one", "wet"))
    created = posted(server)[0][2].object_id
    await undo(hass, device_id=device_id(hass, BABY_ONE_ID))

    with pytest.raises(ServiceValidationError) as no_id:
        await undo(hass, device_id=device_id(hass, BABY_ONE_ID))
    with pytest.raises(ServiceValidationError) as by_id:
        await undo(hass, device_id=device_id(hass, BABY_ONE_ID), object_id=created)

    assert no_id.value.translation_key == "nothing_to_undo"
    assert by_id.value.translation_key == "record_not_found"
    assert [op for _, op, _ in posted(server)] == [OP_ADD, OP_DELETE]


async def test_undo_needs_a_baby_or_a_config_entry(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    with pytest.raises(probatio.Invalid):
        await undo(hass)


async def test_undo_with_an_unknown_config_entry_is_a_validation_error(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    with pytest.raises(ServiceValidationError) as raised:
        await undo(hass, config_entry_id="no-such-entry")
    assert raised.value.translation_key == "service_config_entry_not_found"


async def test_a_failed_undo_raises_a_translated_error_and_keeps_the_record(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    await press(hass, button("baby_one", "wet"))
    before = sensor_states(hass)
    server.post_errors.append((BabyTrackerConnectionError("HTTP 500"), False))

    with pytest.raises(HomeAssistantError) as raised:
        await undo(hass, device_id=device_id(hass, BABY_ONE_ID))

    assert raised.value.translation_key == "write_failed"
    assert sensor_states(hass) == before


async def test_undo_with_two_config_entries_only_touches_the_named_one(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    second_entry: MockConfigEntry,
    server: FakeServer,
    second_server: FakeServer,
) -> None:
    await log_diaper(hass, BABY_ONE_ID, "wet")
    await log_diaper(hass, BABY_THREE_ID, "wet")

    await undo(hass, config_entry_id=second_entry.entry_id)

    assert [op for _, op, _ in posted(server)] == [OP_ADD]
    assert [op for _, op, _ in posted(second_server)] == [OP_ADD, OP_DELETE]
    await undo(hass, device_id=device_id(hass, BABY_ONE_ID))
    assert [op for _, op, _ in posted(server)] == [OP_ADD, OP_DELETE]


async def test_undo_removes_a_bottle_and_the_sensor_goes_back(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    await call(
        hass,
        "log_bottle",
        kind="formula",
        amount=4,
        unit="fl. oz.",
        time="2026-10-26T09:15:00+00:00",
    )
    assert hass.states.get(LAST_FEED).state == "2026-10-26T09:15:00+00:00"

    await undo(hass, device_id=device_id(hass, BABY_ONE_ID))

    assert hass.states.get(LAST_FEED).state == "unknown"
    [(_, op_add, added), (number, op_delete, deleted)] = posted(server)
    assert (op_add, op_delete, number) == (0, 2, 2)
    assert deleted.object_id == added.object_id


@pytest.mark.parametrize("name", ACTIONS)
async def test_undo_removes_a_record_of_every_new_type(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, name: str
) -> None:
    await run_valid(hass, name)
    added = only_record(server)

    await undo(hass, config_entry_id=init_integration.entry_id)

    [_, (_, op, deleted)] = posted(server)
    assert op == 2
    assert deleted.object_id == added.object_id
    assert deleted.record_type == added.record_type


async def test_undo_by_record_id_removes_a_bottle_among_other_writes(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    await run_valid(hass, "log_bottle", object_id=RETRY_ID)
    await run_valid(hass, "log_sleep")

    await undo(hass, device_id=device_id(hass, BABY_ONE_ID), object_id=RETRY_ID)

    [_, _, (_, op, deleted)] = posted(server)
    assert (op, deleted.object_id) == (2, RETRY_ID)


async def test_undo_with_device_id_removes_that_babys_latest_write(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: Any, freezer: Any
) -> None:
    first, _ = await _two_diapers(hass, freezer, server)

    await _call(hass, "undo", {"device_id": device_id(hass, BABY_ONE_ID)})

    assert _deleted(server) == [first]
    assert _issues(hass) == []


async def test_undo_with_config_entry_id_removes_the_accounts_latest_write(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: Any, freezer: Any
) -> None:
    _, second = await _two_diapers(hass, freezer, server)

    await _call(hass, "undo", {"config_entry_id": init_integration.entry_id})

    assert _deleted(server) == [second]
    assert _issues(hass) == []


async def test_undo_with_no_target_is_refused(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    with pytest.raises(vol.Invalid, match="at least one"):
        await _call(hass, "undo", {})


@pytest.mark.parametrize("action", ["log_pump", "undo"])
async def test_an_unknown_account_is_a_validation_error(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: Any, action: str
) -> None:
    data = {
        "config_entry_id": "no-such-entry",
        **({"left_amount": 3} if action == "log_pump" else {}),
    }
    with pytest.raises(ServiceValidationError) as raised:
        await _call(hass, action, data)
    assert raised.value.translation_domain == "homeassistant"
    assert raised.value.translation_key == "service_config_entry_not_found"
    assert server.posted == []


@pytest.mark.parametrize("action", ["log_pump", "undo"])
async def test_an_account_that_is_not_loaded_is_a_validation_error(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: Any, action: str
) -> None:
    await hass.config_entries.async_unload(init_integration.entry_id)
    data = {
        "config_entry_id": init_integration.entry_id,
        **({"left_amount": 3} if action == "log_pump" else {}),
    }
    with pytest.raises(ServiceValidationError) as raised:
        await _call(hass, action, data)
    assert raised.value.translation_domain == "homeassistant"
    assert raised.value.translation_key == "service_config_entry_not_loaded"


@pytest.mark.parametrize("action", ["log_pump", "undo"])
async def test_another_integrations_entry_is_a_validation_error(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: Any, action: str
) -> None:
    other = MockConfigEntry(domain="other_integration")
    other.add_to_hass(hass)
    data = {
        "config_entry_id": other.entry_id,
        **({"left_amount": 3} if action == "log_pump" else {}),
    }

    with pytest.raises(ServiceValidationError) as raised:
        await _call(hass, action, data)

    assert raised.value.translation_key == "service_config_entry_wrong_domain"
    assert server.posted == []
