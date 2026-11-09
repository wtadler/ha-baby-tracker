"""Core PR 26: the last medicine and next medicine dose sensors, and the log_medicine action."""

import asyncio
import math
from datetime import timedelta
from typing import Any
from unittest.mock import patch

import probatio
import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from pybabytracker import BabyTrackerConnectionError, Medication
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.baby_tracker.const import DOMAIN

from . import BABY_TWO, NOW, tick
from .factories import BABY_ONE_ID, DEVICE_A, at
from .fake_server import FakeServer
from .helpers import device_id, posted

LAST = "sensor.baby_one_last_medicine"
NEXT = "sensor.baby_one_next_medicine_dose"
DROPS_ID = "M0000000-0000-4000-8000-000000000001"
SYRUP_ID = "M0000000-0000-4000-8000-000000000002"
# A medicine as a phone embeds it in a dose, with invented values. The interval is in minutes.
DROPS = {
    "name": "Test Drops",
    "BCObjectType": "MedicationSelection",
    "unit": "ml",
    "interval": 360,
    "isPresciption": "false",
    "newFlage": "false",
    "timestamp": "1970-01-01 00:00:00 +0000",
    "amountPerTime": 5,
    "objectID": DROPS_ID,
}
SYRUP = {**DROPS, "name": "Test Syrup", "interval": 240, "amountPerTime": 10, "objectID": SYRUP_ID}


@pytest.fixture(autouse=True)
def _start_at_now(freezer: FrozenDateTimeFactory) -> None:
    """Freeze the clock at NOW before anything is set up."""
    freezer.move_to(NOW)


def _dose(server: FakeServer, number: int, minute: float, medicine: Any, **fields: Any) -> None:
    server.add_record(
        DEVICE_A,
        "Medication",
        f"D0000000-0000-4000-8000-{number:012d}",
        when=at(minute),
        amount=fields.pop("amount", 1),
        medicationSelection=medicine,
        **fields,
    )


async def _log(hass: HomeAssistant, medicine: Any, **extra: Any) -> None:
    data = {"device_id": device_id(hass, BABY_ONE_ID), "medicine": medicine, **extra}
    await hass.services.async_call(DOMAIN, "log_medicine", data, blocking=True)


def _next(hass: HomeAssistant) -> Any:
    state = hass.states.get(NEXT)
    assert state is not None
    return state


