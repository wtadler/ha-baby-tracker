# ruff: noqa: F811
"""The actions' target fields follow the actions guide, and the old ones still work.

Baby actions take ``device_id``. Account actions take ``config_entry_id``. ``baby`` (and
``device`` on ``log_pump``) keep working for one release and raise a repair issue that names
the field to switch to.
"""

import json
from datetime import timedelta
from typing import Any

import pytest
import voluptuous as vol
import yaml
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, is_callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.nighp_baby_tracker.const import DOMAIN

from .factories import BABY_ONE_ID, BABY_THREE_ID
from .helpers import INTEGRATION, NOW, account_device, device_id, posted, press

# Every baby action, the data it needs besides the baby, and whether a timer must already run.
BABY_ACTIONS: dict[str, tuple[dict[str, Any], str | None]] = {
    "log_diaper": ({"type": "wet"}, None),
    "log_bottle": ({"kind": "formula", "amount": 4}, None),
    "log_nursing": ({"left_minutes": 5}, None),
    "log_sleep": ({"start": "2026-10-26T13:00:00+00:00", "end": "2026-10-26T14:00:00+00:00"}, None),
    "start_sleep": ({}, None),
    "stop_sleep": ({}, "button.baby_one_sleep"),
    "start_nursing": ({"side": "left"}, None),
    "switch_nursing_side": ({}, "button.baby_one_nurse_left"),
    "stop_nursing": ({}, "button.baby_one_nurse_left"),
}
ACTION_IDS = list(BABY_ACTIONS)
ALL_ACTIONS = [*BABY_ACTIONS, "log_pump", "undo"]


def _issues(hass: HomeAssistant) -> list[ir.IssueEntry]:
    registry = ir.async_get(hass)
    return [issue for (domain, _), issue in registry.issues.items() if domain == DOMAIN]


def _effect(hass: HomeAssistant, server: Any) -> tuple[Any, ...]:
    """What an action visibly changes: records sent, and the baby's nursing timer."""
    timer = hass.states.get("sensor.baby_one_nursing_timer")
    assert timer is not None
    return (len(server.posted), timer.state, timer.attributes.get("side"))


async def _prepare(hass: HomeAssistant, action: str, freezer: Any) -> None:
    freezer.move_to(NOW)
    prepare = BABY_ACTIONS[action][1]
    if prepare is not None:
        await press(hass, prepare)
        freezer.tick(timedelta(minutes=2))


async def _call(hass: HomeAssistant, action: str, data: dict[str, Any]) -> None:
    await hass.services.async_call(DOMAIN, action, data, blocking=True)


# The baby actions


@pytest.mark.parametrize("action", ACTION_IDS)
async def test_a_baby_action_works_with_device_id_and_raises_no_repair(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: Any, freezer: Any, action: str
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
    hass: HomeAssistant, init_integration: MockConfigEntry, server: Any, freezer: Any, action: str
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
    hass: HomeAssistant, init_integration: MockConfigEntry, server: Any, action: str
) -> None:
    baby = device_id(hass, BABY_ONE_ID)
    with pytest.raises(vol.Invalid, match="at most one"):
        await _call(hass, action, {"device_id": baby, "baby": baby, **BABY_ACTIONS[action][0]})
    assert server.posted == []
    assert _issues(hass) == []


@pytest.mark.parametrize("action", ACTION_IDS)
async def test_a_baby_action_with_no_target_is_refused(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: Any, action: str
) -> None:
    with pytest.raises(vol.Invalid, match="at least one"):
        await _call(hass, action, dict(BABY_ACTIONS[action][0]))
    assert server.posted == []


@pytest.mark.parametrize("field", ["device_id", "baby"])
async def test_an_unknown_baby_device_is_a_validation_error(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: Any, field: str
) -> None:
    with pytest.raises(ServiceValidationError) as raised:
        await _call(hass, "log_diaper", {field: "0" * 32, "type": "wet"})
    assert raised.value.translation_key == "baby_not_found"
    assert server.posted == []


@pytest.mark.parametrize("field", ["device_id", "baby"])
async def test_the_account_device_is_not_a_baby(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: Any, field: str
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
    hass: HomeAssistant, init_integration: MockConfigEntry, server: Any
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
    server: Any,
    second_server: Any,
) -> None:
    baby_three = device_id(hass, BABY_THREE_ID)

    await _call(hass, "log_diaper", {"device_id": baby_three, "type": "wet"})

    assert server.posted == []
    assert len(second_server.posted) == 1


