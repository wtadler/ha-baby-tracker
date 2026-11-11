"""Core PR 29: the last temperature and growth sensors, and the log_temperature action.

Temperature and growth are numbers worth a graph, so each is a measurement sensor in the unit it
was recorded in. Home Assistant shows it in the unit system's unit, as it does every temperature.
"""

import asyncio
import math
from typing import Any
from unittest.mock import patch

import probatio
import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.core import HomeAssistant, State
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.util.unit_system import METRIC_SYSTEM, US_CUSTOMARY_SYSTEM, UnitSystem
from pybabytracker import BabyTrackerConnectionError, Measurement, Temperature
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.baby_tracker.const import DOMAIN
from custom_components.baby_tracker.writes import async_log_temperature

from . import BABY_TWO, NOW, setup_integration, tick
from .factories import BABY_ONE_ID, BABY_TWO_ID, DEVICE_A, DEVICE_B, OP_DELETE, at
from .fake_server import FakeServer
from .helpers import device_id, posted

TEMPERATURE = "sensor.baby_one_last_temperature"
WEIGHT = "sensor.baby_one_weight"
LENGTH = "sensor.baby_one_length"
HEAD = "sensor.baby_one_head_circumference"
SENSORS = (TEMPERATURE, WEIGHT, LENGTH, HEAD)
NOTE = "Added from Home Assistant"


@pytest.fixture(autouse=True)
def _start_at_now(freezer: FrozenDateTimeFactory) -> None:
    """Freeze the clock at NOW before anything is set up."""
    freezer.move_to(NOW)


async def _setup(
    hass: HomeAssistant, entry: MockConfigEntry, units: UnitSystem = US_CUSTOMARY_SYSTEM
) -> None:
    """Set the entry up under a unit system, which fixes the unit each sensor is shown in."""
    hass.config.units = units
    await setup_integration(hass, entry)


def _record_id(number: int) -> str:
    return f"7E000000-0000-4000-8000-{number:012d}"


def _measure(kind: str, value: Any, english: Any) -> dict[str, Any]:
    """A measure as a phone syncs it: an iPhone's "true"/"false", an Android phone's booleans."""
    return {"BCObjectType": kind, "englishMeasure": english, "value": value}


def _temperature(
    server: FakeServer, number: int, minute: float, value: Any, english: Any, **fields: Any
) -> None:
    server.add_record(
        DEVICE_A,
        "Temperature",
        _record_id(number),
        when=at(minute),
        temperature=_measure("TemperatureMeasure", value, english),
        **fields,
    )


def _growth(server: FakeServer, number: int, minute: float, **measures: Any) -> None:
    server.add_record(DEVICE_A, "Growth", _record_id(number), when=at(minute), **measures)


def _state(hass: HomeAssistant, entity_id: str) -> State:
    state = hass.states.get(entity_id)
    assert state is not None, entity_id
    return state


# Last temperature


async def test_an_iphone_fahrenheit_temperature_is_shown_with_its_unit_and_time(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, server: FakeServer
) -> None:
    """Test the 32-bit float the apps store reads as the number entered."""
    _temperature(server, 1, 30, 100.4000015258789, "true")
    await _setup(hass, mock_config_entry)

    state = _state(hass, TEMPERATURE)
    assert state.state == "100.4"
    assert state.attributes["unit_of_measurement"] == "°F"
    assert state.attributes["time"] == at(30).isoformat()


async def test_an_android_celsius_temperature_is_shown_with_its_unit(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, server: FakeServer
) -> None:
    _temperature(server, 1, 30, 37.5, False)
    await _setup(hass, mock_config_entry, METRIC_SYSTEM)

    state = _state(hass, TEMPERATURE)
    assert state.state == "37.5"
    assert state.attributes["unit_of_measurement"] == "°C"


