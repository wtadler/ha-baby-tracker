"""Core PR 21: the pump timer. A switch and a timestamp sensor on the account, and start_pump
and stop_pump.

A pump belongs to the account, not a baby. Like the nursing timer, this one lives only in Home
Assistant until it stops (the app doesn't sync a running pump), is saved with a ``Store``, and
becomes one Pump add at the stop. The write has equal ``leftDuration`` and ``rightDuration``, the
total ``amount`` when one is given at the stop, and never a ``baby``. Everything here is invented.
"""

import asyncio
import logging
from datetime import timedelta
from typing import Any

import probatio
import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.storage import Store
from homeassistant.util.unit_system import METRIC_SYSTEM, US_CUSTOMARY_SYSTEM
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.nighp_baby_tracker.const import DOMAIN
from custom_components.nighp_baby_tracker.pybabytracker import (
    OP_ADD,
    BabyTrackerConnectionError,
    Pump,
    Volume,
)

from . import NOW, sync_interval
from .factories import volume
from .fake_server import FakeServer
from .helpers import (
    LAST_PUMP,
    PUMP_SWITCH,
    PUMP_TIMER,
    account_device,
    call,
    minutes,
    posted,
    state,
    toggle,
    turn_off,
    turn_on,
    undo,
)


@pytest.fixture(autouse=True)
def _start_at_now(freezer: FrozenDateTimeFactory) -> None:
    """Freeze the clock at NOW before anything is set up."""
    freezer.move_to(NOW)


def pump_posts(server: FakeServer) -> list[Pump]:
    return [r for _, op, r in posted(server) if op == OP_ADD and isinstance(r, Pump)]


def only_pump(server: FakeServer) -> Pump:
    [record] = pump_posts(server)
    return record


def issue(hass: HomeAssistant, entry: MockConfigEntry) -> Any:
    return ir.async_get(hass).async_get_issue(DOMAIN, f"pump_timer_long_{entry.entry_id}")


async def at(hass: HomeAssistant, freezer: FrozenDateTimeFactory, minute: float) -> None:
    """Move the clock to ``minute`` after NOW and let what was scheduled for then run."""
    freezer.move_to(minutes(minute))
    async_fire_time_changed(hass, minutes(minute))
    await hass.async_block_till_done()


async def expect_error(
    hass: HomeAssistant, domain: str, service: str, data: dict[str, Any], key: str
) -> HomeAssistantError:
    with pytest.raises(HomeAssistantError) as err:
        await hass.services.async_call(domain, service, data, blocking=True)
    assert getattr(err.value, "translation_key", None) == key
    assert getattr(err.value, "translation_domain", None) == DOMAIN
    return err.value


