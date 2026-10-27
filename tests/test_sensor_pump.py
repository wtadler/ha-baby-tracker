"""Core PR 5: the account device and the last-pump sensor."""

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
)

from . import EMAIL, NOW, get_baby_device, tick
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
from .fake_server import FakeServer
from .helpers import LAST_PUMP, account_device, pump_entity, state_of


def _pump_id(number: int) -> str:
    return f"B0000000-0000-4000-8000-{number:012d}"


@pytest.fixture(autouse=True)
def _start_at_now(freezer: FrozenDateTimeFactory) -> None:
    """Freeze the clock at NOW before anything is set up.

    Timers scheduled at setup then share the clock the tests move, so moving it forward and
    ticking reaches them the way it does in core's tests.
    """
    freezer.move_to(NOW)


async def test_last_pump_is_one_sensor_on_an_account_device_named_for_the_entry(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    device = account_device(hass, init_integration)

    assert device.name == "Baby Tracker account"
    assert device.model == "Account"
    assert device.config_entry_id == init_integration.entry_id
    assert state_of(hass, init_integration).attributes["friendly_name"] == (
        "Baby Tracker account Last pump"
    )


async def test_the_account_email_is_nowhere_in_the_entity_id_or_the_device(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    assert pump_entity(hass, init_integration) == "sensor.baby_tracker_account_last_pump"
    device = account_device(hass, init_integration)
    assert EMAIL not in str(device.name) + str(device.model) + str(device.configuration_url)


async def test_the_account_device_holds_only_the_last_pump_and_pump_timer_sensors(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    device = account_device(hass, init_integration)

    entries = er.async_entries_for_device(er.async_get(hass), device.id)

    sensors = [e for e in entries if e.domain == "sensor"]
    assert sorted(e.unique_id for e in sensors) == [
        f"{init_integration.entry_id}_last_pump",
        f"{init_integration.entry_id}_pump_timer",
    ]
    assert {e.translation_key for e in sensors} == {"last_pump", "pump_timer"}


async def test_no_baby_has_a_last_pump_entity(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    unique_ids = {
        e.unique_id
        for e in er.async_entries_for_config_entry(er.async_get(hass), init_integration.entry_id)
    }

    assert unique_ids.isdisjoint({f"{BABY_ONE_ID}_last_pump", f"{BABY_TWO_ID}_last_pump"})
    assert hass.states.get("sensor.baby_one_last_pump") is None
    assert hass.states.get("sensor.baby_two_last_pump") is None


async def test_the_account_device_is_not_a_baby(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    devices = dr.async_entries_for_config_entry(dr.async_get(hass), init_integration.entry_id)

    assert sorted(d.name for d in devices if d.model != "Account") == ["Baby One", "Baby Two"]
    assert len([d for d in devices if d.model == "Account"]) == 1


async def test_last_pump_is_unknown_when_nothing_was_pumped(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    state = state_of(hass, init_integration)

    assert state.state == "unknown"
    assert state.attributes["amount"] is None


async def test_last_pump_shows_the_newest_pump_whoever_logged_it(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    server.add_pump(DEVICE_A, _pump_id(1), when=at(30), amount=volume(4), note="older")
    server.add_pump(DEVICE_B, _pump_id(2), when=at(90), amount=volume(5), note="newest")
    server.add_pump(DEVICE_A, _pump_id(3), when=at(60), amount=volume(6), note="middle")
    await tick(hass)

    state = state_of(hass, init_integration)
    assert state.state == at(90).isoformat()
    assert state.attributes["note"] == "newest"
    assert state.attributes["amount"] == 5


async def test_last_pump_shows_amounts_and_durations(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    server.add_pump(
        DEVICE_A,
        _pump_id(4),
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

    state = state_of(hass, init_integration)
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
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    server.add_pump(
        DEVICE_A, _pump_id(5), when=at(45), amount=volume(4), leftDuration=5, rightDuration=7
    )
    await tick(hass)

    state = state_of(hass, init_integration)
    assert state.attributes["amount"] == 4
    assert state.attributes["left_amount"] is None
    assert state.attributes["right_amount"] is None


async def test_a_pumped_bottle_is_not_a_pump_session(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    server.add_record(DEVICE_A, "Pumped", _pump_id(6), when=at(45), amount=volume(3))
    await tick(hass)

    assert state_of(hass, init_integration).state == "unknown"


@pytest.mark.engine
async def test_a_deleted_pump_falls_back_to_the_one_before(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    server.add_pump(DEVICE_A, _pump_id(7), when=at(30), amount=volume(4))
    server.add_pump(DEVICE_A, _pump_id(8), when=at(90), amount=volume(5))
    await tick(hass)
    assert state_of(hass, init_integration).state == at(90).isoformat()

    server.add_record(DEVICE_A, "Pump", _pump_id(8), when=at(90), baby=None, op=2, amount=volume(5))
    await tick(hass)

    assert state_of(hass, init_integration).state == at(30).isoformat()


async def test_last_pump_survives_a_reload_from_saved_state(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    server.add_pump(DEVICE_A, _pump_id(9), when=at(45), amount=volume(5))
    await tick(hass)

    assert await hass.config_entries.async_reload(init_integration.entry_id)
    await hass.async_block_till_done()

    assert state_of(hass, init_integration).state == at(45).isoformat()


async def test_a_pump_creates_no_baby_and_changes_no_babys_counts(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    server.add_pump(DEVICE_A, _pump_id(10), when=at(5), amount=volume(5))
    await tick(hass)

    devices = dr.async_entries_for_config_entry(dr.async_get(hass), init_integration.entry_id)
    assert len(devices) == 3  # two babies and the account
    for baby in ("baby_one", "baby_two"):
        assert hass.states.get(f"sensor.{baby}_feeds_today").state == "0"
        assert float(hass.states.get(f"sensor.{baby}_bottle_volume_today").state) == 0


async def test_an_old_style_pump_that_carries_a_baby_creates_no_baby_and_is_not_that_babys(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    ghost = baby_payload("Ghost Baby", BABY_THREE_ID)
    server.add_record(DEVICE_A, "Pump", _pump_id(11), when=at(70), baby=ghost, amount=volume(5))
    server.add_record(DEVICE_A, "Pump", _pump_id(12), when=at(80), baby=None, amount=volume(2))
    await tick(hass)

    assert get_baby_device(dr.async_get(hass), BABY_THREE_ID) is None
    assert hass.states.get("sensor.ghost_baby_last_diaper") is None
    assert state_of(hass, init_integration).state == at(80).isoformat()
    assert hass.states.get("sensor.baby_one_feeds_today").state == "0"


async def test_two_entries_each_have_their_own_account_device_and_sensor(
    hass: HomeAssistant, init_integration: MockConfigEntry, second_entry: MockConfigEntry
) -> None:
    first, second = account_device(hass, init_integration), account_device(hass, second_entry)

    assert first.id != second.id
    assert first.name == second.name == "Baby Tracker account"
    assert pump_entity(hass, init_integration) == "sensor.baby_tracker_account_last_pump"
    assert pump_entity(hass, second_entry) == "sensor.baby_tracker_account_last_pump_2"
    assert first.config_entry_id == init_integration.entry_id
    assert second.config_entry_id == second_entry.entry_id
    assert pump_entity(hass, init_integration) != pump_entity(hass, second_entry)


async def test_each_entrys_last_pump_shows_only_its_own_accounts_pump(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    second_entry: MockConfigEntry,
    server: FakeServer,
    second_server: FakeServer,
) -> None:
    server.add_pump(DEVICE_A, _pump_id(20), when=at(40), amount=volume(4))
    second_server.add_pump(DEVICE_B, _pump_id(21), when=at(80), amount=volume(6))
    await tick(hass)

    assert state_of(hass, init_integration).state == at(40).isoformat()
    assert state_of(hass, second_entry).state == at(80).isoformat()
    assert state_of(hass, init_integration).attributes["amount"] == 4
    assert state_of(hass, second_entry).attributes["amount"] == 6


async def test_a_pump_alone_reaches_the_account_sensor_on_a_refresh(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    """The last pump lives in the sync data, so a pump-only change isn't dropped."""
    assert hass.states.get(LAST_PUMP).state == "unknown"
    server.add_pump(
        "BBBBBBBB-0000-4000-8000-00000000000B", "F0000000-0000-4000-8000-000000000001", when=at(45)
    )
    await tick(hass)
    assert hass.states.get(LAST_PUMP).state == at(45).isoformat()
