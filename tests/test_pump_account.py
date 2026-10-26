# ruff: noqa: F811
"""Pumps belong to the account, not a baby.

The live service's pumps carry no ``baby`` key, so **last pump** is one sensor on an account-level
device per config entry, ``log_pump`` names an account (not a baby), and the old per-baby
last-pump entities are removed from the entity registry on setup.
"""

from __future__ import annotations

from typing import Any

import pytest
import voluptuous as vol
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.nighp_baby_tracker.const import DOMAIN
from custom_components.nighp_baby_tracker.pybabytracker import Pump, Volume

from .conftest import EMAIL, FakeServer
from .factories import (
    BABY_ONE_ID,
    BABY_THREE_ID,
    BABY_TWO_ID,
    DEVICE_A,
    DEVICE_B,
    at,
    baby_payload,
    volume,
)
from .helpers import tick
from .test_writes import (  # noqa: F401  (fixtures: a fake that can post, and a second account)
    EMAIL_TWO,
    device_id,
    fake_client,
    posted,
    second_entry,
    second_server,
    server,
    undo,
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


def _id(number: int) -> str:
    return f"B0000000-0000-4000-8000-{number:012d}"


def pump_entity(hass: HomeAssistant, entry: MockConfigEntry) -> str:
    """The entity ID of an entry's account-level last-pump sensor."""
    entity_id = er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, f"{entry.entry_id}_last_pump"
    )
    assert entity_id is not None, "the account's last-pump sensor doesn't exist"
    return entity_id


def account_device(hass: HomeAssistant, entry: MockConfigEntry) -> dr.DeviceEntry:
    entity = er.async_get(hass).async_get(pump_entity(hass, entry))
    assert entity is not None and entity.device_id is not None
    device = dr.async_get(hass).async_get(entity.device_id)
    assert device is not None
    return device


async def log_pump(hass: HomeAssistant, **data: Any) -> None:
    await hass.services.async_call(DOMAIN, "log_pump", data, blocking=True)


def only_pump(server: FakeServer) -> Pump:
    [(_, _, record)] = posted(server)
    assert isinstance(record, Pump)
    return record


# --- the account-level device and sensor ------------------------------------------------------


async def test_last_pump_is_one_sensor_on_an_account_device_named_for_the_entry(
    hass: HomeAssistant, setup_entry: MockConfigEntry
) -> None:
    device = account_device(hass, setup_entry)

    assert device.name == "Baby Tracker account"
    assert device.model == "Account"
    assert device.config_entries == {setup_entry.entry_id}
    assert state_of(hass, setup_entry).attributes["friendly_name"] == (
        "Baby Tracker account Last pump"
    )


async def test_the_account_email_is_nowhere_in_the_entity_id_or_the_device(
    hass: HomeAssistant, setup_entry: MockConfigEntry
) -> None:
    assert pump_entity(hass, setup_entry) == "sensor.baby_tracker_account_last_pump"
    device = account_device(hass, setup_entry)
    assert EMAIL not in str(device.name) + str(device.model) + str(device.configuration_url)


def state_of(hass: HomeAssistant, entry: MockConfigEntry):
    state = hass.states.get(pump_entity(hass, entry))
    assert state is not None
    return state


async def test_the_account_device_holds_only_the_last_pump_sensor(
    hass: HomeAssistant, setup_entry: MockConfigEntry
) -> None:
    device = account_device(hass, setup_entry)

    entries = er.async_entries_for_device(er.async_get(hass), device.id)

    assert [e.unique_id for e in entries] == [f"{setup_entry.entry_id}_last_pump"]
    assert entries[0].translation_key == "last_pump"


async def test_no_baby_has_a_last_pump_entity(
    hass: HomeAssistant, setup_entry: MockConfigEntry
) -> None:
    unique_ids = {
        e.unique_id
        for e in er.async_entries_for_config_entry(er.async_get(hass), setup_entry.entry_id)
    }

    assert unique_ids.isdisjoint({f"{BABY_ONE_ID}_last_pump", f"{BABY_TWO_ID}_last_pump"})
    assert hass.states.get("sensor.baby_one_last_pump") is None
    assert hass.states.get("sensor.baby_two_last_pump") is None


