"""Core PR 12: log_pump, an account-level action. Its total-only form is in
test_log_pump_total.py."""

from typing import Any

import probatio
import pytest
import voluptuous as vol
from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import issue_registry as ir
from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.nighp_baby_tracker.const import (
    DOMAIN,
)
from custom_components.nighp_baby_tracker.pybabytracker import (
    Pump,
    Volume,
)

from . import NOW
from .factories import (
    BABY_ONE_ID,
    BABY_THREE_ID,
    BABY_TWO_ID,
)
from .fake_server import FakeServer
from .helpers import (
    BOTTLE_TODAY,
    FEEDS_TODAY,
    account_device,
    call,
    device_id,
    log_pump,
    only_record,
    posted,
    state_of,
)

# The live service's Pump keys: shape only.
PER_SIDE_PUMP_KEYS = {
    "BCObjectType",
    "amount",
    "finishSide",
    "leftDuration",
    "leftAmount",
    "newFlage",
    "note",
    "objectID",
    "pictureLoaded",
    "pictureNote",
    "rightAmount",
    "rightDuration",
    "sides",
    "time",
    "timestamp",
    "timezone",
}


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


def key_of(raised: pytest.ExceptionInfo[ServiceValidationError]) -> str | None:
    return raised.value.translation_key


def _issues(hass: HomeAssistant) -> list[ir.IssueEntry]:
    registry = ir.async_get(hass)
    return [issue for (domain, _), issue in registry.issues.items() if domain == DOMAIN]


async def _call(hass: HomeAssistant, action: str, data: dict[str, Any]) -> None:
    await hass.services.async_call(DOMAIN, action, data, blocking=True)


async def test_logging_a_pump_for_one_entry_leaves_the_other_untouched(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    second_entry: MockConfigEntry,
    server: FakeServer,
    second_server: FakeServer,
) -> None:
    await log_pump(
        hass,
        config_entry_id=second_entry.entry_id,
        left_amount=2,
        unit="fl. oz.",
        time="2026-10-26T08:00:00+00:00",
    )

    assert server.posted == []
    only_pump(second_server)
    assert state_of(hass, second_entry).state == "2026-10-26T08:00:00+00:00"
    assert state_of(hass, init_integration).state == "unknown"


