"""Core PR 4: the last nursing sensors (time, total and side minutes) and last sleep duration."""

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pybabytracker import BabyTrackerConnectionError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.baby_tracker.const import DOMAIN

from . import NOW, setup_integration, tick
from .factories import BABY_ONE_ID, DEVICE_A, OP_DELETE, OP_UPDATE, at, volume
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


@pytest.fixture(autouse=True)
def _enable_the_detail_sensors(hass: HomeAssistant, mock_config_entry: MockConfigEntry) -> None:
    """Enable the sensors that start disabled, by registering them before the entry sets up."""
    mock_config_entry.add_to_hass(hass)
    registry = er.async_get(hass)
    for key, entity_id in (
        ("last_nursing_duration", DURATION),
        ("last_nursing_left_time", LEFT),
        ("last_nursing_right_time", RIGHT),
    ):
        registry.async_get_or_create(
            "sensor",
            DOMAIN,
            f"{BABY_ONE_ID}_{key}",
            config_entry=mock_config_entry,
            suggested_object_id=entity_id.removeprefix("sensor."),
            disabled_by=None,
        )


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


@pytest.mark.parametrize(
    ("left", "right", "both", "total", "left_state", "right_state"),
    [
        (-5, 4, 0, "4", "unknown", "4"),  # a negative side is no minutes, not a subtraction
        (5.0, 4, 0, "4", "unknown", "4"),  # a float isn't a whole number of minutes
        ("4", "6", 0, "10", "4", "6"),  # Android sends digit strings
        ("", " ", 0, "0", "unknown", "unknown"),  # empty and whitespace-only strings
        ("+5", "٣", 0, "0", "unknown", "unknown"),  # not plain digits
        (True, None, None, "unknown", "unknown", "unknown"),  # a bool isn't a number
        (10**30, 0, 0, str(10**30), str(10**30), "0"),  # huge, but still a whole number
    ],
)
async def test_the_minutes_read_as_the_library_reads_a_code(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    server: FakeServer,
    left: object,
    right: object,
    both: object,
    total: str,
    left_state: str,
    right_state: str,
) -> None:
    """Test a malformed side gives unknown and adds nothing, whatever the payload holds."""
    _nursing(server, 6, 30, leftDuration=left, rightDuration=right, bothDuration=both)
    await tick(hass)
    assert _value(hass, LAST_NURSING) == at(30).isoformat()
    assert _value(hass, LEFT) == left_state
    assert _value(hass, RIGHT) == right_state
    assert _value(hass, DURATION) == total


async def test_a_nursing_with_no_duration_keys_at_all_is_unknown(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    """Test a record that lacks every duration key still shows when it happened."""
    _nursing(server, 7, 30)
    await tick(hass)
    assert _value(hass, LAST_NURSING) == at(30).isoformat()
    for entity_id in (DURATION, LEFT, RIGHT):
        assert _value(hass, entity_id) == "unknown"


async def test_a_nursing_that_ties_on_time_is_settled_by_its_change_time(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    """Test of two nursings at one moment, the one saved later is the last."""
    server.add_record(
        DEVICE_A,
        "Nursing",
        "N0000000-0000-4000-8000-000000000008",
        when=at(30),
        changed=at(31),
        leftDuration=8,
        rightDuration=0,
        bothDuration=0,
    )
    server.add_record(
        DEVICE_A,
        "Nursing",
        "N0000000-0000-4000-8000-000000000009",
        when=at(30),
        changed=at(32),
        leftDuration=9,
        rightDuration=0,
        bothDuration=0,
    )
    await tick(hass)
    assert _value(hass, LEFT) == "9"


async def test_a_deleted_nursing_hands_the_sensors_back_to_the_one_before(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    """Test deleting the newest nursing moves the readings back to the previous one."""
    _nursing(server, 10, 30, leftDuration=4, rightDuration=0, bothDuration=0)
    _nursing(server, 11, 60, leftDuration=9, rightDuration=0, bothDuration=0)
    await tick(hass)
    assert _value(hass, LEFT) == "9"

    server.add_record(
        DEVICE_A,
        "Nursing",
        "N0000000-0000-4000-8000-000000000011",
        when=at(60),
        op=OP_DELETE,
    )
    await tick(hass)
    assert _value(hass, LEFT) == "4"
    assert _value(hass, LAST_NURSING) == at(30).isoformat()


@pytest.mark.parametrize("entity_id", [LAST_NURSING, DURATION, LEFT, RIGHT, SLEEP])
async def test_the_sensors_are_unavailable_when_the_sync_fails(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, entity_id: str
) -> None:
    """Test a sync failure makes every detail sensor unavailable, like the other sensors."""
    _nursing(server, 12, 30, leftDuration=4, rightDuration=0, bothDuration=0)
    await tick(hass)
    assert _value(hass, entity_id) != "unavailable"

    server.request_error = BabyTrackerConnectionError("down")
    await tick(hass)
    assert _value(hass, entity_id) == "unavailable"


async def test_a_sleep_with_a_malformed_duration_is_unknown(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    """Test a finished sleep whose duration isn't a number reads as unknown."""
    object_id = "S0000000-0000-4000-8000-000000000002"
    server.add_record(DEVICE_A, "Sleep", object_id, when=at(30), duration="soon")
    await tick(hass)
    assert _value(hass, SLEEP) == "unknown"