async def test_the_last_dose_and_when_the_next_is_due_are_shown(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    _dose(server, 1, 30, DROPS, amount=5)
    await tick(hass)

    state = hass.states.get(LAST)
    assert state is not None
    assert state.state == "Test Drops"
    assert state.attributes["amount"] == 5
    assert state.attributes["unit"] == "ml"
    assert state.attributes["medicine_id"] == DROPS_ID
    assert state.attributes["time"] == at(30).isoformat()
    due = _next(hass)
    assert due.state == (at(30) + timedelta(minutes=360)).isoformat()
    assert due.attributes["medicine"] == "Test Drops"
    assert due.attributes["medicines"] == {
        "Test Drops": (at(30) + timedelta(minutes=360)).isoformat()
    }


async def test_a_baby_without_doses_has_unknown_medicine_sensors(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    state = hass.states.get(LAST)
    assert state is not None
    assert state.state == "unknown"
    assert _next(hass).state == "unknown"
    assert _next(hass).attributes["medicine"] is None
    assert _next(hass).attributes["medicines"] == {}


async def test_two_alternating_medicines_each_have_their_own_next_dose(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    """Test the state follows the newest dose, and the attributes keep every medicine's time."""
    _dose(server, 1, 0, DROPS)
    _dose(server, 2, 60, SYRUP)
    await tick(hass)

    drops_next = (at(0) + timedelta(minutes=360)).isoformat()
    syrup_next = (at(60) + timedelta(minutes=240)).isoformat()
    due = _next(hass)
    assert due.state == syrup_next
    assert due.attributes["medicine"] == "Test Syrup"
    assert due.attributes["medicines"] == {"Test Drops": drops_next, "Test Syrup": syrup_next}

    _dose(server, 3, 120, DROPS)
    await tick(hass)

    drops_next = (at(120) + timedelta(minutes=360)).isoformat()
    due = _next(hass)
    assert due.state == drops_next
    assert due.attributes["medicine"] == "Test Drops"
    assert due.attributes["medicines"] == {"Test Drops": drops_next, "Test Syrup": syrup_next}


async def test_only_a_medicines_newest_dose_counts(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    _dose(server, 1, 90, DROPS)
    _dose(server, 2, 30, DROPS)
    await tick(hass)

    assert _next(hass).attributes["medicines"] == {
        "Test Drops": (at(90) + timedelta(minutes=360)).isoformat()
    }


async def test_a_medicine_without_an_interval_is_listed_with_no_next_dose(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    no_interval = {k: v for k, v in SYRUP.items() if k != "interval"}
    _dose(server, 1, 0, DROPS)
    _dose(server, 2, 60, no_interval)
    await tick(hass)

    due = _next(hass)
    assert due.state == "unknown"
    assert due.attributes["medicine"] == "Test Syrup"
    assert due.attributes["medicines"] == {
        "Test Drops": (at(0) + timedelta(minutes=360)).isoformat(),
        "Test Syrup": None,
    }


@pytest.mark.parametrize(
    "interval",
    [
        0,
        -5,
        True,
        False,
        None,
        "240",
        "",
        [240],
        {"a": 1},
        math.nan,
        math.inf,
        -math.inf,
        1e300,
        10**9,
        525601,
    ],
    ids=repr,
)
async def test_an_interval_that_is_not_a_usable_number_has_no_next_dose(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, interval: Any
) -> None:
    _dose(server, 1, 30, {**DROPS, "interval": interval})
    await tick(hass)

    due = _next(hass)
    assert due.state == "unknown"
    assert due.attributes["medicines"] == {"Test Drops": None}
    last = hass.states.get(LAST)
    assert last is not None
    assert last.state == "Test Drops"


async def test_the_longest_interval_is_a_year(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    _dose(server, 1, 30, {**DROPS, "interval": 525600})
    await tick(hass)

    assert _next(hass).state == (at(30) + timedelta(minutes=525600)).isoformat()


async def test_a_fractional_interval_is_in_minutes(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    _dose(server, 1, 30, {**DROPS, "interval": 90.5})
    await tick(hass)

    assert _next(hass).state == (at(30) + timedelta(minutes=90.5)).isoformat()


@pytest.mark.parametrize("selection", [None, "Test Drops", ["x"], 5, True, {}, {"name": None}])
async def test_a_dose_without_a_usable_medicine_has_no_name_or_next_dose(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, selection: Any
) -> None:
    _dose(server, 1, 30, selection)
    await tick(hass)

    last = hass.states.get(LAST)
    assert last is not None
    assert last.state == "unknown"
    assert last.attributes["unit"] is None
    assert last.attributes["medicine_id"] is None
    assert _next(hass).state == "unknown"
    assert _next(hass).attributes["medicines"] == {}


async def test_a_dose_with_no_medicine_key_is_skipped(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    server.add_record(
        DEVICE_A, "Medication", "D0000000-0000-4000-8000-0000000000FF", when=at(30), amount=1
    )
    await tick(hass)

    assert _next(hass).attributes["medicines"] == {}


@pytest.mark.parametrize("amount", ["5", None, True, ["x"], {"a": 1}])
async def test_a_dose_amount_that_is_not_a_number_reads_as_no_amount(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, amount: Any
) -> None:
    _dose(server, 1, 30, DROPS, amount=amount)
    await tick(hass)

    last = hass.states.get(LAST)
    assert last is not None
    assert last.attributes["amount"] is None


async def test_two_medicines_with_one_name_are_listed_by_name_and_id(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    _dose(server, 1, 0, DROPS)
    _dose(server, 2, 10, {**SYRUP, "name": "Test Drops"})
    await tick(hass)

    medicines = _next(hass).attributes["medicines"]
    assert set(medicines) == {f"Test Drops ({DROPS_ID})", f"Test Drops ({SYRUP_ID})"}


async def test_another_babys_doses_are_not_counted(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    _dose(server, 1, 0, DROPS, baby=BABY_TWO)
    await tick(hass)

    assert _next(hass).attributes["medicines"] == {}
    two = hass.states.get("sensor.baby_two_next_medicine_dose")
    assert two is not None
    assert two.attributes["medicines"] == {
        "Test Drops": (at(0) + timedelta(minutes=360)).isoformat()
    }


async def test_log_medicine_gives_the_usual_dose_by_default(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    _dose(server, 1, 30, DROPS)
    await tick(hass)

    await _log(hass, "test drops")

    [(_, _, record)] = posted(server)
    assert isinstance(record, Medication)
    assert record.amount == 5
    assert record.medication_selection == DROPS
    assert record.note == "Added from Home Assistant"
    assert record.time == NOW
    state = hass.states.get(LAST)
    assert state is not None
    assert state.attributes["time"] == NOW.isoformat()


async def test_log_medicine_with_an_amount_and_a_note(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    _dose(server, 1, 30, DROPS)
    await tick(hass)

    await _log(hass, "Test Drops", amount=2.5, note="After lunch")

    [(_, _, record)] = posted(server)
    assert record.amount == 2.5
    assert record.note == "After lunch"


@pytest.mark.parametrize("note", ["", " ", "   ", "\t\n"], ids=repr)
async def test_log_medicine_with_a_blank_note_says_it_came_from_home_assistant(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, note: str
) -> None:
    _dose(server, 1, 30, DROPS)
    await tick(hass)

    await _log(hass, "Test Drops", note=note)

    [(_, _, record)] = posted(server)
    assert record.note == "Added from Home Assistant"


async def test_log_medicine_at_a_given_time(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    _dose(server, 1, 30, DROPS)
    await tick(hass)

    await _log(hass, "Test Drops", time="2026-10-26T12:00:00+00:00")

    [(_, _, record)] = posted(server)
    assert record.time is not None
    assert record.time.isoformat() == "2026-10-26T12:00:00+00:00"


async def test_log_medicine_by_the_medicines_id(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    _dose(server, 1, 30, DROPS)
    await tick(hass)

    await _log(hass, DROPS_ID)

    [(_, _, record)] = posted(server)
    assert record.medication_selection == DROPS


async def test_a_medicine_first_dosed_on_a_phone_since_the_last_sync_can_be_logged(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    """Test the write's own sync finds it, so the stale state doesn't refuse it."""
    _dose(server, 1, 30, DROPS)

    await _log(hass, "Test Drops")

    [(_, _, record)] = posted(server)
    assert record.medication_selection == DROPS


async def test_an_unknown_medicine_is_refused_by_name_with_nothing_posted(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    with pytest.raises(ServiceValidationError) as raised:
        await _log(hass, "Test Elixir")

    assert raised.value.translation_key == "unknown_medicine"
    assert raised.value.translation_placeholders == {"medicine": "Test Elixir"}
    assert server.posted == []


@pytest.mark.parametrize("name", ["", " ", "   ", "\t"], ids=repr)
async def test_a_blank_medicine_name_is_a_translated_error(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, name: str
) -> None:
    with pytest.raises(ServiceValidationError) as raised:
        await _log(hass, name)

    assert raised.value.translation_key == "invalid_selection_name"
    assert server.posted == []


@pytest.mark.parametrize("name", [5, True, 1.5], ids=repr)
async def test_a_medicine_name_of_another_type_is_read_as_text(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, name: Any
) -> None:
    with pytest.raises(ServiceValidationError) as raised:
        await _log(hass, name)

    assert raised.value.translation_key == "unknown_medicine"
    assert server.posted == []


@pytest.mark.parametrize("name", [None, ["Test Drops"], {"a": 1}], ids=repr)
async def test_a_medicine_name_that_is_not_text_is_a_schema_error(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, name: Any
) -> None:
    with pytest.raises(probatio.Invalid):
        await _log(hass, name)
    assert server.posted == []


async def test_a_missing_medicine_is_a_schema_error(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    with pytest.raises(probatio.Invalid):
        await hass.services.async_call(
            DOMAIN, "log_medicine", {"device_id": device_id(hass, BABY_ONE_ID)}, blocking=True
        )
    assert server.posted == []


async def test_two_medicines_with_one_name_are_refused_until_picked_by_id(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    _dose(server, 1, 0, DROPS)
    _dose(server, 2, 10, {**SYRUP, "name": "Test Drops"})
    await tick(hass)

    with pytest.raises(ServiceValidationError) as raised:
        await _log(hass, "Test Drops")

    assert raised.value.translation_key == "ambiguous_selection"
    placeholders = raised.value.translation_placeholders
    assert placeholders is not None
    assert placeholders["medicine"] == "Test Drops"
    assert DROPS_ID in placeholders["ids"]
    assert SYRUP_ID in placeholders["ids"]
    assert server.posted == []

    await _log(hass, SYRUP_ID)
    [(_, _, record)] = posted(server)
    assert record.medication_selection is not None
    assert record.medication_selection["objectID"] == SYRUP_ID


async def test_a_medicine_without_a_usual_dose_needs_an_amount(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    _dose(server, 1, 30, {k: v for k, v in DROPS.items() if k != "amountPerTime"})
    await tick(hass)

    with pytest.raises(ServiceValidationError) as raised:
        await _log(hass, "Test Drops")
    assert raised.value.translation_key == "no_usual_dose"
    assert server.posted == []

    await _log(hass, "Test Drops", amount=3)
    [(_, _, record)] = posted(server)
    assert record.amount == 3


@pytest.mark.parametrize("usual", [None, 0, -1, True, "5", math.nan], ids=repr)
async def test_a_usual_dose_that_is_not_a_positive_number_needs_an_amount(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, usual: Any
) -> None:
    _dose(server, 1, 30, {**DROPS, "amountPerTime": usual})
    await tick(hass)

    with pytest.raises(ServiceValidationError) as raised:
        await _log(hass, "Test Drops")
    assert raised.value.translation_key == "no_usual_dose"
    assert server.posted == []


@pytest.mark.parametrize("amount", [0, -1, 0.001, math.nan, math.inf, -math.inf], ids=repr)
async def test_an_amount_that_is_not_a_positive_number_is_a_translated_error(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, amount: float
) -> None:
    _dose(server, 1, 30, DROPS)
    await tick(hass)

    with pytest.raises(ServiceValidationError) as raised:
        await _log(hass, "Test Drops", amount=amount)

    assert raised.value.translation_key == "invalid_amount"
    assert server.posted == []


@pytest.mark.parametrize("amount", [True, False, "abc", "", [1], {"a": 1}], ids=repr)
async def test_an_amount_of_the_wrong_type_is_a_schema_error(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, amount: Any
) -> None:
    _dose(server, 1, 30, DROPS)
    await tick(hass)

    with pytest.raises(probatio.Invalid):
        await _log(hass, "Test Drops", amount=amount)
    assert server.posted == []


async def test_an_amount_given_as_text_is_read_as_a_number(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    _dose(server, 1, 30, DROPS)
    await tick(hass)

    await _log(hass, "Test Drops", amount="2.5")

    [(_, _, record)] = posted(server)
    assert record.amount == 2.5


async def test_a_sync_that_fails_before_the_write_posts_nothing(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    _dose(server, 1, 30, DROPS)
    await tick(hass)
    server.request_error = BabyTrackerConnectionError("down")

    with pytest.raises(HomeAssistantError) as raised:
        await _log(hass, "Test Drops")

    assert raised.value.translation_key == "sync_failed"
    assert server.posted == []


async def test_two_calls_with_one_record_id_post_once(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    _dose(server, 1, 30, DROPS)
    await tick(hass)
    record_id = "6F1C2A3E-0000-4000-8000-000000000002"

    await asyncio.gather(
        _log(hass, "Test Drops", object_id=record_id),
        _log(hass, "Test Drops", object_id=record_id),
    )

    [(_, _, record)] = posted(server)
    assert record.object_id == record_id


async def test_two_calls_without_a_record_id_post_twice_one_after_the_other(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    _dose(server, 1, 30, DROPS)
    await tick(hass)

    await asyncio.gather(_log(hass, "Test Drops"), _log(hass, "Test Drops"))

    assert len(posted(server)) == 2
    assert server.max_active_posts == 1


async def test_a_record_id_that_is_not_a_uuid_is_a_schema_error(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    _dose(server, 1, 30, DROPS)
    await tick(hass)

    with pytest.raises(probatio.Invalid):
        await _log(hass, "Test Drops", object_id="not-a-uuid")
    assert server.posted == []


async def test_an_account_device_is_refused(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    _dose(server, 1, 30, DROPS)
    await tick(hass)

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            "log_medicine",
            {"device_id": "no-such-device", "medicine": "Test Drops"},
            blocking=True,
        )
    assert server.posted == []


async def test_a_record_id_taken_by_another_record_is_refused_not_called_a_bad_amount(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    _dose(server, 1, 30, DROPS)
    await tick(hass)
    taken = "6F1C2A3E-0000-4000-8000-0000000000AA"
    await hass.services.async_call(
        DOMAIN,
        "log_diaper",
        {"device_id": device_id(hass, BABY_ONE_ID), "type": "wet", "object_id": taken},
        blocking=True,
    )
    server.posted.clear()

    with pytest.raises(ServiceValidationError) as raised:
        await _log(hass, "Test Drops", object_id=taken)

    assert raised.value.translation_key == "object_id_in_use"
    assert server.posted == []


async def test_a_value_error_the_library_raises_is_a_write_error_not_a_bad_amount(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    """Test a refusal unrelated to the amount isn't mislabelled as one."""
    _dose(server, 1, 30, DROPS)
    await tick(hass)

    with (
        patch.object(
            init_integration.runtime_data.engine.__class__,
            "add_medication",
            side_effect=ValueError("no device ID"),
        ),
        pytest.raises(HomeAssistantError) as raised,
    ):
        await _log(hass, "Test Drops")

    assert not isinstance(raised.value, ServiceValidationError)
    assert raised.value.translation_key == "write_failed"
    assert server.posted == []


async def test_medicines_that_differ_only_in_case_are_both_listed_with_their_ids(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    _dose(server, 1, 0, {**DROPS, "name": "Drops"})
    _dose(server, 2, 10, {**SYRUP, "name": " drops "})
    await tick(hass)

    medicines = _next(hass).attributes["medicines"]
    assert len(medicines) == 2
    assert {label.rsplit("(", 1)[1] for label in medicines} == {f"{DROPS_ID})", f"{SYRUP_ID})"}