async def test_an_android_fahrenheit_temperature_reads_its_boolean_unit(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, server: FakeServer
) -> None:
    _temperature(server, 1, 30, 99.5, True)
    await _setup(hass, mock_config_entry)

    state = _state(hass, TEMPERATURE)
    assert state.state == "99.5"
    assert state.attributes["unit_of_measurement"] == "°F"


async def test_an_iphone_celsius_temperature_reads_its_text_unit(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, server: FakeServer
) -> None:
    _temperature(server, 1, 30, 38.2, "false")
    await _setup(hass, mock_config_entry, METRIC_SYSTEM)

    assert _state(hass, TEMPERATURE).state == "38.2"


async def test_a_temperature_in_the_apps_database_shape_is_read(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, server: FakeServer
) -> None:
    server.add_record(
        DEVICE_A, "Temperature", _record_id(1), when=at(30), temp=100.2, isEnglishScale=1
    )
    await _setup(hass, mock_config_entry)

    assert _state(hass, TEMPERATURE).state == "100.2"


async def test_a_fahrenheit_reading_is_shown_in_celsius_under_the_metric_system(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, server: FakeServer
) -> None:
    _temperature(server, 1, 30, 100.4, "true")
    await _setup(hass, mock_config_entry, METRIC_SYSTEM)

    state = _state(hass, TEMPERATURE)
    assert state.attributes["unit_of_measurement"] == "°C"
    assert float(state.state) == pytest.approx(38.0, abs=0.01)


async def test_a_celsius_reading_is_shown_in_fahrenheit_under_us_customary(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, server: FakeServer
) -> None:
    _temperature(server, 1, 30, 38.0, "false")
    await _setup(hass, mock_config_entry)

    state = _state(hass, TEMPERATURE)
    assert state.attributes["unit_of_measurement"] == "°F"
    assert float(state.state) == pytest.approx(100.4, abs=0.01)


async def test_the_newest_temperature_wins_by_when_it_was_taken(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, server: FakeServer
) -> None:
    _temperature(server, 1, 90, 99.1, "true")
    _temperature(server, 2, 30, 101.3, "true")
    await _setup(hass, mock_config_entry)

    state = _state(hass, TEMPERATURE)
    assert state.state == "99.1"
    assert state.attributes["time"] == at(90).isoformat()


async def test_two_temperatures_at_one_time_go_by_when_they_were_saved(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, server: FakeServer
) -> None:
    _temperature(server, 1, 30, 99.1, "true", changed=at(40))
    _temperature(server, 2, 30, 101.3, "true", changed=at(35))
    await _setup(hass, mock_config_entry)

    assert _state(hass, TEMPERATURE).state == "99.1"


@pytest.mark.parametrize(
    "measure",
    [
        None,
        "warm",
        {"BCObjectType": "TemperatureMeasure", "englishMeasure": "true"},
        {"BCObjectType": "TemperatureMeasure", "englishMeasure": "true", "value": "hot"},
        {"BCObjectType": "TemperatureMeasure", "englishMeasure": "true", "value": True},
    ],
    ids=repr,
)
async def test_a_newer_temperature_without_a_reading_does_not_hide_an_older_one(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, server: FakeServer, measure: Any
) -> None:
    _temperature(server, 1, 30, 99.1, "true")
    server.add_record(DEVICE_A, "Temperature", _record_id(2), when=at(90), temperature=measure)
    await _setup(hass, mock_config_entry)

    state = _state(hass, TEMPERATURE)
    assert state.state == "99.1"
    assert state.attributes["time"] == at(30).isoformat()


async def test_the_temperature_updates_when_a_new_one_syncs(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, server: FakeServer
) -> None:
    await _setup(hass, mock_config_entry)
    assert _state(hass, TEMPERATURE).state == "unknown"

    _temperature(server, 1, 30, 99.1, "true")
    await tick(hass)
    assert _state(hass, TEMPERATURE).state == "99.1"

    server.add_record(
        DEVICE_B,
        "Temperature",
        _record_id(2),
        when=at(60),
        temperature=_measure("TemperatureMeasure", 101.5, "true"),
    )
    await tick(hass)
    state = _state(hass, TEMPERATURE)
    assert state.state == "101.5"
    assert state.attributes["time"] == at(60).isoformat()