# log_pump takes an account


async def test_log_pump_takes_an_account_with_config_entry_id(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: Any
) -> None:
    await _call(hass, "log_pump", {"config_entry_id": init_integration.entry_id, "left_amount": 3})

    assert len(server.posted) == 1
    assert _issues(hass) == []


@pytest.mark.parametrize("alias", ["device", "baby"])
async def test_log_pump_still_takes_a_device_and_raises_a_repair(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: Any, alias: str
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
    hass: HomeAssistant, init_integration: MockConfigEntry, server: Any, alias: str
) -> None:
    await _call(hass, "log_pump", {alias: device_id(hass, BABY_ONE_ID), "left_amount": 3})

    assert len(server.posted) == 1


@pytest.mark.parametrize(
    "fields",
    [("config_entry_id", "device"), ("config_entry_id", "baby"), ("device", "baby")],
)
async def test_log_pump_with_two_targets_is_refused(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: Any, fields: tuple[str, str]
) -> None:
    values = {
        "config_entry_id": init_integration.entry_id,
        "device": account_device(hass, init_integration).id,
        "baby": device_id(hass, BABY_ONE_ID),
    }
    with pytest.raises(vol.Invalid, match="at most one"):
        await _call(
            hass, "log_pump", {**{field: values[field] for field in fields}, "left_amount": 3}
        )
    assert server.posted == []


async def test_log_pump_with_no_target_is_refused(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: Any
) -> None:
    with pytest.raises(vol.Invalid, match="at least one"):
        await _call(hass, "log_pump", {"left_amount": 3})
    assert server.posted == []


# undo: a baby by device_id, the account by config_entry_id


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


async def test_undo_still_takes_baby_and_raises_a_repair(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: Any, freezer: Any
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


async def test_undo_with_no_target_is_refused(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    with pytest.raises(vol.Invalid, match="at least one"):
        await _call(hass, "undo", {})


# Config entries are looked up with Home Assistant's own helper


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


# Setup


def test_the_actions_are_set_up_by_a_callback_named_for_core() -> None:
    from custom_components.nighp_baby_tracker import services

    assert is_callback(services.async_setup_services)
    assert not hasattr(services, "async_register_services")


async def test_setting_up_the_actions_registers_every_one_of_them(hass: HomeAssistant) -> None:
    from custom_components.nighp_baby_tracker.services import async_setup_services

    async_setup_services(hass)

    assert set(hass.services.async_services_for_domain(DOMAIN)) == set(ALL_ACTIONS)


# services.yaml and strings


def _yaml() -> dict[str, Any]:
    return yaml.safe_load((INTEGRATION / "services.yaml").read_text())


def _strings() -> dict[str, Any]:
    return json.loads((INTEGRATION / "strings.json").read_text())


@pytest.mark.parametrize("action", ACTION_IDS)
def test_every_baby_action_has_a_required_device_id_field(action: str) -> None:
    fields = _yaml()[action]["fields"]

    assert "baby" not in fields
    assert fields["device_id"]["required"] is True
    assert fields["device_id"]["selector"] == {"device": {"filter": [{"integration": DOMAIN}]}}


def test_log_pump_has_a_required_config_entry_id_and_no_device_field() -> None:
    fields = _yaml()["log_pump"]["fields"]

    assert {"device", "baby", "device_id"}.isdisjoint(fields)
    assert fields["config_entry_id"]["required"] is True
    assert fields["config_entry_id"]["selector"] == {"config_entry": {"integration": DOMAIN}}


def test_undo_offers_a_device_for_a_baby_and_an_account_for_all() -> None:
    fields = _yaml()["undo"]["fields"]

    assert set(fields) == {"config_entry_id", "device_id", "object_id"}
    assert fields["device_id"]["selector"] == {"device": {"filter": [{"integration": DOMAIN}]}}
    assert fields["config_entry_id"]["selector"] == {"config_entry": {"integration": DOMAIN}}


@pytest.mark.parametrize("action", ALL_ACTIONS)
def test_every_yaml_field_has_a_name_and_description_and_no_alias_is_left(action: str) -> None:
    strings = _strings()["services"][action]["fields"]

    assert set(strings) == set(_yaml()[action]["fields"])
    assert all(field["name"] and field["description"] for field in strings.values())


def test_the_deprecation_repair_is_translated() -> None:
    issue = _strings()["issues"]["deprecated_action_field"]

    assert issue["title"]
    for placeholder in ("{action}", "{field}", "{replacement}"):
        assert placeholder in issue["description"]