async def start(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    await hass.services.async_call(
        DOMAIN, "start_pump", {"config_entry_id": entry.entry_id}, blocking=True
    )


async def stop(hass: HomeAssistant, entry: MockConfigEntry, **data: Any) -> None:
    await hass.services.async_call(
        DOMAIN, "stop_pump", {"config_entry_id": entry.entry_id, **data}, blocking=True
    )


# --- entities ------------------------------------------------------------------------------------


async def test_the_account_device_gets_a_pump_switch_and_a_pump_timer_sensor(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    device = account_device(hass, init_integration)
    entries = er.async_entries_for_device(er.async_get(hass), device.id)

    [switch] = [e for e in entries if e.domain == "switch"]
    assert switch.unique_id == f"{init_integration.entry_id}_pump"
    assert switch.entity_id == PUMP_SWITCH
    [sensor] = [e for e in entries if e.domain == "sensor" and e.translation_key == "pump_timer"]
    assert sensor.unique_id == f"{init_integration.entry_id}_pump_timer"
    assert sensor.entity_id == PUMP_TIMER
    assert state(hass, PUMP_SWITCH).name == "Baby Tracker account Pump timer"
    assert device.name == "Baby Tracker account"


async def test_an_idle_pump_timer_reads_off_and_unknown(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    assert state(hass, PUMP_SWITCH).state == "off"
    assert state(hass, PUMP_TIMER).state == "unknown"


async def test_no_pump_entity_is_added_to_a_baby_device(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    registry = er.async_get(hass)
    babies = [
        device
        for device in dr.async_entries_for_config_entry(
            dr.async_get(hass), init_integration.entry_id
        )
        if device.id != account_device(hass, init_integration).id
    ]
    assert babies
    for device in babies:
        assert not [
            e
            for e in er.async_entries_for_device(registry, device.id)
            if "pump_timer" in e.unique_id
        ]


# --- start and stop with the switch --------------------------------------------------------------


async def test_turning_the_switch_on_posts_nothing_and_shows_the_start_time(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    await turn_on(hass, PUMP_SWITCH)

    assert server.posted == []
    assert state(hass, PUMP_SWITCH).state == "on"
    assert state(hass, PUMP_TIMER).state == NOW.isoformat()


async def test_turning_the_switch_on_then_off_posts_one_pump_with_equal_rounded_durations(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    server: FakeServer,
    freezer: FrozenDateTimeFactory,
) -> None:
    hass.config.units = US_CUSTOMARY_SYSTEM  # the zero follows the unit system
    await turn_on(hass, PUMP_SWITCH)
    freezer.move_to(NOW + timedelta(minutes=12, seconds=20))

    await turn_off(hass, PUMP_SWITCH)

    record = only_pump(server)
    payload = record.to_payload()
    assert (payload["leftDuration"], payload["rightDuration"]) == (12, 12)
    assert record.time == NOW  # the session's start
    assert "baby" not in payload
    # No volume was given at the stop: the app writes 0 for that, never a missing key.
    assert payload["amount"] == volume(0)
    assert "leftAmount" not in payload
    assert "rightAmount" not in payload
    assert payload["sides"] == 0
    assert state(hass, PUMP_SWITCH).state == "off"
    assert state(hass, PUMP_TIMER).state == "unknown"


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (0, 1),  # a stop at once still logs a minute, as nursing does
        (29, 1),
        (30, 1),
        (89, 1),
        (90, 2),  # half up
        (149, 2),
        (150, 3),
        (3600, 60),
        (4 * 3600, 240),
        (5 * 3600, 240),  # a forgotten timer is capped at the most the library takes
    ],
)
async def test_the_minutes_are_rounded_half_up_with_a_floor_of_one_and_a_cap_of_240(
    seconds: int,
    expected: int,
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    server: FakeServer,
    freezer: FrozenDateTimeFactory,
) -> None:
    await turn_on(hass, PUMP_SWITCH)
    freezer.move_to(NOW + timedelta(seconds=seconds))

    await turn_off(hass, PUMP_SWITCH)

    record = only_pump(server)
    assert (record.left_duration, record.right_duration) == (expected, expected)


async def test_turning_the_switch_on_twice_is_a_clear_error_and_keeps_the_first_start(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    server: FakeServer,
    freezer: FrozenDateTimeFactory,
) -> None:
    await turn_on(hass, PUMP_SWITCH)
    freezer.move_to(minutes(5))

    await expect_error(
        hass, "switch", "turn_on", {"entity_id": PUMP_SWITCH}, "pump_already_running"
    )

    assert server.posted == []
    assert state(hass, PUMP_TIMER).state == NOW.isoformat()
    assert state(hass, PUMP_SWITCH).state == "on"


async def test_two_simultaneous_turn_ons_start_one_timer_and_refuse_the_other(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    results = await asyncio.gather(
        turn_on(hass, PUMP_SWITCH), turn_on(hass, PUMP_SWITCH), return_exceptions=True
    )

    errors = [r for r in results if isinstance(r, BaseException)]
    assert len(errors) == 1
    assert isinstance(errors[0], ServiceValidationError)
    assert errors[0].translation_key == "pump_already_running"
    assert server.posted == []
    assert state(hass, PUMP_SWITCH).state == "on"


async def test_turning_the_switch_off_when_idle_is_a_clear_error_and_posts_nothing(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    await expect_error(hass, "switch", "turn_off", {"entity_id": PUMP_SWITCH}, "no_pump_timer")

    assert server.posted == []
    assert state(hass, PUMP_SWITCH).state == "off"


async def test_the_switch_can_start_again_after_a_stop(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    server: FakeServer,
    freezer: FrozenDateTimeFactory,
) -> None:
    await toggle(hass, PUMP_SWITCH)
    freezer.move_to(minutes(10))
    await toggle(hass, PUMP_SWITCH)
    freezer.move_to(minutes(20))
    await toggle(hass, PUMP_SWITCH)
    freezer.move_to(minutes(35))
    await toggle(hass, PUMP_SWITCH)

    first, second = pump_posts(server)
    assert (first.left_duration, second.left_duration) == (10, 15)
    assert first.object_id != second.object_id
    assert second.time == minutes(20)


async def test_the_stopped_pump_is_the_last_pump_and_can_be_undone(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    server: FakeServer,
    freezer: FrozenDateTimeFactory,
) -> None:
    await turn_on(hass, PUMP_SWITCH)
    freezer.move_to(minutes(8))
    await turn_off(hass, PUMP_SWITCH)

    last = state(hass, LAST_PUMP)
    assert last.state == NOW.isoformat()
    assert last.attributes["left_duration_minutes"] == 8
    assert last.attributes["right_duration_minutes"] == 8
    assert last.attributes["amount"] is None  # an amount of 0 is no volume, not a 0 fl. oz. pump
    assert last.attributes["unit"] is None

    await undo(hass, config_entry_id=init_integration.entry_id)

    assert state(hass, LAST_PUMP).state == "unknown"


# --- the actions ---------------------------------------------------------------------------------


async def test_start_pump_starts_the_timer_the_switch_shows(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    await start(hass, init_integration)

    assert server.posted == []
    assert state(hass, PUMP_SWITCH).state == "on"
    assert state(hass, PUMP_TIMER).state == NOW.isoformat()


async def test_start_pump_while_running_is_a_clear_error_and_posts_nothing(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    server: FakeServer,
    freezer: FrozenDateTimeFactory,
) -> None:
    await start(hass, init_integration)
    freezer.move_to(minutes(3))

    await expect_error(
        hass,
        DOMAIN,
        "start_pump",
        {"config_entry_id": init_integration.entry_id},
        "pump_already_running",
    )

    assert server.posted == []
    assert state(hass, PUMP_TIMER).state == NOW.isoformat()


async def test_the_switch_on_then_start_pump_is_the_same_error(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    await turn_on(hass, PUMP_SWITCH)

    await expect_error(
        hass,
        DOMAIN,
        "start_pump",
        {"config_entry_id": init_integration.entry_id},
        "pump_already_running",
    )


async def test_stop_pump_without_an_amount_logs_the_minutes_with_an_amount_of_zero(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    server: FakeServer,
    freezer: FrozenDateTimeFactory,
) -> None:
    hass.config.units = US_CUSTOMARY_SYSTEM  # the zero follows the unit system
    await start(hass, init_integration)
    freezer.move_to(minutes(7))

    await stop(hass, init_integration)

    record = only_pump(server)
    payload = record.to_payload()
    assert (payload["leftDuration"], payload["rightDuration"]) == (7, 7)
    assert payload["amount"]["value"] == 0
    assert "leftAmount" not in payload
    assert "rightAmount" not in payload
    assert record.volume == Volume(0, "oz")
    assert "baby" not in payload
    assert state(hass, PUMP_SWITCH).state == "off"


@pytest.mark.parametrize(
    ("unit", "expected"),
    [
        ("fl. oz.", Volume(4.5, "oz")),
        (None, Volume(4.5, "oz")),  # fl. oz. is the default, as for every other volume
        ("mL", Volume(4.5, "ml")),
    ],
)
async def test_stop_pump_with_an_amount_writes_the_total_and_no_side_amounts(
    unit: str | None,
    expected: Volume,
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    server: FakeServer,
    freezer: FrozenDateTimeFactory,
) -> None:
    await start(hass, init_integration)
    freezer.move_to(minutes(14))

    await stop(hass, init_integration, amount=4.5, **({"unit": unit} if unit else {}))

    record = only_pump(server)
    payload = record.to_payload()
    assert record.volume == expected
    assert (payload["leftDuration"], payload["rightDuration"]) == (14, 14)
    assert "leftAmount" not in payload
    assert "rightAmount" not in payload
    assert "baby" not in payload
    assert payload["sides"] == 0
    last = state(hass, LAST_PUMP)
    assert last.attributes["amount"] == 4.5
    assert last.attributes["left_amount"] is None


async def test_stop_pump_saves_a_note(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    server: FakeServer,
    freezer: FrozenDateTimeFactory,
) -> None:
    await start(hass, init_integration)
    freezer.move_to(minutes(5))

    await stop(hass, init_integration, note="invented note")

    assert only_pump(server).note == "invented note"


async def test_stop_pump_when_idle_is_a_clear_error_and_posts_nothing(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    await expect_error(
        hass, DOMAIN, "stop_pump", {"config_entry_id": init_integration.entry_id}, "no_pump_timer"
    )
    await expect_error(
        hass,
        DOMAIN,
        "stop_pump",
        {"config_entry_id": init_integration.entry_id, "amount": 3},
        "no_pump_timer",
    )

    assert server.posted == []


@pytest.mark.parametrize(
    ("data", "key"),
    [
        ({"amount": 0}, "invalid_amount"),
        ({"amount": -2}, "invalid_amount"),
        ({"amount": float("nan")}, "invalid_amount"),
        ({"amount": float("inf")}, "invalid_amount"),
        ({"amount": 0.004}, "amount_too_small"),
    ],
)
async def test_a_bad_amount_at_the_stop_is_refused_and_keeps_the_timer_running(
    data: dict[str, Any],
    key: str,
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    server: FakeServer,
    freezer: FrozenDateTimeFactory,
) -> None:
    await start(hass, init_integration)
    freezer.move_to(minutes(6))

    await expect_error(
        hass, DOMAIN, "stop_pump", {"config_entry_id": init_integration.entry_id, **data}, key
    )

    assert server.posted == []
    assert state(hass, PUMP_SWITCH).state == "on"
    await stop(hass, init_integration, amount=2)  # the timer is still there
    assert only_pump(server).left_duration == 6


async def test_an_unknown_unit_at_the_stop_is_refused_by_the_action_and_keeps_the_timer(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    await start(hass, init_integration)

    with pytest.raises(probatio.MultipleInvalid):  # the schema refuses it before the action runs
        await stop(hass, init_integration, amount=2, unit="cups")

    assert server.posted == []
    assert state(hass, PUMP_SWITCH).state == "on"


async def test_a_failed_stop_keeps_the_timer_and_the_retry_lands_once_with_the_same_amount(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    server: FakeServer,
    freezer: FrozenDateTimeFactory,
) -> None:
    await start(hass, init_integration)
    server.post_errors.append((BabyTrackerConnectionError("down"), False))
    freezer.move_to(minutes(9))

    with pytest.raises(HomeAssistantError):
        await stop(hass, init_integration, amount=3)
    assert state(hass, PUMP_SWITCH).state == "on"
    await stop(hass, init_integration, amount=3)

    record = only_pump(server)
    assert record.volume == Volume(3, "oz")
    assert record.left_duration == 9
    assert state(hass, PUMP_SWITCH).state == "off"


async def test_stopping_during_a_failing_sync_says_so_and_keeps_the_timer(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    server: FakeServer,
    freezer: FrozenDateTimeFactory,
) -> None:
    await turn_on(hass, PUMP_SWITCH)
    freezer.move_to(minutes(5))
    server.request_error = BabyTrackerConnectionError("down")

    await expect_error(hass, "switch", "turn_off", {"entity_id": PUMP_SWITCH}, "sync_failed")

    assert state(hass, PUMP_SWITCH).state == "on"
    assert server.posted == []


async def test_starting_works_while_a_sync_is_failing_because_nothing_is_posted(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    server.request_error = BabyTrackerConnectionError("down")

    await start(hass, init_integration)

    assert state(hass, PUMP_TIMER).state == NOW.isoformat()


async def test_a_timer_that_cannot_be_saved_still_turns_the_switch_on_and_says_so(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    real_save = Store.async_save

    async def save(self: Store[dict[str, Any]], data: dict[str, Any]) -> None:
        if self.key.endswith(".pump"):
            raise OSError("disk full")
        await real_save(self, data)

    from unittest.mock import patch

    with patch.object(Store, "async_save", save):
        await expect_error(hass, "switch", "turn_on", {"entity_id": PUMP_SWITCH}, "pump_not_saved")

    assert state(hass, PUMP_SWITCH).state == "on"


# --- saved with a Store, and surviving a reload --------------------------------------------------


async def test_a_running_timer_is_saved_and_clearing_it_is_saved_too(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    hass_storage: dict[str, Any],
    freezer: FrozenDateTimeFactory,
) -> None:
    key = f"{DOMAIN}.{init_integration.entry_id}.pump"

    await turn_on(hass, PUMP_SWITCH)
    saved = hass_storage[key]["data"]
    assert saved["started"] == NOW.isoformat()
    assert saved["object_id"]

    freezer.move_to(minutes(3))
    await turn_off(hass, PUMP_SWITCH)
    assert hass_storage[key]["data"] == {}


async def test_a_running_timer_survives_a_reload_and_syncs_every_five_minutes(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    server: FakeServer,
    freezer: FrozenDateTimeFactory,
) -> None:
    await turn_on(hass, PUMP_SWITCH)
    freezer.move_to(minutes(4))

    assert await hass.config_entries.async_reload(init_integration.entry_id)
    await hass.async_block_till_done()

    assert init_integration.state is ConfigEntryState.LOADED
    assert state(hass, PUMP_SWITCH).state == "on"
    assert state(hass, PUMP_TIMER).state == NOW.isoformat()
    assert await sync_interval(hass, server) == timedelta(minutes=5)

    freezer.move_to(minutes(13))
    await turn_off(hass, PUMP_SWITCH)
    record = only_pump(server)
    assert (record.left_duration, record.right_duration) == (13, 13)  # counted from the start
    assert record.time == NOW


async def test_a_reload_after_a_failed_stop_keeps_the_record_id_the_retry_writes(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    server: FakeServer,
    hass_storage: dict[str, Any],
    freezer: FrozenDateTimeFactory,
) -> None:
    key = f"{DOMAIN}.{init_integration.entry_id}.pump"
    await turn_on(hass, PUMP_SWITCH)
    chosen = hass_storage[key]["data"]["object_id"]
    server.post_errors.append((BabyTrackerConnectionError("down"), False))
    freezer.move_to(minutes(5))
    with pytest.raises(HomeAssistantError):
        await turn_off(hass, PUMP_SWITCH)
    assert await hass.config_entries.async_reload(init_integration.entry_id)
    await hass.async_block_till_done()

    await turn_off(hass, PUMP_SWITCH)

    record = only_pump(server)
    assert record.object_id == chosen  # fixed at the start, so a retry can't write a second pump
    assert record.left_duration == 5


async def test_an_idle_timer_stays_idle_after_a_reload(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    assert await hass.config_entries.async_reload(init_integration.entry_id)
    await hass.async_block_till_done()

    assert state(hass, PUMP_SWITCH).state == "off"
    assert state(hass, PUMP_TIMER).state == "unknown"


@pytest.mark.parametrize(
    "saved",
    [
        "not a mapping",
        {"object_id": "AB000000-0000-4000-8000-0000000000A1"},  # no start
        {"object_id": "AB000000-0000-4000-8000-0000000000A1", "started": "garbage"},
        {"object_id": "AB000000-0000-4000-8000-0000000000A1", "started": "2026-10-26T15:00:00"},
        {"started": "2026-10-26T14:50:00+00:00"},  # no record ID
        {"object_id": "", "started": "2026-10-26T14:50:00+00:00"},  # an empty record ID
        [1, 2],
    ],
)
async def test_a_saved_timer_that_cannot_be_read_is_dropped_with_a_warning(
    saved: object,
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    hass_storage: dict[str, Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    key = f"{DOMAIN}.{mock_config_entry.entry_id}.pump"
    hass_storage[key] = {"version": 1, "minor_version": 1, "key": key, "data": saved}
    mock_config_entry.add_to_hass(hass)

    with caplog.at_level(logging.WARNING):
        await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.LOADED
    assert state(hass, PUMP_SWITCH).state == "off"
    assert "pump timer" in caplog.text.lower()


async def test_a_saved_timer_is_picked_up_at_setup(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    hass_storage: dict[str, Any],
    server: FakeServer,
    freezer: FrozenDateTimeFactory,
) -> None:
    key = f"{DOMAIN}.{mock_config_entry.entry_id}.pump"
    started = NOW - timedelta(minutes=20)
    saved = {"object_id": "AB000000-0000-4000-8000-0000000000A1", "started": started.isoformat()}
    hass_storage[key] = {"version": 1, "minor_version": 1, "key": key, "data": saved}
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert state(hass, PUMP_SWITCH).state == "on"
    assert state(hass, PUMP_TIMER).state == started.isoformat()
    await turn_off(hass, PUMP_SWITCH)
    record = only_pump(server)
    assert record.object_id == "AB000000-0000-4000-8000-0000000000A1"
    assert record.left_duration == 20
    assert record.time == started


async def test_removing_the_entry_deletes_the_saved_pump_timer(
    hass: HomeAssistant, init_integration: MockConfigEntry, hass_storage: dict[str, Any]
) -> None:
    key = f"{DOMAIN}.{init_integration.entry_id}.pump"
    await turn_on(hass, PUMP_SWITCH)
    assert key in hass_storage

    await hass.config_entries.async_remove(init_integration.entry_id)
    await hass.async_block_till_done()

    assert key not in hass_storage


# --- polling -------------------------------------------------------------------------------------


async def test_an_idle_account_polls_every_five_minutes(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    assert await sync_interval(hass, server) == timedelta(minutes=5)


# --- the forgotten-timer repair ------------------------------------------------------------------


async def test_no_repair_before_an_hour_and_one_at_exactly_an_hour(
    hass: HomeAssistant, init_integration: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    await turn_on(hass, PUMP_SWITCH)

    await at(hass, freezer, 59)
    assert issue(hass, init_integration) is None
    await at(hass, freezer, 59.9)
    assert issue(hass, init_integration) is None
    await at(hass, freezer, 60)

    assert issue(hass, init_integration) is not None


async def test_the_repair_is_a_translated_not_fixable_warning(
    hass: HomeAssistant, init_integration: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    await turn_on(hass, PUMP_SWITCH)

    await at(hass, freezer, 61)

    found = issue(hass, init_integration)
    assert found.translation_key == "pump_timer_long"
    assert found.severity is ir.IssueSeverity.WARNING
    assert found.is_fixable is False


async def test_the_repair_clears_when_the_timer_stops(
    hass: HomeAssistant, init_integration: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    await turn_on(hass, PUMP_SWITCH)
    await at(hass, freezer, 61)
    assert issue(hass, init_integration) is not None

    await turn_off(hass, PUMP_SWITCH)

    assert issue(hass, init_integration) is None


async def test_the_repair_is_raised_once_and_a_dismissal_stays(
    hass: HomeAssistant, init_integration: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    await turn_on(hass, PUMP_SWITCH)
    await at(hass, freezer, 61)
    ir.async_ignore_issue(hass, DOMAIN, f"pump_timer_long_{init_integration.entry_id}", True)

    await at(hass, freezer, 80)

    assert issue(hass, init_integration).dismissed_version is not None


async def test_a_reload_with_an_overdue_timer_raises_the_repair_again_and_unload_clears_it(
    hass: HomeAssistant, init_integration: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    await turn_on(hass, PUMP_SWITCH)
    await at(hass, freezer, 61)

    await hass.config_entries.async_unload(init_integration.entry_id)
    assert issue(hass, init_integration) is None
    await hass.config_entries.async_setup(init_integration.entry_id)
    await hass.async_block_till_done()

    assert issue(hass, init_integration) is not None


async def test_the_pump_repair_is_independent_of_a_nursing_timers(
    hass: HomeAssistant, init_integration: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    await turn_on(hass, PUMP_SWITCH)
    freezer.move_to(minutes(30))
    await turn_on(hass, "switch.baby_one_nursing_left")
    await at(hass, freezer, 61)  # the pump's hour, not the nursing timer's
    assert issue(hass, init_integration) is not None
    nursing = ir.async_get(hass).async_get_issue(
        DOMAIN, f"nursing_timer_long_{init_integration.entry_id}_{_baby_one()}"
    )
    assert nursing is None

    await turn_off(hass, "switch.baby_one_nursing_left")

    assert issue(hass, init_integration) is not None  # stopping nursing leaves the pump's repair


def _baby_one() -> str:
    from .factories import BABY_ONE_ID

    return BABY_ONE_ID


async def test_a_nursing_timer_stop_does_not_clear_or_start_the_pump_timer(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    server: FakeServer,
    freezer: FrozenDateTimeFactory,
) -> None:
    await turn_on(hass, "switch.baby_one_nursing_left")
    freezer.move_to(minutes(5))
    await turn_off(hass, "switch.baby_one_nursing_left")

    assert state(hass, PUMP_SWITCH).state == "off"
    assert pump_posts(server) == []


# --- two accounts --------------------------------------------------------------------------------


async def test_each_account_has_its_own_pump_timer(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    second_entry: MockConfigEntry,
    server: FakeServer,
    second_server: FakeServer,
    freezer: FrozenDateTimeFactory,
) -> None:
    second_switch = er.async_get(hass).async_get_entity_id(
        "switch", DOMAIN, f"{second_entry.entry_id}_pump"
    )
    assert second_switch is not None
    assert second_switch != PUMP_SWITCH

    await start(hass, second_entry)

    assert state(hass, second_switch).state == "on"
    assert state(hass, PUMP_SWITCH).state == "off"
    freezer.move_to(minutes(6))
    await stop(hass, second_entry, amount=2)
    assert pump_posts(server) == []
    assert only_pump(second_server).left_duration == 6
    assert state(hass, PUMP_SWITCH).state == "off"


# --- a call through the shared helper, for the other action tests --------------------------------


async def test_the_account_helper_reaches_start_and_stop(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    server: FakeServer,
    freezer: FrozenDateTimeFactory,
) -> None:
    await call(hass, "start_pump")
    freezer.move_to(minutes(3))
    await call(hass, "stop_pump", amount=1, unit="mL")

    record = only_pump(server)
    assert record.volume == Volume(1, "ml")
    assert record.left_duration == 3


async def test_a_saved_timer_that_cannot_be_loaded_at_all_starts_with_none_and_says_so(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    real_load = Store.async_load

    async def load(self: Store[Any]) -> Any:
        if self.key.endswith(".pump"):
            raise HomeAssistantError("corrupt file")
        return await real_load(self)

    from unittest.mock import patch

    mock_config_entry.add_to_hass(hass)
    with patch.object(Store, "async_load", load), caplog.at_level(logging.WARNING):
        await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.LOADED
    assert state(hass, PUMP_SWITCH).state == "off"
    assert "saved pump timer couldn't be read" in caplog.text


async def test_a_zero_amount_pump_from_the_app_shows_no_volume_and_adds_to_no_total(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    from .factories import DEVICE_A, at, volume

    server.add_pump(
        DEVICE_A,
        "F0000000-0000-4000-8000-0000000000A9",
        when=at(30),
        amount=volume(0),
        leftDuration=9,
        rightDuration=9,
        sides=0,
    )
    from .helpers import sync

    await sync(hass, init_integration)

    last = state(hass, LAST_PUMP)
    assert last.state != "unknown"
    assert last.attributes["left_duration_minutes"] == 9
    assert last.attributes["amount"] is None
    assert last.attributes["unit"] is None
    assert state(hass, "sensor.baby_one_bottle_volume_today").state == "0.0"
    assert state(hass, "sensor.baby_one_feeds_today").state == "0"


async def test_a_pump_with_a_real_amount_still_shows_it(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    from .factories import DEVICE_A, at, volume
    from .helpers import sync

    server.add_pump(
        DEVICE_A,
        "F0000000-0000-4000-8000-0000000000AA",
        when=at(30),
        amount=volume(120, english=False),
    )
    await sync(hass, init_integration)

    assert state(hass, LAST_PUMP).attributes["amount"] == 120


async def test_stop_pump_with_no_amount_and_an_explicit_ml_writes_a_zero_in_ml(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    server: FakeServer,
    freezer: FrozenDateTimeFactory,
) -> None:
    await start(hass, init_integration)
    freezer.move_to(minutes(5))

    await stop(hass, init_integration, unit="mL")

    record = only_pump(server)
    assert record.to_payload()["amount"]["englishMeasure"] == "false"
    assert record.volume == Volume(0, "ml")


async def test_stop_pump_with_no_amount_and_an_explicit_fl_oz_writes_a_zero_in_ounces(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    server: FakeServer,
    freezer: FrozenDateTimeFactory,
) -> None:
    await start(hass, init_integration)
    freezer.move_to(minutes(5))

    await stop(hass, init_integration, unit="fl. oz.")

    assert only_pump(server).volume == Volume(0, "oz")


async def test_with_no_amount_and_no_unit_the_zero_follows_the_bottle_unit_the_account_uses(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    server: FakeServer,
    freezer: FrozenDateTimeFactory,
) -> None:
    from .helpers import set_options

    cases = [
        (US_CUSTOMARY_SYSTEM, {}, Volume(0, "oz")),
        (METRIC_SYSTEM, {}, Volume(0, "ml")),
        (US_CUSTOMARY_SYSTEM, {"bottle_unit": "mL"}, Volume(0, "ml")),
    ]
    for index, (units, options, expected) in enumerate(cases):
        hass.config.units = units
        if options:
            await set_options(
                hass, init_integration, bottle_size=120, bottle_type="formula", **options
            )
        server.posted.clear()
        await start(hass, init_integration)
        freezer.move_to(minutes(5 + index * 10))
        await stop(hass, init_integration)
        assert only_pump(server).volume == expected, (units, options)
