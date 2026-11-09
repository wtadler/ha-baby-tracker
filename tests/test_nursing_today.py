"""Core PR 7: nursing minutes today, and the last breast side the app shows."""

from typing import Any

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pybabytracker import BabyTrackerConnectionError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from . import BABY_TWO, NOW, tick
from .factories import DEVICE_A, OP_DELETE, at
from .fake_server import FakeServer

MINUTES = "sensor.baby_one_nursing_minutes_today"
# Minutes from the factories' base time (the day before) to an hour before NOW, so today.
TODAY = int((NOW - at(0)).total_seconds() // 60) - 60
LAST_BREAST = "sensor.baby_tracker_account_last_breast"


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


async def test_both_new_sensors_start_enabled(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    registry = er.async_get(hass)
    for entity_id in (MINUTES, LAST_BREAST):
        entry = registry.async_get(entity_id)
        assert entry is not None
        assert entry.disabled_by is None


async def test_a_session_that_started_left_and_ended_right_reads_left(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    """Test the state is the stored finishSide, the side the session started on."""
    _nursing(server, 1, 30, leftDuration=5, rightDuration=7, finishSide=1)
    await tick(hass)

    assert _state(hass, LAST_BREAST).state == "left"


async def test_a_session_that_started_right_and_ended_left_reads_right(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    _nursing(server, 1, 30, leftDuration=7, rightDuration=5, finishSide=2)
    await tick(hass)

    assert _state(hass, LAST_BREAST).state == "right"


async def test_an_android_session_reads_its_side_from_a_digit_string(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    _nursing(server, 1, 30, kind="NursingSession", detailed=True, finishSide="2")
    await tick(hass)

    assert _state(hass, LAST_BREAST).state == "right"


@pytest.mark.parametrize("side", [3, -1, "", " ", "left", 1.5, True, None, [], "٢"])
async def test_a_side_that_is_not_left_or_right_reads_none(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, side: object
) -> None:
    _nursing(server, 1, 30, finishSide=side)
    await tick(hass)

    state = _state(hass, LAST_BREAST)
    assert state.state == "none"
    assert state.attributes["kind"] == "nursing"


async def test_a_session_with_no_side_key_reads_none(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    server.add_record(DEVICE_A, "Nursing", "N0000000-0000-4000-8000-000000000001", when=at(30))
    await tick(hass)

    assert _state(hass, LAST_BREAST).state == "none"


async def test_a_pump_that_is_the_newest_gives_its_side_even_after_a_two_sided_nursing(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    _nursing(server, 1, 30, leftDuration=5, rightDuration=7, finishSide=1)
    _pump(server, 2, 60, side=2)
    await tick(hass)

    state = _state(hass, LAST_BREAST)
    assert (state.state, state.attributes["kind"]) == ("right", "pump")
    assert state.attributes["time"] == at(60).isoformat()


async def test_a_pump_and_a_nursing_at_the_same_moment_give_the_pump(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    _nursing(server, 1, 60, finishSide=1)
    _pump(server, 2, 60, side=2)
    await tick(hass)

    state = _state(hass, LAST_BREAST)
    assert (state.state, state.attributes["kind"]) == ("right", "pump")


async def test_a_milk_stash_adjustment_is_not_a_last_breast(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    _nursing(server, 1, 30, finishSide=1)
    _pump(server, 2, 60, side=2, label="Adjustment")
    await tick(hass)

    state = _state(hass, LAST_BREAST)
    assert (state.state, state.attributes["kind"]) == ("left", "nursing")


async def test_a_pump_with_no_side_key_reads_none(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    server.add_pump(DEVICE_A, "P0000000-0000-4000-8000-000000000001", when=at(30))
    await tick(hass)

    state = _state(hass, LAST_BREAST)
    assert (state.state, state.attributes["kind"]) == ("none", "pump")


async def test_a_record_dated_after_now_is_ignored(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    _nursing(server, 1, 30, finishSide=1)
    _nursing(server, 2, (NOW - at(0)).total_seconds() / 60 + 600, finishSide=2)
    await tick(hass)

    assert _state(hass, LAST_BREAST).state == "left"


async def test_deleting_the_newest_session_hands_the_sensor_back(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    _nursing(server, 1, 30, finishSide=1)
    _nursing(server, 2, 60, finishSide=2)
    await tick(hass)
    assert _state(hass, LAST_BREAST).state == "right"

    server.add_record(
        DEVICE_A, "Nursing", "N0000000-0000-4000-8000-000000000002", when=at(60), op=OP_DELETE
    )
    await tick(hass)
    assert _state(hass, LAST_BREAST).state == "left"


@pytest.mark.parametrize("entity_id", [MINUTES, LAST_BREAST])
async def test_the_sensors_are_unavailable_when_the_sync_fails(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, entity_id: str
) -> None:
    server.request_error = BabyTrackerConnectionError("down")
    await tick(hass)

    assert _state(hass, entity_id).state == "unavailable"


@pytest.mark.parametrize(
    ("left", "right", "both", "minutes"),
    [
        (-5, 7, 0, 7),  # a negative side adds nothing
        (5.0, 7, 0, 7),  # a float adds nothing
        ("4", "6", "0", 10),  # Android sends digit strings
        ("", " ", None, 0),  # empty, whitespace-only, and null add nothing
        (True, [], {}, 0),  # wrong types add nothing
        (5, 7, 3, 15),  # both-sides time is added on top
    ],
)
async def test_malformed_minutes_add_what_the_library_adds(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    server: FakeServer,
    left: object,
    right: object,
    both: object,
    minutes: int,
) -> None:
    _nursing(server, 1, TODAY, leftDuration=left, rightDuration=right, bothDuration=both)
    await tick(hass)

    state = _state(hass, MINUTES)
    assert state.state == str(minutes)
    assert state.attributes["nursings"] == 1


async def test_a_session_with_no_duration_keys_counts_as_a_nursing_of_no_minutes(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    server.add_record(DEVICE_A, "Nursing", "N0000000-0000-4000-8000-000000000001", when=at(TODAY))
    await tick(hass)

    state = _state(hass, MINUTES)
    assert (state.state, state.attributes["nursings"]) == ("0", 1)


async def test_nursing_minutes_are_zero_before_any_nursing(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    state = _state(hass, MINUTES)
    assert (state.state, state.attributes["nursings"]) == ("0", 0)


async def test_nursing_minutes_are_per_baby(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    _nursing(server, 1, TODAY)
    _nursing(server, 2, TODAY, baby=BABY_TWO, leftDuration=3, rightDuration=0)
    await tick(hass)

    assert _state(hass, MINUTES).state == "12"
    assert _state(hass, "sensor.baby_two_nursing_minutes_today").state == "3"