async def test_the_account_device_is_not_a_baby(
    hass: HomeAssistant, setup_entry: MockConfigEntry
) -> None:
    devices = dr.async_entries_for_config_entry(dr.async_get(hass), setup_entry.entry_id)

    assert sorted(d.name for d in devices if d.model != "Account") == ["Baby One", "Baby Two"]
    assert len([d for d in devices if d.model == "Account"]) == 1


async def test_last_pump_is_unknown_when_nothing_was_pumped(
    hass: HomeAssistant, setup_entry: MockConfigEntry
) -> None:
    state = state_of(hass, setup_entry)

    assert state.state == "unknown"
    assert "amount" not in state.attributes


async def test_last_pump_shows_the_newest_pump_whoever_logged_it(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer
) -> None:
    server.add_pump(DEVICE_A, _id(1), when=at(30), amount=volume(4), note="older")
    server.add_pump(DEVICE_B, _id(2), when=at(90), amount=volume(5), note="newest")
    server.add_pump(DEVICE_A, _id(3), when=at(60), amount=volume(6), note="middle")
    await tick(hass)

    state = state_of(hass, setup_entry)
    assert state.state == at(90).isoformat()
    assert state.attributes["note"] == "newest"
    assert state.attributes["amount"] == 5


async def test_last_pump_shows_amounts_and_durations(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer
) -> None:
    server.add_pump(
        DEVICE_A,
        _id(4),
        when=at(45),
        amount=volume(5),
        leftAmount=volume(2),
        rightAmount=volume(3),
        leftDuration=10,
        rightDuration=12,
        sides=3,
        note="morning",
    )
    await tick(hass)

    state = state_of(hass, setup_entry)
    assert state.state == at(45).isoformat()
    assert state.attributes["amount"] == 5
    assert state.attributes["unit"] == "fl. oz."
    assert state.attributes["left_amount"] == 2
    assert state.attributes["right_amount"] == 3
    assert state.attributes["left_duration_minutes"] == 10
    assert state.attributes["right_duration_minutes"] == 12
    assert state.attributes["sides"] == 3
    assert state.attributes["note"] == "morning"


async def test_a_total_only_pump_has_no_side_amounts(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer
) -> None:
    server.add_pump(
        DEVICE_A, _id(5), when=at(45), amount=volume(4), leftDuration=5, rightDuration=7
    )
    await tick(hass)

    state = state_of(hass, setup_entry)
    assert state.attributes["amount"] == 4
    assert "left_amount" not in state.attributes
    assert "right_amount" not in state.attributes


async def test_a_pumped_bottle_is_not_a_pump_session(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer
) -> None:
    server.add_record(DEVICE_A, "Pumped", _id(6), when=at(45), amount=volume(3))
    await tick(hass)

    assert state_of(hass, setup_entry).state == "unknown"


async def test_a_deleted_pump_falls_back_to_the_one_before(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer
) -> None:
    server.add_pump(DEVICE_A, _id(7), when=at(30), amount=volume(4))
    server.add_pump(DEVICE_A, _id(8), when=at(90), amount=volume(5))
    await tick(hass)
    assert state_of(hass, setup_entry).state == at(90).isoformat()

    server.add_record(DEVICE_A, "Pump", _id(8), when=at(90), baby=None, op=2, amount=volume(5))
    await tick(hass)

    assert state_of(hass, setup_entry).state == at(30).isoformat()


async def test_last_pump_survives_a_reload_from_saved_state(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer
) -> None:
    server.add_pump(DEVICE_A, _id(9), when=at(45), amount=volume(5))
    await tick(hass)

    assert await hass.config_entries.async_reload(setup_entry.entry_id)
    await hass.async_block_till_done()

    assert state_of(hass, setup_entry).state == at(45).isoformat()


