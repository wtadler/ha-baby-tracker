"""Core PR 7: nursing minutes today, and the last breast side the app shows."""

from typing import Any

import pybabytracker
import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from . import BABY_TWO, NOW, tick
from .factories import DEVICE_A, at
from .fake_server import FakeServer

MINUTES = "sensor.baby_one_nursing_minutes_today"
# Minutes from the factories' base time (the day before) to an hour before NOW, so today.
TODAY = int((NOW - at(0)).total_seconds() // 60) - 60
LAST_BREAST = "sensor.baby_tracker_account_last_breast"
NEEDS_SESSIONS = pytest.mark.skipif(
    not hasattr(pybabytracker, "NursingSession"), reason="needs a pybabytracker that decodes it"
)


@pytest.fixture(autouse=True)
def _start_at_now(freezer: FrozenDateTimeFactory) -> None:
    """Freeze the clock at NOW before anything is set up."""
    freezer.move_to(NOW)


def _nursing(
    server: FakeServer, number: int, minute: float, kind: str = "Nursing", **fields: Any
) -> None:
    sides = {"leftDuration": 5, "rightDuration": 7, "bothDuration": 0, "finishSide": 1}
    server.add_record(
        DEVICE_A, kind, f"N0000000-0000-4000-8000-{number:012d}", when=at(minute), **sides | fields
    )


def _pump(server: FakeServer, number: int, minute: float, side: int, **fields: Any) -> None:
    payload_fields = {
        "finishSide": side,
        "amount": {"BCObjectType": "VolumeMeasure", "value": 2, "englishMeasure": "true"},
    }
    server.add_pump(
        DEVICE_A,
        f"P0000000-0000-4000-8000-{number:012d}",
        when=at(minute),
        **payload_fields | fields,
    )


def _state(hass: HomeAssistant, entity_id: str) -> Any:
    state = hass.states.get(entity_id)
    assert state is not None, f"{entity_id} does not exist"
    return state


async def test_nursing_minutes_today_add_up_the_days_sessions(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    _nursing(server, 1, TODAY)
    _nursing(server, 2, TODAY + 10, bothDuration=3)
    _nursing(server, 3, 30)  # yesterday: not counted
    await tick(hass)

    state = _state(hass, MINUTES)
    assert state.state == str(12 + 15)
    assert state.attributes["nursings"] == 2
    assert state.attributes["unit_of_measurement"] == "min"
    assert state.attributes["device_class"] == "duration"


@NEEDS_SESSIONS
async def test_an_android_timer_session_counts_toward_nursing_minutes(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    _nursing(server, 1, TODAY, kind="NursingSession", detailed=True)
    await tick(hass)

    assert _state(hass, MINUTES).state == "12"


async def test_the_last_breast_is_the_newest_nursing_of_any_baby(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    assert _state(hass, LAST_BREAST).state == "unknown"
    _nursing(server, 1, 30, finishSide=1)
    _nursing(server, 2, 60, finishSide=2, baby=BABY_TWO)
    await tick(hass)

    state = _state(hass, LAST_BREAST)
    assert state.state == "right"
    assert state.attributes["kind"] == "nursing"
    assert state.attributes["time"] == at(60).isoformat()


async def test_a_newer_pump_is_the_last_breast(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    _nursing(server, 1, 30, finishSide=2)
    _pump(server, 2, 60, side=1)
    await tick(hass)

    state = _state(hass, LAST_BREAST)
    assert (state.state, state.attributes["kind"]) == ("left", "pump")


async def test_a_session_without_a_side_is_none(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    _nursing(server, 1, 30, finishSide=0)
    await tick(hass)

    assert _state(hass, LAST_BREAST).state == "none"
