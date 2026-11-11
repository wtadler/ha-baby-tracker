"""Core PR 4: the last nursing sensor and the last sleep duration sensor."""

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from pybabytracker import BabyTrackerConnectionError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from . import NOW, setup_integration, tick
from .factories import BABY_ONE_ID, DEVICE_A, OP_DELETE, OP_UPDATE, at, volume
from .fake_server import FakeServer

LAST_NURSING = "sensor.baby_one_last_nursing"
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


async def test_the_sensor_shows_when_the_newest_nursing_happened(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    populated_server: FakeServer,
) -> None:
    """Test the time comes from the newest nursing."""
    await setup_integration(hass, mock_config_entry)

    assert _value(hass, LAST_NURSING) == at(40).isoformat()
    state = hass.states.get(LAST_NURSING)
    assert state is not None
    assert state.attributes["device_class"] == "timestamp"


async def test_a_bottle_after_the_nursing_does_not_hide_it(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    populated_server: FakeServer,
) -> None:
    """Test the sensor keeps the nursing when a later feed is a bottle."""
    await setup_integration(hass, mock_config_entry)
    # populated_server's pumped bottle at minute 60 is already newer than the nursing at 40.
    assert _value(hass, "sensor.baby_one_last_feed") == at(60).isoformat()
    assert _value(hass, LAST_NURSING) == at(40).isoformat()


async def test_a_newer_nursing_replaces_the_reading(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    """Test a nursing that syncs later moves the sensor to it, a bottle after it or not."""
    _nursing(server, 1, 30, leftDuration=4, rightDuration=0, bothDuration=0)
    await tick(hass)
    assert _value(hass, LAST_NURSING) == at(30).isoformat()

    _nursing(server, 2, 90, leftDuration=10, rightDuration=15, bothDuration=2)
    server.add_record(
        DEVICE_A, "Formula", "N0000000-0000-4000-8000-000000000003", when=at(95), amount=volume(3)
    )
    await tick(hass)
    assert _value(hass, LAST_NURSING) == at(90).isoformat()


async def test_the_sensor_is_unknown_before_the_first_nursing(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """Test a baby that has never nursed has an unknown reading."""
    assert _value(hass, LAST_NURSING) == "unknown"


async def test_a_nursing_with_no_minutes_still_shows_when_it_happened(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    """Test the time doesn't depend on the minutes the payload carries."""
    _nursing(server, 5, 30, leftDuration=None, rightDuration=None, bothDuration=None)
    await tick(hass)
    assert _value(hass, LAST_NURSING) == at(30).isoformat()


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

    _nursing(server, 2, 90, leftDuration=3, rightDuration=0, bothDuration=0)
    await tick(hass)
    assert _value(hass, LAST_NURSING) == at(90).isoformat()


@pytest.mark.parametrize(
    ("duration", "state"),
    [
        (-5, "unknown"),  # a negative length is no minutes
        (5.0, "unknown"),  # a float isn't a whole number of minutes
        ("40", "40"),  # Android sends digit strings
        ("", "unknown"),  # an empty string
        ("  ", "unknown"),  # whitespace only
        ("+5", "unknown"),  # not plain digits
        ("٣", "unknown"),  # not ASCII digits
        (True, "unknown"),  # a bool isn't a number
        (None, "unknown"),
        ([], "unknown"),
        (10**30, str(10**30)),  # huge, but still a whole number
    ],
)
async def test_the_sleep_minutes_read_as_the_library_reads_a_code(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    server: FakeServer,
    duration: object,
    state: str,
) -> None:
    """Test a malformed length gives unknown, whatever the payload holds."""
    server.add_record(
        DEVICE_A, "Sleep", "S0000000-0000-4000-8000-000000000006", when=at(30), duration=duration
    )
    await tick(hass)
    assert _value(hass, SLEEP) == state


async def test_a_nursing_with_no_duration_keys_at_all_still_shows_when_it_happened(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    """Test a record that lacks every duration key still shows when it happened."""
    _nursing(server, 7, 30)
    await tick(hass)
    assert _value(hass, LAST_NURSING) == at(30).isoformat()


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
    assert _value(hass, LAST_NURSING) == at(30).isoformat()
    # The sensor shows the time alone, which the two share: the coordinator holds the record.
    last = init_integration.runtime_data.data.babies[BABY_ONE_ID].last_nursing
    assert last is not None
    assert last.object_id == "N0000000-0000-4000-8000-000000000009"


async def test_a_deleted_nursing_hands_the_sensor_back_to_the_one_before(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    """Test deleting the newest nursing moves the reading back to the previous one."""
    _nursing(server, 10, 30, leftDuration=4, rightDuration=0, bothDuration=0)
    _nursing(server, 11, 60, leftDuration=9, rightDuration=0, bothDuration=0)
    await tick(hass)
    assert _value(hass, LAST_NURSING) == at(60).isoformat()

    server.add_record(
        DEVICE_A,
        "Nursing",
        "N0000000-0000-4000-8000-000000000011",
        when=at(60),
        op=OP_DELETE,
    )
    await tick(hass)
    assert _value(hass, LAST_NURSING) == at(30).isoformat()


@pytest.mark.parametrize("entity_id", [LAST_NURSING, SLEEP])
async def test_the_sensors_are_unavailable_when_the_sync_fails(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, entity_id: str
) -> None:
    """Test a sync failure makes each detail sensor unavailable, like the other sensors."""
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