async def test_log_pump_by_config_entry_id_writes_a_pump_with_no_baby_and_the_real_keys(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    await log_pump(
        hass,
        config_entry_id=init_integration.entry_id,
        left_amount=2,
        right_amount=5,
        unit="fl. oz.",
        left_minutes=1,
        right_minutes=1,
        finish_side="left",
    )

    record = only_pump(server)
    payload = record.to_payload()
    assert "baby" not in payload
    assert set(payload) == PER_SIDE_PUMP_KEYS
    assert record.volume == Volume(7, "oz")
    assert record.baby is None


async def test_log_pump_accepts_the_account_device(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    await log_pump(
        hass, device=account_device(hass, init_integration).id, left_amount=3, unit="fl. oz."
    )

    assert "baby" not in only_pump(server).to_payload()


async def test_log_pump_accepts_a_baby_device_and_resolves_it_to_its_account(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    await log_pump(hass, device=device_id(hass, BABY_TWO_ID), left_amount=3, unit="fl. oz.")

    record = only_pump(server)
    assert "baby" not in record.to_payload()  # the baby that was named is not recorded
    assert state_of(hass, init_integration).state != "unknown"


async def test_log_pump_through_a_second_accounts_baby_device_posts_to_that_account(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    second_entry: MockConfigEntry,
    server: FakeServer,
    second_server: FakeServer,
) -> None:
    await log_pump(hass, device=device_id(hass, BABY_THREE_ID), left_amount=3, unit="fl. oz.")

    assert server.posted == []
    only_pump(second_server)


async def test_log_pump_through_a_second_accounts_device_posts_to_that_account(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    second_entry: MockConfigEntry,
    server: FakeServer,
    second_server: FakeServer,
) -> None:
    await log_pump(
        hass, device=account_device(hass, second_entry).id, left_amount=3, unit="fl. oz."
    )

    assert server.posted == []
    only_pump(second_server)


async def test_log_pump_needs_an_account_or_a_device(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    with pytest.raises(vol.Invalid, match="config_entry_id"):
        await log_pump(hass, left_amount=3)
    assert server.posted == []


async def test_log_pump_takes_one_of_config_entry_id_and_device_not_both(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    with pytest.raises(vol.Invalid, match="at most one"):
        await log_pump(
            hass,
            config_entry_id=init_integration.entry_id,
            device=account_device(hass, init_integration).id,
            left_amount=3,
        )
    assert server.posted == []


async def test_log_pump_for_an_unknown_account_is_a_validation_error(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    with pytest.raises(ServiceValidationError) as raised:
        await log_pump(hass, config_entry_id="no-such-entry", left_amount=3)
    assert raised.value.translation_key == "service_config_entry_not_found"
    assert server.posted == []


async def test_log_pump_for_an_account_that_is_not_loaded_is_a_validation_error(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    device = account_device(hass, init_integration)
    await hass.config_entries.async_unload(init_integration.entry_id)
    assert init_integration.state is ConfigEntryState.NOT_LOADED

    with pytest.raises(ServiceValidationError) as raised:
        await log_pump(hass, device=device.id, left_amount=3)
    assert raised.value.translation_key == "service_config_entry_not_loaded"
    with pytest.raises(ServiceValidationError) as raised:
        await log_pump(hass, config_entry_id=init_integration.entry_id, left_amount=3)
    assert raised.value.translation_key == "service_config_entry_not_loaded"


@pytest.mark.parametrize("device", ["nope", "00000000000000000000000000000000"])
async def test_log_pump_for_a_device_that_is_not_ours_is_a_validation_error(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, device: str
) -> None:
    with pytest.raises(ServiceValidationError) as raised:
        await log_pump(hass, device=device, left_amount=3)
    assert raised.value.translation_key == "baby_not_found"
    assert server.posted == []


async def test_log_pump_for_another_integrations_device_is_a_validation_error(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    other = MockConfigEntry(domain="other_integration")
    other.add_to_hass(hass)
    foreign = dr.async_get(hass).async_get_or_create(
        config_entry_id=other.entry_id, identifiers={("other_integration", "thing")}
    )

    with pytest.raises(ServiceValidationError) as raised:
        await log_pump(hass, device=foreign.id, left_amount=3)
    assert raised.value.translation_key == "baby_not_found"
    assert server.posted == []


async def test_log_pump_still_requires_an_amount_and_a_unit_it_knows(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    with pytest.raises(ServiceValidationError) as raised:
        await log_pump(hass, config_entry_id=init_integration.entry_id, left_minutes=5)
    assert raised.value.translation_key == "no_amount"
    with pytest.raises(probatio.MultipleInvalid):
        await log_pump(hass, config_entry_id=init_integration.entry_id, left_amount=60, unit="cups")
    assert server.posted == []


async def test_log_pump_totals_the_two_sides_and_decodes_back(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    await call(
        hass,
        "log_pump",
        left_amount=6,
        right_amount=7.5,
        unit="fl. oz.",
        left_minutes=10,
        right_minutes=12,
    )

    record = only_record(server)
    assert isinstance(record, Pump)
    assert record.volume == Volume(13.5, "oz")
    assert record.left_volume == Volume(6, "oz")
    assert record.right_volume == Volume(7.5, "oz")
    assert (record.left_duration, record.right_duration) == (10, 12)


async def test_log_pump_with_one_side_only_writes_zero_for_the_other(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    await call(hass, "log_pump", left_amount=3, unit="fl. oz.", left_minutes=15)

    record = only_record(server)
    assert record.right_volume == Volume(0, "oz")
    assert record.volume == Volume(3, "oz")


async def test_log_pump_without_a_unit_uses_the_unit_systems_bottle_unit(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    hass.config.units = US_CUSTOMARY_SYSTEM

    await call(hass, "log_pump", left_amount=3, right_amount=2)

    assert only_record(server).volume == Volume(5, "oz")


async def test_log_pump_with_no_amount_is_a_validation_error(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    with pytest.raises(ServiceValidationError) as raised:
        await call(hass, "log_pump", left_minutes=10)
    assert key_of(raised) == "no_amount"
    with pytest.raises(ServiceValidationError) as raised:
        await call(hass, "log_pump", left_amount=0, right_amount=0)
    assert key_of(raised) == "invalid_amount"
    assert server.posted == []


async def test_log_pump_with_a_negative_side_is_a_validation_error(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    with pytest.raises(ServiceValidationError) as raised:
        await call(hass, "log_pump", left_amount=-2, right_amount=5)
    assert key_of(raised) == "invalid_amount"
    assert server.posted == []


async def test_a_pump_and_a_sleep_are_not_feeds_today(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    server: FakeServer,
    freezer: FrozenDateTimeFactory,
) -> None:
    await hass.config.async_set_time_zone("America/New_York")
    freezer.move_to(NOW)

    await call(hass, "log_pump", left_amount=3, unit="fl. oz.", time="2026-10-26T14:00:00+00:00")
    await call(
        hass, "log_sleep", start="2026-10-26T12:00:00+00:00", end="2026-10-26T13:00:00+00:00"
    )

    assert hass.states.get(FEEDS_TODAY).state == "0"
    assert float(hass.states.get(BOTTLE_TODAY).state) == 0


async def test_log_pump_takes_an_account_with_config_entry_id(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    await _call(hass, "log_pump", {"config_entry_id": init_integration.entry_id, "left_amount": 3})

    assert len(server.posted) == 1
    assert _issues(hass) == []


@pytest.mark.parametrize(
    "fields",
    [("config_entry_id", "device"), ("config_entry_id", "baby"), ("device", "baby")],
)
async def test_log_pump_with_two_targets_is_refused(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    server: FakeServer,
    fields: tuple[str, str],
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
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    with pytest.raises(vol.Invalid, match="at least one"):
        await _call(hass, "log_pump", {"left_amount": 3})
    assert server.posted == []