async def test_a_deleted_temperature_falls_back_to_the_one_before(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, server: FakeServer
) -> None:
    _temperature(server, 1, 30, 99.1, "true")
    _temperature(server, 2, 60, 101.5, "true")
    await _setup(hass, mock_config_entry)
    assert _state(hass, TEMPERATURE).state == "101.5"

    server.add_record(DEVICE_A, "Temperature", _record_id(2), when=at(60), op=OP_DELETE)
    await tick(hass)

    assert _state(hass, TEMPERATURE).state == "99.1"


async def test_another_babys_temperature_is_not_shown(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, server: FakeServer
) -> None:
    _temperature(server, 1, 30, 99.1, "true", baby=BABY_TWO)
    await _setup(hass, mock_config_entry)

    assert _state(hass, TEMPERATURE).state == "unknown"
    assert _state(hass, "sensor.baby_two_last_temperature").state == "99.1"


async def test_the_temperature_sensor_is_a_temperature_measurement(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    await _setup(hass, mock_config_entry)

    state = _state(hass, TEMPERATURE)
    assert state.attributes["device_class"] == SensorDeviceClass.TEMPERATURE
    assert state.attributes["state_class"] == SensorStateClass.MEASUREMENT
    assert state.attributes["friendly_name"] == "Baby One Last temperature"


# Weight, length, and head circumference


async def test_an_iphone_growth_record_in_us_units_shows_all_three(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, server: FakeServer
) -> None:
    _growth(
        server,
        1,
        30,
        weight=_measure("WeightMeasure", 9.25, "true"),
        length=_measure("LengthMeasure", 21.5, "true"),
        head=_measure("LengthMeasure", 14.25, "true"),
    )
    await _setup(hass, mock_config_entry)

    weight, length, head = (_state(hass, entity) for entity in (WEIGHT, LENGTH, HEAD))
    assert (weight.state, weight.attributes["unit_of_measurement"]) == ("9.25", "lb")
    assert (length.state, length.attributes["unit_of_measurement"]) == ("21.5", "in")
    assert (head.state, head.attributes["unit_of_measurement"]) == ("14.25", "in")
    for state in (weight, length, head):
        assert state.attributes["time"] == at(30).isoformat()


async def test_an_android_growth_record_in_metric_units_shows_all_three(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, server: FakeServer
) -> None:
    _growth(
        server,
        1,
        30,
        weight=_measure("WeightMeasure", 4.2, False),
        length=_measure("LengthMeasure", 54.5, False),
        head=_measure("LengthMeasure", 36.0, False),
    )
    await _setup(hass, mock_config_entry, METRIC_SYSTEM)

    weight, length, head = (_state(hass, entity) for entity in (WEIGHT, LENGTH, HEAD))
    assert (weight.state, weight.attributes["unit_of_measurement"]) == ("4.2", "kg")
    assert (length.state, length.attributes["unit_of_measurement"]) == ("54.5", "cm")
    assert (head.state, head.attributes["unit_of_measurement"]) == ("36.0", "cm")


async def test_an_android_growth_record_in_us_units_reads_its_boolean_units(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, server: FakeServer
) -> None:
    _growth(
        server,
        1,
        30,
        weight=_measure("WeightMeasure", 9.25, True),
        length=_measure("LengthMeasure", 21.5, True),
    )
    await _setup(hass, mock_config_entry)

    assert _state(hass, WEIGHT).state == "9.25"
    assert _state(hass, LENGTH).state == "21.5"


async def test_a_growth_record_in_the_apps_database_shape_is_read(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, server: FakeServer
) -> None:
    _growth(
        server,
        1,
        30,
        weight=4.2,
        isEnglishWeightScale=0,
        length=54.5,
        head=36.0,
        isEnglishLengthScale=0,
    )
    await _setup(hass, mock_config_entry, METRIC_SYSTEM)

    assert _state(hass, WEIGHT).state == "4.2"
    assert _state(hass, LENGTH).state == "54.5"
    assert _state(hass, HEAD).state == "36.0"


async def test_a_32_bit_growth_value_reads_as_the_number_entered(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, server: FakeServer
) -> None:
    _growth(server, 1, 30, weight=_measure("WeightMeasure", 9.300000190734863, "true"))
    await _setup(hass, mock_config_entry)

    assert _state(hass, WEIGHT).state == "9.3"


async def test_pounds_are_shown_in_kilograms_under_the_metric_system(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, server: FakeServer
) -> None:
    _growth(
        server,
        1,
        30,
        weight=_measure("WeightMeasure", 10, "true"),
        length=_measure("LengthMeasure", 20, "true"),
    )
    await _setup(hass, mock_config_entry, METRIC_SYSTEM)

    weight, length = _state(hass, WEIGHT), _state(hass, LENGTH)
    assert weight.attributes["unit_of_measurement"] == "kg"
    assert float(weight.state) == pytest.approx(4.536, abs=0.001)
    assert length.attributes["unit_of_measurement"] == "cm"
    assert float(length.state) == pytest.approx(50.8, abs=0.01)


async def test_kilograms_are_shown_in_pounds_under_us_customary(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, server: FakeServer
) -> None:
    _growth(
        server,
        1,
        30,
        weight=_measure("WeightMeasure", 4.5, "false"),
        head=_measure("LengthMeasure", 35.56, "false"),
    )
    await _setup(hass, mock_config_entry)

    weight, head = _state(hass, WEIGHT), _state(hass, HEAD)
    assert weight.attributes["unit_of_measurement"] == "lb"
    assert float(weight.state) == pytest.approx(9.921, abs=0.001)
    assert head.attributes["unit_of_measurement"] == "in"
    assert float(head.state) == pytest.approx(14.0, abs=0.01)


async def test_a_newer_weight_only_record_does_not_hide_an_older_length(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, server: FakeServer
) -> None:
    _growth(
        server,
        1,
        30,
        weight=_measure("WeightMeasure", 9.25, "true"),
        length=_measure("LengthMeasure", 21.5, "true"),
        head=_measure("LengthMeasure", 14.25, "true"),
    )
    _growth(server, 2, 90, weight=_measure("WeightMeasure", 9.5, "true"))
    await _setup(hass, mock_config_entry)

    weight, length, head = (_state(hass, entity) for entity in (WEIGHT, LENGTH, HEAD))
    assert (weight.state, weight.attributes["time"]) == ("9.5", at(90).isoformat())
    assert (length.state, length.attributes["time"]) == ("21.5", at(30).isoformat())
    assert (head.state, head.attributes["time"]) == ("14.25", at(30).isoformat())


async def test_a_newer_head_only_record_does_not_hide_an_older_weight(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, server: FakeServer
) -> None:
    _growth(server, 1, 30, weight=_measure("WeightMeasure", 9.25, "true"))
    _growth(server, 2, 90, head=_measure("LengthMeasure", 14.5, "true"))
    await _setup(hass, mock_config_entry)

    assert _state(hass, WEIGHT).state == "9.25"
    assert _state(hass, HEAD).state == "14.5"
    assert _state(hass, LENGTH).state == "unknown"


async def test_each_growth_sensor_follows_its_own_newest_reading(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, server: FakeServer
) -> None:
    """Test records synced out of order still give each measurement its newest reading."""
    _growth(server, 1, 120, length=_measure("LengthMeasure", 22.0, "true"))
    _growth(
        server,
        2,
        30,
        weight=_measure("WeightMeasure", 9.0, "true"),
        length=_measure("LengthMeasure", 21.0, "true"),
    )
    _growth(server, 3, 60, weight=_measure("WeightMeasure", 9.5, "true"))
    await _setup(hass, mock_config_entry)

    assert _state(hass, WEIGHT).state == "9.5"
    assert _state(hass, LENGTH).state == "22.0"


async def test_the_growth_sensors_update_when_a_new_record_syncs(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, server: FakeServer
) -> None:
    await _setup(hass, mock_config_entry)
    for entity in (WEIGHT, LENGTH, HEAD):
        assert _state(hass, entity).state == "unknown"

    _growth(
        server,
        1,
        30,
        weight=_measure("WeightMeasure", 9.25, "true"),
        length=_measure("LengthMeasure", 21.5, "true"),
    )
    await tick(hass)

    assert _state(hass, WEIGHT).state == "9.25"
    assert _state(hass, LENGTH).state == "21.5"
    assert _state(hass, HEAD).state == "unknown"


async def test_another_babys_growth_is_not_shown(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, server: FakeServer
) -> None:
    server.add_record(
        DEVICE_A,
        "Growth",
        _record_id(1),
        when=at(30),
        baby=BABY_TWO,
        weight=_measure("WeightMeasure", 9.25, "true"),
    )
    await _setup(hass, mock_config_entry)

    assert _state(hass, WEIGHT).state == "unknown"
    assert _state(hass, "sensor.baby_two_weight").state == "9.25"


async def test_the_growth_sensors_are_weight_and_distance_measurements(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    await _setup(hass, mock_config_entry)

    weight, length, head = (_state(hass, entity) for entity in (WEIGHT, LENGTH, HEAD))
    assert weight.attributes["device_class"] == SensorDeviceClass.WEIGHT
    assert length.attributes["device_class"] == SensorDeviceClass.DISTANCE
    assert head.attributes["device_class"] == SensorDeviceClass.DISTANCE
    for state in (weight, length, head):
        assert state.attributes["state_class"] == SensorStateClass.MEASUREMENT
    assert weight.attributes["friendly_name"] == "Baby One Weight"
    assert length.attributes["friendly_name"] == "Baby One Length"
    assert head.attributes["friendly_name"] == "Baby One Head circumference"


# Every one of the four


@pytest.mark.parametrize("units", [METRIC_SYSTEM, US_CUSTOMARY_SYSTEM], ids=["metric", "us"])
async def test_every_baby_has_the_four_sensors_unknown_until_a_reading_and_enabled(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    entity_registry: er.EntityRegistry,
    units: UnitSystem,
) -> None:
    await _setup(hass, mock_config_entry, units)

    for baby, baby_id in (("baby_one", BABY_ONE_ID), ("baby_two", BABY_TWO_ID)):
        for key in ("last_temperature", "weight", "length", "head_circumference"):
            entity_id = f"sensor.{baby}_{key}"
            state = _state(hass, entity_id)
            assert state.state == "unknown"
            assert state.attributes["time"] is None
            registered = entity_registry.async_get(entity_id)
            assert registered is not None
            assert registered.disabled_by is None
            assert registered.unique_id == f"{baby_id}_{key}"


async def test_the_unit_with_no_reading_is_the_unit_systems(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    await _setup(hass, mock_config_entry, METRIC_SYSTEM)

    units = [_state(hass, entity).attributes["unit_of_measurement"] for entity in SENSORS]
    assert units == ["°C", "kg", "cm", "cm"]


async def test_the_unit_with_no_reading_is_us_customary_when_home_assistant_is(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    await _setup(hass, mock_config_entry)

    units = [_state(hass, entity).attributes["unit_of_measurement"] for entity in SENSORS]
    assert units == ["°F", "lb", "in", "in"]


# log_temperature


async def _log(hass: HomeAssistant, temperature: Any, **extra: Any) -> None:
    data = {"device_id": device_id(hass, BABY_ONE_ID), "temperature": temperature, **extra}
    await hass.services.async_call(DOMAIN, "log_temperature", data, blocking=True)


def _posted_temperature(server: FakeServer) -> Temperature:
    [(_, _, record)] = posted(server)
    assert isinstance(record, Temperature)
    return record


async def test_log_temperature_in_fahrenheit(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, server: FakeServer
) -> None:
    await _setup(hass, mock_config_entry, METRIC_SYSTEM)

    await _log(hass, 100.4, unit="°F")

    record = _posted_temperature(server)
    assert record.reading == Measurement(100.4, "F")
    assert record.baby_id == BABY_ONE_ID
    assert record.time == NOW
    assert record.note == NOTE


async def test_log_temperature_in_celsius(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, server: FakeServer
) -> None:
    await _setup(hass, mock_config_entry)

    await _log(hass, 37.8, unit="°C")

    assert _posted_temperature(server).reading == Measurement(37.8, "C")


async def test_log_temperature_defaults_to_celsius_under_the_metric_system(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, server: FakeServer
) -> None:
    await _setup(hass, mock_config_entry, METRIC_SYSTEM)

    await _log(hass, 37.8)

    assert _posted_temperature(server).reading == Measurement(37.8, "C")


async def test_log_temperature_defaults_to_fahrenheit_under_us_customary(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, server: FakeServer
) -> None:
    await _setup(hass, mock_config_entry)

    await _log(hass, 100.4)

    assert _posted_temperature(server).reading == Measurement(100.4, "F")


async def test_a_logged_temperature_shows_on_the_sensor_at_once(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, server: FakeServer
) -> None:
    await _setup(hass, mock_config_entry)

    await _log(hass, 100.4)

    state = _state(hass, TEMPERATURE)
    assert state.state == "100.4"
    assert state.attributes["time"] == NOW.isoformat()


async def test_log_temperature_with_a_note_and_a_time(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, server: FakeServer
) -> None:
    await _setup(hass, mock_config_entry)

    await _log(hass, 100.4, note="Under the arm", time="2026-10-26T12:00:00+00:00")

    record = _posted_temperature(server)
    assert record.note == "Under the arm"
    assert record.time is not None
    assert record.time.isoformat() == "2026-10-26T12:00:00+00:00"


@pytest.mark.parametrize("note", ["", " ", "   ", "\t\n"], ids=repr)
async def test_log_temperature_with_a_blank_note_says_it_came_from_home_assistant(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, server: FakeServer, note: str
) -> None:
    await _setup(hass, mock_config_entry)

    await _log(hass, 100.4, note=note)

    assert _posted_temperature(server).note == NOTE


async def test_a_temperature_given_as_text_is_read_as_a_number(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, server: FakeServer
) -> None:
    await _setup(hass, mock_config_entry)

    await _log(hass, "99.6")

    assert _posted_temperature(server).reading == Measurement(99.6, "F")


async def test_a_temperature_is_rounded_to_two_decimals(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, server: FakeServer
) -> None:
    await _setup(hass, mock_config_entry)

    await _log(hass, 99.456)

    assert _posted_temperature(server).reading == Measurement(99.46, "F")


@pytest.mark.parametrize(
    ("temperature", "unit"),
    [
        (37.5, "°F"),
        (85.9, "°F"),
        (113.1, "°F"),
        (98.6, "°C"),
        (29.9, "°C"),
        (45.1, "°C"),
        (0, "°C"),
        (-5, "°F"),
    ],
)
async def test_an_out_of_range_temperature_is_refused_with_nothing_posted(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    server: FakeServer,
    temperature: float,
    unit: str,
) -> None:
    await _setup(hass, mock_config_entry)

    with pytest.raises(ServiceValidationError) as raised:
        await _log(hass, temperature, unit=unit)

    assert raised.value.translation_key == "temperature_out_of_range"
    low, high = ("86", "113") if unit == "°F" else ("30", "45")
    assert raised.value.translation_placeholders == {"low": low, "high": high, "unit": unit}
    assert server.posted == []


@pytest.mark.parametrize(("temperature", "unit"), [(86, "°F"), (113, "°F"), (30, "°C"), (45, "°C")])
async def test_the_ends_of_the_range_are_accepted(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    server: FakeServer,
    temperature: float,
    unit: str,
) -> None:
    await _setup(hass, mock_config_entry)

    await _log(hass, temperature, unit=unit)

    assert _posted_temperature(server).reading == Measurement(temperature, unit[-1])


async def test_a_celsius_number_under_the_us_system_default_is_refused(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, server: FakeServer
) -> None:
    """Test 37.5 with the default unit (°F here) is caught, not logged as a freezing baby."""
    await _setup(hass, mock_config_entry)

    with pytest.raises(ServiceValidationError) as raised:
        await _log(hass, 37.5)

    assert raised.value.translation_key == "temperature_out_of_range"
    assert server.posted == []


@pytest.mark.parametrize("temperature", [math.nan, math.inf, -math.inf], ids=repr)
async def test_a_temperature_that_is_not_finite_is_refused(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, server: FakeServer, temperature: float
) -> None:
    await _setup(hass, mock_config_entry)

    with pytest.raises(ServiceValidationError) as raised:
        await _log(hass, temperature)

    assert raised.value.translation_key == "temperature_out_of_range"
    assert server.posted == []


@pytest.mark.parametrize("unit", ["F", "C", "K", "°K", "fahrenheit", "", 1, None], ids=repr)
async def test_a_unit_that_is_not_fahrenheit_or_celsius_is_a_schema_error(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, server: FakeServer, unit: Any
) -> None:
    await _setup(hass, mock_config_entry)

    with pytest.raises(probatio.Invalid):
        await _log(hass, 100.4, unit=unit)
    assert server.posted == []


async def test_a_bad_unit_given_directly_is_a_translated_error_with_nothing_posted(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, server: FakeServer
) -> None:
    await _setup(hass, mock_config_entry)

    with pytest.raises(ServiceValidationError) as raised:
        await async_log_temperature(mock_config_entry.runtime_data, BABY_ONE_ID, 300, unit="K")

    assert raised.value.translation_key == "invalid_temperature_unit"
    assert server.posted == []


@pytest.mark.parametrize("temperature", [True, False, "hot", "", [100], {"a": 1}, None], ids=repr)
async def test_a_temperature_of_the_wrong_type_is_a_schema_error(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, server: FakeServer, temperature: Any
) -> None:
    await _setup(hass, mock_config_entry)

    with pytest.raises(probatio.Invalid):
        await _log(hass, temperature)
    assert server.posted == []


async def test_a_missing_temperature_is_a_schema_error(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, server: FakeServer
) -> None:
    await _setup(hass, mock_config_entry)

    with pytest.raises(probatio.Invalid):
        await hass.services.async_call(
            DOMAIN, "log_temperature", {"device_id": device_id(hass, BABY_ONE_ID)}, blocking=True
        )
    assert server.posted == []


async def test_two_calls_with_one_record_id_post_once(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, server: FakeServer
) -> None:
    await _setup(hass, mock_config_entry)
    record_id = "6F1C2A3E-0000-4000-8000-000000000029"

    await asyncio.gather(
        _log(hass, 100.4, object_id=record_id), _log(hass, 100.4, object_id=record_id)
    )

    assert _posted_temperature(server).object_id == record_id


async def test_a_lowercase_record_id_is_written_in_uppercase(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, server: FakeServer
) -> None:
    await _setup(hass, mock_config_entry)

    await _log(hass, 100.4, object_id="6f1c2a3e-0000-4000-8000-00000000002a")

    assert _posted_temperature(server).object_id == "6F1C2A3E-0000-4000-8000-00000000002A"


async def test_two_calls_without_a_record_id_post_twice_one_after_the_other(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, server: FakeServer
) -> None:
    await _setup(hass, mock_config_entry)

    await asyncio.gather(_log(hass, 100.4), _log(hass, 99.8))

    assert len(posted(server)) == 2
    assert server.max_active_posts == 1


async def test_a_record_id_that_is_not_a_uuid_is_a_schema_error(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, server: FakeServer
) -> None:
    await _setup(hass, mock_config_entry)

    with pytest.raises(probatio.Invalid):
        await _log(hass, 100.4, object_id="not-a-uuid")
    assert server.posted == []


async def test_a_record_id_taken_by_another_record_is_refused(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, server: FakeServer
) -> None:
    await _setup(hass, mock_config_entry)
    taken = "6F1C2A3E-0000-4000-8000-0000000000AB"
    await hass.services.async_call(
        DOMAIN,
        "log_diaper",
        {"device_id": device_id(hass, BABY_ONE_ID), "type": "wet", "object_id": taken},
        blocking=True,
    )
    server.posted.clear()

    with pytest.raises(ServiceValidationError) as raised:
        await _log(hass, 100.4, object_id=taken)

    assert raised.value.translation_key == "object_id_in_use"
    assert server.posted == []


async def test_a_sync_that_fails_before_the_write_posts_nothing(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, server: FakeServer
) -> None:
    await _setup(hass, mock_config_entry)
    server.request_error = BabyTrackerConnectionError("down")

    with pytest.raises(HomeAssistantError) as raised:
        await _log(hass, 100.4)

    assert raised.value.translation_key == "sync_failed"
    assert server.posted == []


async def test_a_value_error_the_library_raises_is_a_write_error(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, server: FakeServer
) -> None:
    """Test a refusal the checks here didn't foresee isn't mislabelled as a bad reading."""
    await _setup(hass, mock_config_entry)

    with (
        patch.object(
            mock_config_entry.runtime_data.engine.__class__,
            "add_temperature",
            side_effect=ValueError("no device ID"),
        ),
        pytest.raises(HomeAssistantError) as raised,
    ):
        await _log(hass, 100.4)

    assert not isinstance(raised.value, ServiceValidationError)
    assert raised.value.translation_key == "write_failed"
    assert server.posted == []


async def test_an_unknown_device_is_refused(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, server: FakeServer
) -> None:
    await _setup(hass, mock_config_entry)

    with pytest.raises(ServiceValidationError) as raised:
        await hass.services.async_call(
            DOMAIN,
            "log_temperature",
            {"device_id": "no-such-device", "temperature": 100.4},
            blocking=True,
        )
    assert raised.value.translation_key == "baby_not_found"
    assert server.posted == []


async def test_the_second_baby_gets_its_own_temperature(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, server: FakeServer
) -> None:
    await _setup(hass, mock_config_entry)

    await hass.services.async_call(
        DOMAIN,
        "log_temperature",
        {"device_id": device_id(hass, BABY_TWO_ID), "temperature": 99.0},
        blocking=True,
    )

    assert _posted_temperature(server).baby_id == BABY_TWO_ID
    assert _state(hass, "sensor.baby_two_last_temperature").state == "99.0"
    assert _state(hass, TEMPERATURE).state == "unknown"


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf], ids=repr)
async def test_a_reading_that_is_not_finite_does_not_hide_an_older_one(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, server: FakeServer, value: float
) -> None:
    """Test a NaN or Infinity, which a payload's JSON can carry, reads as no reading."""
    _temperature(server, 1, 30, 99.1, "true")
    _temperature(server, 2, 90, value, "true")
    _growth(server, 3, 30, weight=_measure("WeightMeasure", 9.25, "true"))
    _growth(server, 4, 90, weight=_measure("WeightMeasure", value, "true"))
    await _setup(hass, mock_config_entry)

    assert _state(hass, TEMPERATURE).state == "99.1"
    assert _state(hass, WEIGHT).state == "9.25"
