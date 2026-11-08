"""Core PR 26: the last medicine and next medicine dose sensors, and the log_medicine action."""

from datetime import timedelta
from typing import Any

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from pybabytracker import Medication
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.baby_tracker.const import DOMAIN

from . import NOW, tick
from .factories import BABY_ONE_ID, DEVICE_A, at
from .fake_server import FakeServer
from .helpers import device_id, posted

LAST = "sensor.baby_one_last_medicine"
NEXT = "sensor.baby_one_next_medicine_dose"
# A medicine as an iPhone embedded it in a dose, with invented values.
DROPS = {
    "name": "Test Drops",
    "BCObjectType": "MedicationSelection",
    "unit": "ml",
    "interval": 240,
    "isPresciption": "false",
    "newFlage": "false",
    "timestamp": "1970-01-01 00:00:00 +0000",
    "amountPerTime": 3.75,
    "objectID": "M0000000-0000-4000-8000-000000000001",
}


@pytest.fixture(autouse=True)
def _start_at_now(freezer: FrozenDateTimeFactory) -> None:
    """Freeze the clock at NOW before anything is set up."""
    freezer.move_to(NOW)


def _dose(server: FakeServer, number: int, minute: float, medicine: dict[str, Any]) -> None:
    server.add_record(
        DEVICE_A,
        "Medication",
        f"D0000000-0000-4000-8000-{number:012d}",
        when=at(minute),
        amount=medicine.get("amountPerTime", 1),
        medicationSelection=medicine,
    )


async def _log(hass: HomeAssistant, medicine: str, **extra: Any) -> None:
    data = {"device_id": device_id(hass, BABY_ONE_ID), "medicine": medicine, **extra}
    await hass.services.async_call(DOMAIN, "log_medicine", data, blocking=True)


async def test_the_last_dose_and_when_the_next_is_due_are_shown(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    _dose(server, 1, 30, DROPS)
    await tick(hass)

    state = hass.states.get(LAST)
    assert state is not None
    assert state.state == "Test Drops"
    assert state.attributes["amount"] == 3.75
    assert state.attributes["unit"] == "ml"
    assert state.attributes["time"] == at(30).isoformat()
    due = hass.states.get(NEXT)
    assert due is not None
    assert due.state == (at(30) + timedelta(minutes=240)).isoformat()
    assert due.attributes["medicine"] == "Test Drops"


async def test_a_medicine_without_an_interval_has_no_next_dose(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    _dose(server, 1, 30, {**DROPS, "interval": 0})
    await tick(hass)

    due = hass.states.get(NEXT)
    assert due is not None
    assert due.state == "unknown"


async def test_log_medicine_gives_the_usual_dose_by_default(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    _dose(server, 1, 30, DROPS)
    await tick(hass)

    await _log(hass, "test drops")

    [(_, _, record)] = posted(server)
    assert isinstance(record, Medication)
    assert record.amount == 3.75
    assert record.medication_selection == DROPS
    assert record.note == "Added from Home Assistant"
    state = hass.states.get(LAST)
    assert state is not None
    assert state.attributes["time"] == NOW.isoformat()


async def test_log_medicine_with_an_amount(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    _dose(server, 1, 30, DROPS)
    await tick(hass)

    await _log(hass, "Test Drops", amount=2.5)

    [(_, _, record)] = posted(server)
    assert record.amount == 2.5


async def test_an_unknown_medicine_is_refused_by_name_with_nothing_posted(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    with pytest.raises(ServiceValidationError) as raised:
        await _log(hass, "Tylenol")

    assert raised.value.translation_key == "unknown_medicine"
    assert raised.value.translation_placeholders == {"medicine": "Tylenol"}
    assert server.posted == []


async def test_a_medicine_without_a_usual_dose_needs_an_amount(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    _dose(server, 1, 30, {k: v for k, v in DROPS.items() if k != "amountPerTime"})
    await tick(hass)

    with pytest.raises(ServiceValidationError) as raised:
        await _log(hass, "Test Drops")
    assert raised.value.translation_key == "medicine_needs_amount"
    with pytest.raises(ServiceValidationError) as raised:
        await _log(hass, "Test Drops", amount=0)
    assert raised.value.translation_key == "invalid_amount"
    assert server.posted == []
