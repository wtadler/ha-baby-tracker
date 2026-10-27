"""HACS only: the deprecated ``baby`` and ``device`` fields on the actions, and their repairs.
The tests that also cover ``device_id`` stay in core with the actions they serve, so the cut
keeps those cases and drops the rest."""

from datetime import timedelta
from typing import Any

import pytest
import voluptuous as vol
from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.nighp_baby_tracker.const import (
    DOMAIN,
)
from custom_components.nighp_baby_tracker.pybabytracker import (
    Pump,
)

from .factories import (
    BABY_ONE_ID,
    BABY_THREE_ID,
)
from .fake_server import FakeServer
from .helpers import (
    ACTION_IDS,
    BABY_ACTIONS,
    NOW,
    account_device,
    device_id,
    log_pump,
    posted,
    press,
    turn_on,
)


def only_pump(server: FakeServer) -> Pump:
    [(_, _, record)] = posted(server)
    assert isinstance(record, Pump)
    return record


@pytest.fixture(autouse=True)
def _start_at_now(freezer: FrozenDateTimeFactory) -> None:
    """Freeze the clock at NOW before anything is set up.

    Timers scheduled at setup then share the clock the tests move, so moving it forward and
    ticking reaches them the way it does in core's tests.
    """
    freezer.move_to(NOW)


def _issues(hass: HomeAssistant) -> list[ir.IssueEntry]:
    registry = ir.async_get(hass)
    return [issue for (domain, _), issue in registry.issues.items() if domain == DOMAIN]


def _effect(hass: HomeAssistant, server: FakeServer) -> tuple[Any, ...]:
    """What an action visibly changes: records sent, and the baby's nursing timer."""
    timer = hass.states.get("sensor.baby_one_nursing_timer")
    assert timer is not None
    return (len(server.posted), timer.state, timer.attributes.get("side"))


async def _prepare(hass: HomeAssistant, action: str, freezer: FrozenDateTimeFactory) -> None:
    freezer.move_to(NOW)
    prepare = BABY_ACTIONS[action][1]
    if prepare is not None:
        await turn_on(hass, prepare)
        freezer.tick(timedelta(minutes=2))


async def _call(hass: HomeAssistant, action: str, data: dict[str, Any]) -> None:
    await hass.services.async_call(DOMAIN, action, data, blocking=True)


async def _two_diapers(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, server: FakeServer
) -> tuple[str, str]:
    """Write baby one's diaper, then baby two's. Returns their record IDs, oldest first."""
    freezer.move_to(NOW)
    await press(hass, "button.baby_one_wet_diaper")
    freezer.tick(timedelta(minutes=1))
    await press(hass, "button.baby_two_wet_diaper")
    first, second = (record.object_id for _, _, record in posted(server))
    return first, second


def _deleted(server: FakeServer) -> list[str]:
    from custom_components.nighp_baby_tracker.pybabytracker import OP_DELETE

    return [record.object_id for _, op, record in posted(server) if op == OP_DELETE]


