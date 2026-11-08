"""Core PR 4: the last nursing sensors (time, total and side minutes) and last sleep duration."""

import pybabytracker
import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from . import NOW, setup_integration, tick
from .factories import DEVICE_A, OP_UPDATE, at, volume
from .fake_server import FakeServer

LAST_NURSING = "sensor.baby_one_last_nursing"
DURATION = "sensor.baby_one_last_nursing_duration"
LEFT = "sensor.baby_one_last_nursing_left_time"
RIGHT = "sensor.baby_one_last_nursing_right_time"
SLEEP = "sensor.baby_one_last_sleep_duration"


@pytest.fixture(autouse=True)
def _start_at_now(freezer: FrozenDateTimeFactory) -> None:
    """Freeze the clock at NOW before anything is set up."""
    freezer.move_to(NOW)


def _value(hass: HomeAssistant, entity_id: str) -> str:
    state = hass.states.get(entity_id)
    assert state is not None, f"{entity_id} does not exist"
    return state.state


def _nursing(server: FakeServer, number: int, minute: int, **sides: object) -> None:
    server.add_record(
        DEVICE_A, "Nursing", f"N0000000-0000-4000-8000-{number:012d}", when=at(minute), **sides
    )


async def test_the_sensors_show_the_newest_nursing(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    populated_server: FakeServer,
) -> None:
    """Test the time and minutes come from the newest nursing."""
    await setup_integration(hass, mock_config_entry)

    assert _value(hass, LAST_NURSING) == at(40).isoformat()
    assert _value(hass, DURATION) == "19"
    assert _value(hass, LEFT) == "7"
    assert _value(hass, RIGHT) == "12"
    state = hass.states.get(DURATION)
    assert state is not None
    assert state.attributes["device_class"] == "duration"
    assert state.attributes["unit_of_measurement"] == "min"


async def test_a_bottle_after_the_nursing_does_not_hide_it(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    populated_server: FakeServer,
) -> None:
    """Test the sensors keep the nursing when a later feed is a bottle."""
    await setup_integration(hass, mock_config_entry)
    # populated_server's pumped bottle at minute 60 is already newer than the nursing at 40.
    assert _value(hass, "sensor.baby_one_last_feed") == at(60).isoformat()
    assert _value(hass, LAST_NURSING) == at(40).isoformat()
    assert _value(hass, LEFT) == "7"


async def test_a_newer_nursing_replaces_the_readings(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    """Test a nursing that syncs later moves every sensor to it."""
    _nursing(server, 1, 30, leftDuration=4, rightDuration=0, bothDuration=0)
    await tick(hass)
    assert _value(hass, DURATION) == "4"

    _nursing(server, 2, 90, leftDuration=10, rightDuration=15, bothDuration=2)
    server.add_record(
        DEVICE_A, "Formula", "N0000000-0000-4000-8000-000000000003", when=at(95), amount=volume(3)
    )
    await tick(hass)
    assert _value(hass, LAST_NURSING) == at(90).isoformat()
    assert _value(hass, DURATION) == "27"
    assert _value(hass, LEFT) == "10"
    assert _value(hass, RIGHT) == "15"


async def test_the_sensors_are_unknown_before_the_first_nursing(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """Test a baby that has never nursed has unknown readings."""
    for entity_id in (LAST_NURSING, DURATION, LEFT, RIGHT):
        assert _value(hass, entity_id) == "unknown"


async def test_a_side_with_no_whole_minutes_is_unknown(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    """Test a side the payload doesn't give as an int is unknown and left out of the total."""
    _nursing(server, 4, 30, leftDuration=6, rightDuration=[], bothDuration=True)
    await tick(hass)
    assert _value(hass, LEFT) == "6"
    assert _value(hass, RIGHT) == "unknown"
    assert _value(hass, DURATION) == "6"


async def test_a_nursing_with_no_minutes_at_all_has_no_total(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    """Test the total is unknown, not 0, when no side has minutes."""
    _nursing(server, 5, 30, leftDuration=None, rightDuration=None, bothDuration=None)
    await tick(hass)
    assert _value(hass, LAST_NURSING) == at(30).isoformat()
    assert _value(hass, DURATION) == "unknown"


async def test_the_sleep_duration_shows_the_newest_finished_sleep(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    populated_server: FakeServer,
) -> None:
    """Test the last sleep's minutes, and unknown while baby two's sleep still runs."""
    await setup_integration(hass, mock_config_entry)
    assert _value(hass, SLEEP) == "45"
    state = hass.states.get(SLEEP)
    assert state is not None
    assert state.attributes["device_class"] == "duration"
    assert state.attributes["unit_of_measurement"] == "min"
    assert _value(hass, "sensor.baby_two_last_sleep_duration") == "unknown"


async def test_the_sleep_duration_is_unknown_while_a_sleep_runs_then_shows_it(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    """Test a phone's sleep has no duration while it runs, and its length once stopped."""
    assert _value(hass, SLEEP) == "unknown"
    object_id = "S0000000-0000-4000-8000-000000000001"
    server.add_record(DEVICE_A, "Sleep", object_id, when=at(30), duration=0)
    await tick(hass)
    assert _value(hass, SLEEP) == "unknown"

    server.add_record(DEVICE_A, "Sleep", object_id, when=at(30), op=OP_UPDATE, duration=50)
    await tick(hass)
    assert _value(hass, SLEEP) == "50"


@pytest.mark.skipif(
    not hasattr(pybabytracker, "NursingSession"), reason="needs a pybabytracker that decodes it"
)
async def test_an_android_timer_session_is_the_last_nursing(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    """Test Android's NursingSession counts, newer or older than a Nursing."""
    _nursing(server, 1, 30, leftDuration=4, rightDuration=0, bothDuration=0)
    server.add_record(
        DEVICE_A,
        "NursingSession",
        "N0000000-0000-4000-8000-000000000009",
        when=at(60),
        leftDuration=6,
        rightDuration=9,
        bothDuration=0,
        finishSide=1,
        detailed=True,
        state=5,
        pairSessionState=0,
        leftTimestamps=[],
        rightTimestamps=[],
    )
    await tick(hass)
    assert _value(hass, LAST_NURSING) == at(60).isoformat()
    assert _value(hass, DURATION) == "15"

    _nursing(server, 2, 90, leftDuration=3, rightDuration=0, bothDuration=0)
    await tick(hass)
    assert _value(hass, LAST_NURSING) == at(90).isoformat()
    assert _value(hass, DURATION) == "3"