async def test_a_pump_creates_no_baby_and_changes_no_babys_counts(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer
) -> None:
    server.add_pump(DEVICE_A, _id(10), when=at(5), amount=volume(5))
    await tick(hass)

    devices = dr.async_entries_for_config_entry(dr.async_get(hass), setup_entry.entry_id)
    assert len(devices) == 3  # two babies and the account
    for baby in ("baby_one", "baby_two"):
        assert hass.states.get(f"sensor.{baby}_feeds_today").state == "0"
        assert float(hass.states.get(f"sensor.{baby}_bottle_volume_today").state) == 0


async def test_an_old_style_pump_that_carries_a_baby_creates_no_baby_and_is_not_that_babys(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer
) -> None:
    ghost = baby_payload("Ghost Baby", BABY_THREE_ID)
    server.add_record(DEVICE_A, "Pump", _id(11), when=at(70), baby=ghost, amount=volume(5))
    server.add_record(DEVICE_A, "Pump", _id(12), when=at(80), baby=None, amount=volume(2))
    await tick(hass)

    assert dr.async_get(hass).async_get_device(identifiers={(DOMAIN, BABY_THREE_ID)}) is None
    assert hass.states.get("sensor.ghost_baby_last_diaper") is None
    assert state_of(hass, setup_entry).state == at(80).isoformat()
    assert hass.states.get("sensor.baby_one_feeds_today").state == "0"


# --- two accounts: each has its own device and pump ----------------------------------------------


async def test_two_entries_each_have_their_own_account_device_and_sensor(
    hass: HomeAssistant, setup_entry: MockConfigEntry, second_entry: MockConfigEntry
) -> None:
    first, second = account_device(hass, setup_entry), account_device(hass, second_entry)

    assert first.id != second.id
    assert first.name == second.name == "Baby Tracker account"
    assert pump_entity(hass, setup_entry) == "sensor.baby_tracker_account_last_pump"
    assert pump_entity(hass, second_entry) == "sensor.baby_tracker_account_last_pump_2"
    assert first.config_entries == {setup_entry.entry_id}
    assert second.config_entries == {second_entry.entry_id}
    assert pump_entity(hass, setup_entry) != pump_entity(hass, second_entry)


async def test_each_entrys_last_pump_shows_only_its_own_accounts_pump(
    hass: HomeAssistant,
    setup_entry: MockConfigEntry,
    second_entry: MockConfigEntry,
    server: FakeServer,
    second_server: FakeServer,
) -> None:
    server.add_pump(DEVICE_A, _id(20), when=at(40), amount=volume(4))
    second_server.add_pump(DEVICE_B, _id(21), when=at(80), amount=volume(6))
    await tick(hass)

    assert state_of(hass, setup_entry).state == at(40).isoformat()
    assert state_of(hass, second_entry).state == at(80).isoformat()
    assert state_of(hass, setup_entry).attributes["amount"] == 4
    assert state_of(hass, second_entry).attributes["amount"] == 6