async def test_log_pump_still_accepts_baby_as_an_alias_of_device(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    await log_pump(hass, baby=device_id(hass, BABY_ONE_ID), left_amount=3, unit="fl. oz.")

    assert "baby" not in only_pump(server).to_payload()


async def test_the_baby_alias_resolves_a_second_accounts_baby_to_that_account(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    second_entry: MockConfigEntry,
    server: FakeServer,
    second_server: FakeServer,
) -> None:
    await log_pump(hass, baby=device_id(hass, BABY_THREE_ID), left_amount=3, unit="fl. oz.")

    assert server.posted == []
    only_pump(second_server)


async def test_the_baby_alias_for_an_unknown_device_is_a_validation_error(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    with pytest.raises(ServiceValidationError) as raised:
        await log_pump(hass, baby="nope", left_amount=3)
    assert raised.value.translation_key == "baby_not_found"


@pytest.mark.parametrize("other", ["device", "config_entry_id"])
async def test_baby_with_device_or_an_account_is_refused_as_two_targets(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, other: str
) -> None:
    value = (
        account_device(hass, init_integration).id
        if other == "device"
        else init_integration.entry_id
    )
    with pytest.raises(vol.Invalid, match="at most one"):
        await log_pump(hass, baby=device_id(hass, BABY_ONE_ID), left_amount=3, **{other: value})
    assert server.posted == []


@pytest.mark.parametrize("action", ACTION_IDS)
async def test_a_baby_action_works_with_device_id_and_raises_no_repair(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    server: FakeServer,
    freezer: FrozenDateTimeFactory,
    action: str,
) -> None:
    await _prepare(hass, action, freezer)
    before = _effect(hass, server)

    await _call(
        hass, action, {"device_id": device_id(hass, BABY_ONE_ID), **BABY_ACTIONS[action][0]}
    )

    assert _effect(hass, server) != before
    assert _issues(hass) == []


@pytest.mark.parametrize("action", ACTION_IDS)
async def test_the_deprecated_baby_field_still_works_and_raises_a_repair(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    server: FakeServer,
    freezer: FrozenDateTimeFactory,
    action: str,
) -> None:
    await _prepare(hass, action, freezer)
    before = _effect(hass, server)

    await _call(hass, action, {"baby": device_id(hass, BABY_ONE_ID), **BABY_ACTIONS[action][0]})

    assert _effect(hass, server) != before
    issue = ir.async_get(hass).async_get_issue(DOMAIN, f"deprecated_{action}_baby")
    assert issue is not None
    assert issue.translation_key == "deprecated_action_field"
    assert issue.translation_placeholders == {
        "action": action,
        "field": "baby",
        "replacement": "device_id",
    }
    assert issue.severity is ir.IssueSeverity.WARNING
    assert issue.is_fixable is False


async def test_using_the_alias_again_keeps_a_single_repair(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    for _ in range(3):
        await _call(hass, "log_diaper", {"baby": device_id(hass, BABY_ONE_ID), "type": "wet"})

    assert [issue.issue_id for issue in _issues(hass)] == ["deprecated_log_diaper_baby"]


async def test_each_action_gets_its_own_repair(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    baby = device_id(hass, BABY_ONE_ID)
    await _call(hass, "log_diaper", {"baby": baby, "type": "wet"})
    await _call(hass, "log_bottle", {"baby": baby, "kind": "formula", "amount": 4})

    assert {issue.issue_id for issue in _issues(hass)} == {
        "deprecated_log_diaper_baby",
        "deprecated_log_bottle_baby",
    }


@pytest.mark.parametrize("action", ACTION_IDS)
async def test_a_baby_action_with_both_fields_is_refused(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, action: str
) -> None:
    baby = device_id(hass, BABY_ONE_ID)
    with pytest.raises(vol.Invalid, match="at most one"):
        await _call(hass, action, {"device_id": baby, "baby": baby, **BABY_ACTIONS[action][0]})
    assert server.posted == []
    assert _issues(hass) == []


@pytest.mark.parametrize("action", ACTION_IDS)
async def test_a_baby_action_with_no_target_is_refused(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, action: str
) -> None:
    with pytest.raises(vol.Invalid, match="at least one"):
        await _call(hass, action, dict(BABY_ACTIONS[action][0]))
    assert server.posted == []


@pytest.mark.parametrize("field", ["device_id", "baby"])
async def test_an_unknown_baby_device_is_a_validation_error(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, field: str
) -> None:
    with pytest.raises(ServiceValidationError) as raised:
        await _call(hass, "log_diaper", {field: "0" * 32, "type": "wet"})
    assert raised.value.translation_key == "baby_not_found"
    assert server.posted == []


@pytest.mark.parametrize("field", ["device_id", "baby"])
async def test_the_account_device_is_not_a_baby(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, field: str
) -> None:
    with pytest.raises(ServiceValidationError) as raised:
        await _call(
            hass,
            "log_diaper",
            {field: account_device(hass, init_integration).id, "type": "wet"},
        )
    assert raised.value.translation_key == "baby_not_found"
    assert server.posted == []


async def test_a_baby_of_an_unloaded_account_says_the_account_is_not_loaded(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    baby = device_id(hass, BABY_ONE_ID)
    await hass.config_entries.async_unload(init_integration.entry_id)
    assert init_integration.state is ConfigEntryState.NOT_LOADED

    with pytest.raises(ServiceValidationError) as raised:
        await _call(hass, "log_diaper", {"device_id": baby, "type": "wet"})
    assert raised.value.translation_domain == "homeassistant"
    assert raised.value.translation_key == "service_config_entry_not_loaded"


async def test_the_device_id_of_a_second_account_writes_to_that_account(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    second_entry: MockConfigEntry,
    server: FakeServer,
    second_server: FakeServer,
) -> None:
    baby_three = device_id(hass, BABY_THREE_ID)

    await _call(hass, "log_diaper", {"device_id": baby_three, "type": "wet"})

    assert server.posted == []
    assert len(second_server.posted) == 1


@pytest.mark.parametrize("alias", ["device", "baby"])
async def test_log_pump_still_takes_a_device_and_raises_a_repair(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, alias: str
) -> None:
    await _call(
        hass, "log_pump", {alias: account_device(hass, init_integration).id, "left_amount": 3}
    )

    assert len(server.posted) == 1
    issue = ir.async_get(hass).async_get_issue(DOMAIN, f"deprecated_log_pump_{alias}")
    assert issue is not None
    assert issue.translation_key == "deprecated_action_field"
    assert issue.translation_placeholders == {
        "action": "log_pump",
        "field": alias,
        "replacement": "config_entry_id",
    }
    assert issue.severity is ir.IssueSeverity.WARNING
    assert issue.is_fixable is False


@pytest.mark.parametrize("alias", ["device", "baby"])
async def test_log_pump_with_a_babys_device_still_reaches_the_account(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, alias: str
) -> None:
    await _call(hass, "log_pump", {alias: device_id(hass, BABY_ONE_ID), "left_amount": 3})

    assert len(server.posted) == 1


async def test_undo_still_takes_baby_and_raises_a_repair(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    server: FakeServer,
    freezer: FrozenDateTimeFactory,
) -> None:
    first, _ = await _two_diapers(hass, freezer, server)

    await _call(hass, "undo", {"baby": device_id(hass, BABY_ONE_ID)})

    assert _deleted(server) == [first]
    issue = ir.async_get(hass).async_get_issue(DOMAIN, "deprecated_undo_baby")
    assert issue is not None
    assert issue.translation_placeholders == {
        "action": "undo",
        "field": "baby",
        "replacement": "device_id",
    }
    assert issue.severity is ir.IssueSeverity.WARNING