async def test_logging_a_pump_for_one_entry_leaves_the_other_untouched(
    hass: HomeAssistant,
    setup_entry: MockConfigEntry,
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

    assert server.posted == []  # type: ignore[attr-defined]
    only_pump(second_server)
    assert state_of(hass, second_entry).state == "2026-10-26T08:00:00+00:00"
    assert state_of(hass, setup_entry).state == "unknown"


# --- log_pump -------------------------------------------------------------------------------------


async def test_log_pump_by_config_entry_id_writes_a_pump_with_no_baby_and_the_real_keys(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer
) -> None:
    await log_pump(
        hass,
        config_entry_id=setup_entry.entry_id,
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


async def test_log_pump_updates_the_last_pump_sensor_without_a_poll(
    hass: HomeAssistant, setup_entry: MockConfigEntry
) -> None:
    await log_pump(
        hass,
        config_entry_id=setup_entry.entry_id,
        left_amount=2,
        right_amount=3,
        unit="fl. oz.",
        time="2026-10-26T08:00:00+00:00",
    )

    state = state_of(hass, setup_entry)
    assert state.state == "2026-10-26T08:00:00+00:00"
    assert state.attributes["amount"] == 5


async def test_log_pump_accepts_the_account_device(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer
) -> None:
    await log_pump(hass, device=account_device(hass, setup_entry).id, left_amount=3, unit="fl. oz.")

    assert "baby" not in only_pump(server).to_payload()


async def test_log_pump_accepts_a_baby_device_and_resolves_it_to_its_account(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer
) -> None:
    await log_pump(hass, device=device_id(hass, BABY_TWO_ID), left_amount=3, unit="fl. oz.")

    record = only_pump(server)
    assert "baby" not in record.to_payload()  # the baby that was named is not recorded
    assert state_of(hass, setup_entry).state != "unknown"


async def test_log_pump_through_a_second_accounts_baby_device_posts_to_that_account(
    hass: HomeAssistant,
    setup_entry: MockConfigEntry,
    second_entry: MockConfigEntry,
    server: FakeServer,
    second_server: FakeServer,
) -> None:
    await log_pump(hass, device=device_id(hass, BABY_THREE_ID), left_amount=3, unit="fl. oz.")

    assert server.posted == []  # type: ignore[attr-defined]
    only_pump(second_server)


async def test_log_pump_through_a_second_accounts_device_posts_to_that_account(
    hass: HomeAssistant,
    setup_entry: MockConfigEntry,
    second_entry: MockConfigEntry,
    server: FakeServer,
    second_server: FakeServer,
) -> None:
    await log_pump(
        hass, device=account_device(hass, second_entry).id, left_amount=3, unit="fl. oz."
    )

    assert server.posted == []  # type: ignore[attr-defined]
    only_pump(second_server)


async def test_log_pump_needs_an_account_or_a_device(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer
) -> None:
    with pytest.raises(vol.Invalid, match="config_entry_id"):
        await log_pump(hass, left_amount=3)
    assert server.posted == []  # type: ignore[attr-defined]


async def test_log_pump_takes_one_of_config_entry_id_and_device_not_both(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer
) -> None:
    with pytest.raises(vol.Invalid, match="at most one"):
        await log_pump(
            hass,
            config_entry_id=setup_entry.entry_id,
            device=account_device(hass, setup_entry).id,
            left_amount=3,
        )
    assert server.posted == []  # type: ignore[attr-defined]


async def test_log_pump_still_accepts_baby_as_an_alias_of_device(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer
) -> None:
    await log_pump(hass, baby=device_id(hass, BABY_ONE_ID), left_amount=3, unit="fl. oz.")

    assert "baby" not in only_pump(server).to_payload()


async def test_the_baby_alias_resolves_a_second_accounts_baby_to_that_account(
    hass: HomeAssistant,
    setup_entry: MockConfigEntry,
    second_entry: MockConfigEntry,
    server: FakeServer,
    second_server: FakeServer,
) -> None:
    await log_pump(hass, baby=device_id(hass, BABY_THREE_ID), left_amount=3, unit="fl. oz.")

    assert server.posted == []  # type: ignore[attr-defined]
    only_pump(second_server)


async def test_the_baby_alias_for_an_unknown_device_is_a_validation_error(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer
) -> None:
    with pytest.raises(ServiceValidationError) as raised:
        await log_pump(hass, baby="nope", left_amount=3)
    assert raised.value.translation_key == "baby_not_found"


@pytest.mark.parametrize("other", ["device", "config_entry_id"])
async def test_baby_with_device_or_an_account_is_refused_as_two_targets(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer, other: str
) -> None:
    value = account_device(hass, setup_entry).id if other == "device" else setup_entry.entry_id
    with pytest.raises(vol.Invalid, match="at most one"):
        await log_pump(hass, baby=device_id(hass, BABY_ONE_ID), left_amount=3, **{other: value})
    assert server.posted == []  # type: ignore[attr-defined]


async def test_log_pump_for_an_unknown_account_is_a_validation_error(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer
) -> None:
    with pytest.raises(ServiceValidationError) as raised:
        await log_pump(hass, config_entry_id="no-such-entry", left_amount=3)
    assert raised.value.translation_key == "entry_not_found"
    assert server.posted == []  # type: ignore[attr-defined]


async def test_log_pump_for_an_account_that_is_not_loaded_is_a_validation_error(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer
) -> None:
    device = account_device(hass, setup_entry)
    await hass.config_entries.async_unload(setup_entry.entry_id)
    assert setup_entry.state is ConfigEntryState.NOT_LOADED

    with pytest.raises(ServiceValidationError) as raised:
        await log_pump(hass, device=device.id, left_amount=3)
    assert raised.value.translation_key == "entry_not_loaded"
    with pytest.raises(ServiceValidationError) as raised:
        await log_pump(hass, config_entry_id=setup_entry.entry_id, left_amount=3)
    assert raised.value.translation_key == "entry_not_loaded"


@pytest.mark.parametrize("device", ["nope", "00000000000000000000000000000000"])
async def test_log_pump_for_a_device_that_is_not_ours_is_a_validation_error(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer, device: str
) -> None:
    with pytest.raises(ServiceValidationError) as raised:
        await log_pump(hass, device=device, left_amount=3)
    assert raised.value.translation_key == "baby_not_found"
    assert server.posted == []  # type: ignore[attr-defined]


async def test_log_pump_for_another_integrations_device_is_a_validation_error(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer
) -> None:
    other = MockConfigEntry(domain="other_integration")
    other.add_to_hass(hass)
    foreign = dr.async_get(hass).async_get_or_create(
        config_entry_id=other.entry_id, identifiers={("other_integration", "thing")}
    )

    with pytest.raises(ServiceValidationError) as raised:
        await log_pump(hass, device=foreign.id, left_amount=3)
    assert raised.value.translation_key == "baby_not_found"
    assert server.posted == []  # type: ignore[attr-defined]


async def test_log_pump_still_requires_an_amount_and_a_unit_it_supports(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer
) -> None:
    with pytest.raises(ServiceValidationError) as raised:
        await log_pump(hass, config_entry_id=setup_entry.entry_id, left_minutes=5)
    assert raised.value.translation_key == "no_amount"
    with pytest.raises(ServiceValidationError) as raised:
        await log_pump(hass, config_entry_id=setup_entry.entry_id, left_amount=60, unit="mL")
    assert raised.value.translation_key == "metric_not_supported"
    assert server.posted == []  # type: ignore[attr-defined]


async def test_a_baby_service_given_the_account_device_is_a_validation_error(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer
) -> None:
    with pytest.raises(ServiceValidationError) as raised:
        await hass.services.async_call(
            DOMAIN,
            "log_diaper",
            {"baby": account_device(hass, setup_entry).id, "type": "wet"},
            blocking=True,
        )
    assert raised.value.translation_key == "baby_not_found"
    assert server.posted == []  # type: ignore[attr-defined]


async def test_undo_removes_a_pump_by_account(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer
) -> None:
    await log_pump(hass, config_entry_id=setup_entry.entry_id, left_amount=3, unit="fl. oz.")

    await undo(hass, config_entry_id=setup_entry.entry_id)

    assert [op for _, op, _ in posted(server)] == [0, 2]
    assert state_of(hass, setup_entry).state == "unknown"


async def test_undo_for_a_baby_never_picks_a_pump(
    hass: HomeAssistant, setup_entry: MockConfigEntry, server: FakeServer
) -> None:
    await log_pump(hass, config_entry_id=setup_entry.entry_id, left_amount=3, unit="fl. oz.")
    pump_id = posted(server)[0][2].object_id

    with pytest.raises(ServiceValidationError) as raised:
        await undo(hass, baby=device_id(hass, BABY_ONE_ID))
    assert raised.value.translation_key == "nothing_to_undo"
    with pytest.raises(ServiceValidationError) as raised:
        await undo(hass, baby=device_id(hass, BABY_ONE_ID), object_id=pump_id)
    assert raised.value.translation_key == "record_not_for_baby"
    assert len(posted(server)) == 1


# --- one-time cleanup of the old per-baby last-pump entities ---------------------------------


def add_old_pump_entity(
    hass: HomeAssistant, entry: MockConfigEntry, baby_id: str, name: str
) -> er.RegistryEntry:
    """What an earlier version registered: ``<baby_id>_last_pump`` on the baby's device."""
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, baby_id)}, name=name
    )
    return er.async_get(hass).async_get_or_create(
        "sensor",
        DOMAIN,
        f"{baby_id}_last_pump",
        config_entry=entry,
        device_id=device.id,
        translation_key="last_pump",
        has_entity_name=True,
    )


async def test_setup_removes_the_old_per_baby_last_pump_entities(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    config_entry.add_to_hass(hass)
    old_one = add_old_pump_entity(hass, config_entry, BABY_ONE_ID, "Baby One")
    old_two = add_old_pump_entity(hass, config_entry, BABY_TWO_ID, "Baby Two")
    registry = er.async_get(hass)
    assert registry.async_get(old_one.entity_id) is not None

    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    assert registry.async_get(old_one.entity_id) is None
    assert registry.async_get(old_two.entity_id) is None
    assert registry.async_get_entity_id("sensor", DOMAIN, f"{BABY_ONE_ID}_last_pump") is None
    assert registry.async_get_entity_id("sensor", DOMAIN, f"{BABY_TWO_ID}_last_pump") is None
    assert hass.states.get(old_one.entity_id) is None
    # ... and the account's replaces them.
    assert state_of(hass, config_entry).state == "unknown"


async def test_the_cleanup_leaves_every_other_per_baby_entity_alone(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    config_entry.add_to_hass(hass)
    add_old_pump_entity(hass, config_entry, BABY_ONE_ID, "Baby One")

    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    registry = er.async_get(hass)
    assert registry.async_get_entity_id("sensor", DOMAIN, f"{BABY_ONE_ID}_last_pump") is None
    for key in ("last_diaper", "last_feed", "last_sleep", "diapers_today", "feeds_today"):
        assert registry.async_get_entity_id("sensor", DOMAIN, f"{BABY_ONE_ID}_{key}") is not None, (
            key
        )
    assert hass.states.get("sensor.baby_one_last_diaper") is not None


async def test_the_cleanup_runs_again_harmlessly_on_a_reload(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    config_entry.add_to_hass(hass)
    add_old_pump_entity(hass, config_entry, BABY_ONE_ID, "Baby One")
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    first = pump_entity(hass, config_entry)

    assert await hass.config_entries.async_reload(config_entry.entry_id)
    await hass.async_block_till_done()

    assert pump_entity(hass, config_entry) == first
    assert (
        er.async_get(hass).async_get_entity_id("sensor", DOMAIN, f"{BABY_ONE_ID}_last_pump") is None
    )


async def test_the_cleanup_keeps_the_new_account_sensors_registry_entry(
    hass: HomeAssistant, setup_entry: MockConfigEntry
) -> None:
    registry = er.async_get(hass)
    before = registry.async_get(pump_entity(hass, setup_entry))

    assert await hass.config_entries.async_reload(setup_entry.entry_id)
    await hass.async_block_till_done()

    after = registry.async_get(pump_entity(hass, setup_entry))
    assert before is not None and after is not None
    assert after.id == before.id  # kept, not removed and recreated


async def test_the_cleanup_runs_for_every_entry_and_each_keeps_its_own_account_sensor(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    config_entry.add_to_hass(hass)
    other = MockConfigEntry(
        domain=DOMAIN, title=EMAIL_TWO, unique_id=EMAIL_TWO, data=dict(config_entry.data)
    )
    other.add_to_hass(hass)
    old_first = add_old_pump_entity(hass, config_entry, BABY_ONE_ID, "Baby One")
    old_second = add_old_pump_entity(hass, other, BABY_THREE_ID, "Baby Three")

    await hass.config_entries.async_setup(config_entry.entry_id)  # sets up the whole domain
    await hass.async_block_till_done()

    registry = er.async_get(hass)
    assert registry.async_get(old_first.entity_id) is None
    assert registry.async_get(old_second.entity_id) is None
    assert pump_entity(hass, config_entry) != pump_entity(hass, other)
    assert account_device(hass, config_entry).id != account_device(hass, other).id


async def test_the_cleanup_removes_an_old_entity_for_a_baby_no_longer_in_the_account(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    config_entry.add_to_hass(hass)
    gone = add_old_pump_entity(hass, config_entry, BABY_THREE_ID, "Baby Three")

    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    assert er.async_get(hass).async_get(gone.entity_id) is None
