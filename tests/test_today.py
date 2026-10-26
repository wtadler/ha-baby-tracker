"""Today's counts, today's bottle volume, and the asleep sensor."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.util.unit_system import METRIC_SYSTEM, US_CUSTOMARY_SYSTEM
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.nighp_baby_tracker.const import (
    CONF_DEVICE_UUID,
    CONF_EMAIL,
    CONF_PASSWORD,
    CONF_SCAN_INTERVAL,
    DOMAIN,
)
from custom_components.nighp_baby_tracker.pybabytracker import BabyTrackerConnectionError

from .conftest import BABY_ONE, BABY_TWO, EMAIL, PASSWORD, FakeServer
from .factories import (
    BABY_ONE_ID,
    BABY_THREE_ID,
    BABY_TWO_ID,
    DEVICE_A,
    DEVICE_B,
    OP_DELETE,
    OP_UPDATE,
    baby_payload,
    volume,
)
from .helpers import tick

NEW_YORK = ZoneInfo("America/New_York")
BABY_THREE = baby_payload("Baby Three", BABY_THREE_ID)
ML_PER_OZ = 29.5735295625


def local(year: int, month: int, day: int, hour: int = 0, minute: int = 0, second: int = 0):
    return datetime(year, month, day, hour, minute, second, tzinfo=NEW_YORK)


# The default "now" is Monday, October 26, 2026 at 4:00 p.m. Eastern (daylight time).
NOW = local(2026, 10, 26, 16)
MIDNIGHT = local(2026, 10, 27)
TODAY = local(2026, 10, 26, 9)
YESTERDAY = local(2026, 10, 25, 9)

_counter = 0


def _id() -> str:
    global _counter
    _counter += 1
    return f"A0000000-0000-4000-8000-{_counter:012d}"


@pytest.fixture(autouse=True)
def _reset_counter() -> Iterator[None]:
    global _counter
    _counter = 0
    yield


def _state(hass: HomeAssistant, entity: str, baby: str = "baby_one"):
    domain = "binary_sensor" if entity == "asleep" else "sensor"
    state = hass.states.get(f"{domain}.{baby}_{entity}")
    assert state is not None, f"{domain}.{baby}_{entity} does not exist"
    return state


def _value(hass: HomeAssistant, entity: str, baby: str = "baby_one") -> str:
    return _state(hass, entity, baby).state


@pytest.fixture
async def clock(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> FrozenDateTimeFactory:
    """Eastern time, frozen at the default "now"."""
    await hass.config.async_set_time_zone("America/New_York")
    freezer.move_to(NOW)
    return freezer


@pytest.fixture
def empty(server: FakeServer) -> FakeServer:
    """The fake service with no records: each test adds exactly what it needs."""
    server.logs.clear()
    server.names.clear()
    server.names[DEVICE_A] = "Test Phone"
    server.add_payload(DEVICE_A, BABY_ONE)
    server.add_payload(DEVICE_A, BABY_TWO)
    return server


@pytest.fixture
def entry(hass: HomeAssistant, clock: FrozenDateTimeFactory) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=EMAIL,
        unique_id=EMAIL,
        data={
            CONF_EMAIL: EMAIL,
            CONF_PASSWORD: PASSWORD,
            CONF_DEVICE_UUID: "EEEEEEEE-0000-4000-8000-0000000000EE",
        },
        # A day-long interval, so a sync can never be what moves a count at midnight.
        options={CONF_SCAN_INTERVAL: 1440},
    )
    entry.add_to_hass(hass)
    return entry


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED


async def _pass_midnight(
    hass: HomeAssistant, clock: FrozenDateTimeFactory, moment: datetime = MIDNIGHT
) -> None:
    """Move the clock to just after a moment and let Home Assistant's timers run."""
    clock.move_to(moment + timedelta(seconds=1))
    async_fire_time_changed(hass, moment + timedelta(seconds=1))
    await hass.async_block_till_done()


def _diaper(server: FakeServer, when: datetime, status: int = 0, baby=BABY_ONE, **kw) -> str:
    object_id = _id()
    server.add_record(DEVICE_A, "Diaper", object_id, when=when, status=status, baby=baby, **kw)
    return object_id


def _breakdown(state) -> tuple[int, int, int]:
    attributes = state.attributes
    return attributes["wet"], attributes["dirty"], attributes["mixed"]


# Entities


async def test_each_baby_gets_the_four_new_entities(
    hass: HomeAssistant, entry: MockConfigEntry, empty: FakeServer
) -> None:
    await _setup(hass, entry)
    registry = er.async_get(hass)
    devices = dr.async_get(hass)
    for baby_id in (BABY_ONE_ID, BABY_TWO_ID):
        device = devices.async_get_device(identifiers={(DOMAIN, baby_id)})
        assert device is not None
        by_key = {
            entity.translation_key: entity
            for entity in er.async_entries_for_device(registry, device.id)
        }
        for key in ("diapers_today", "feeds_today", "bottle_volume_today", "asleep"):
            assert by_key[key].unique_id == f"{baby_id}_{key}"


async def test_the_entities_are_named_after_the_baby(
    hass: HomeAssistant, entry: MockConfigEntry, empty: FakeServer
) -> None:
    await _setup(hass, entry)
    names = {
        "diapers_today": "Diapers today",
        "feeds_today": "Feeds today",
        "bottle_volume_today": "Bottle volume today",
        "asleep": "Asleep",
    }
    for baby, name in (("baby_one", "Baby One"), ("baby_two", "Baby Two")):
        for key, label in names.items():
            assert _state(hass, key, baby).attributes["friendly_name"] == f"{name} {label}"


async def test_the_volume_sensor_is_a_volume(
    hass: HomeAssistant, entry: MockConfigEntry, empty: FakeServer
) -> None:
    await _setup(hass, entry)
    assert _state(hass, "bottle_volume_today").attributes["device_class"] == "volume"


# Diapers


async def test_three_diapers_today_and_two_yesterday_count_three(
    hass: HomeAssistant, entry: MockConfigEntry, empty: FakeServer
) -> None:
    for status in (0, 1, 2):
        _diaper(empty, TODAY, status)
    for status in (0, 1):
        _diaper(empty, YESTERDAY, status)
    await _setup(hass, entry)
    state = _state(hass, "diapers_today")
    assert state.state == "3"
    assert _breakdown(state) == (1, 1, 1)


async def test_the_diaper_attributes_break_the_count_down_by_type(
    hass: HomeAssistant, entry: MockConfigEntry, empty: FakeServer
) -> None:
    for status in (0, 0, 0, 1, 1, 2):
        _diaper(empty, TODAY, status)
    await _setup(hass, entry)
    state = _state(hass, "diapers_today")
    assert state.state == "6"
    assert _breakdown(state) == (3, 2, 1)


async def test_an_unrecognized_diaper_status_counts_in_the_total_only(
    hass: HomeAssistant, entry: MockConfigEntry, empty: FakeServer
) -> None:
    _diaper(empty, TODAY, 0)
    _diaper(empty, TODAY, 9)
    await _setup(hass, entry)
    state = _state(hass, "diapers_today")
    assert state.state == "2"
    assert _breakdown(state) == (1, 0, 0)


async def test_babies_do_not_share_counts(
    hass: HomeAssistant, entry: MockConfigEntry, empty: FakeServer
) -> None:
    _diaper(empty, TODAY, 0)
    _diaper(empty, TODAY, 1, baby=BABY_TWO)
    _diaper(empty, TODAY, 2, baby=BABY_TWO)
    await _setup(hass, entry)
    assert _value(hass, "diapers_today", "baby_one") == "1"
    assert _value(hass, "diapers_today", "baby_two") == "2"


async def test_a_diaper_from_another_device_is_counted_after_the_next_sync(
    hass: HomeAssistant, entry: MockConfigEntry, empty: FakeServer
) -> None:
    await _setup(hass, entry)
    assert _value(hass, "diapers_today") == "0"
    empty.add_record(DEVICE_B, "Diaper", _id(), when=TODAY, status=1)
    await tick(hass, minutes=1440)
    state = _state(hass, "diapers_today")
    assert state.state == "1"
    assert _breakdown(state) == (0, 1, 0)


async def test_a_diaper_dated_today_but_deleted_is_not_counted(
    hass: HomeAssistant, entry: MockConfigEntry, empty: FakeServer
) -> None:
    _diaper(empty, TODAY, 0)
    gone = _diaper(empty, TODAY, 1)
    empty.add_record(DEVICE_A, "Diaper", gone, when=TODAY, status=1, op=OP_DELETE)
    await _setup(hass, entry)
    state = _state(hass, "diapers_today")
    assert state.state == "1"
    assert _breakdown(state) == (1, 0, 0)


async def test_a_diaper_deleted_after_the_first_sync_drops_out_of_the_count(
    hass: HomeAssistant, entry: MockConfigEntry, empty: FakeServer
) -> None:
    gone = _diaper(empty, TODAY, 1)
    await _setup(hass, entry)
    assert _value(hass, "diapers_today") == "1"
    empty.add_record(DEVICE_A, "Diaper", gone, when=TODAY, status=1, op=OP_DELETE)
    await tick(hass, minutes=1440)
    assert _value(hass, "diapers_today") == "0"


async def test_a_diaper_edited_to_another_day_leaves_the_count(
    hass: HomeAssistant, entry: MockConfigEntry, empty: FakeServer
) -> None:
    object_id = _diaper(empty, TODAY, 0)
    await _setup(hass, entry)
    assert _value(hass, "diapers_today") == "1"
    empty.add_record(
        DEVICE_A, "Diaper", object_id, when=YESTERDAY, changed=NOW, status=0, op=OP_UPDATE
    )
    await tick(hass, minutes=1440)
    assert _value(hass, "diapers_today") == "0"


# Feeds


async def test_the_feed_count_covers_nursing_formula_and_pumped(
    hass: HomeAssistant, entry: MockConfigEntry, empty: FakeServer
) -> None:
    empty.add_record(DEVICE_A, "Nursing", _id(), when=TODAY, leftDuration=5, rightDuration=3)
    empty.add_record(DEVICE_A, "Formula", _id(), when=TODAY, amount=volume(4))
    empty.add_record(DEVICE_A, "Pumped", _id(), when=TODAY, amount=volume(90, english=False))
    empty.add_record(DEVICE_A, "Nursing", _id(), when=YESTERDAY, leftDuration=5)
    await _setup(hass, entry)
    assert _value(hass, "feeds_today") == "3"


async def test_other_feeds_pump_sessions_and_diapers_are_not_feeds(
    hass: HomeAssistant, entry: MockConfigEntry, empty: FakeServer
) -> None:
    empty.add_record(DEVICE_A, "OtherFeed", _id(), when=TODAY, amount=2.5)
    empty.add_record(DEVICE_A, "Pump", _id(), when=TODAY, amount=volume(100, english=False))
    _diaper(empty, TODAY)
    empty.add_record(DEVICE_A, "Sleep", _id(), when=TODAY, duration=30)
    await _setup(hass, entry)
    assert _value(hass, "feeds_today") == "0"


async def test_a_deleted_feed_is_not_counted(
    hass: HomeAssistant, entry: MockConfigEntry, empty: FakeServer
) -> None:
    gone = _id()
    empty.add_record(DEVICE_A, "Formula", gone, when=TODAY, amount=volume(4))
    empty.add_record(DEVICE_A, "Formula", gone, when=TODAY, amount=volume(4), op=OP_DELETE)
    empty.add_record(DEVICE_A, "Nursing", _id(), when=TODAY, leftDuration=5)
    await _setup(hass, entry)
    assert _value(hass, "feeds_today") == "1"


async def test_the_feed_count_does_not_mix_babies(
    hass: HomeAssistant, entry: MockConfigEntry, empty: FakeServer
) -> None:
    empty.add_record(DEVICE_A, "Nursing", _id(), when=TODAY, leftDuration=5)
    empty.add_record(DEVICE_A, "Nursing", _id(), when=TODAY, baby=BABY_TWO, leftDuration=5)
    empty.add_record(DEVICE_A, "Nursing", _id(), when=TODAY, baby=BABY_TWO, leftDuration=5)
    await _setup(hass, entry)
    assert _value(hass, "feeds_today", "baby_one") == "1"
    assert _value(hass, "feeds_today", "baby_two") == "2"


# Volume


async def test_mixed_milliliter_and_ounce_bottles_sum_in_milliliters_for_metric(
    hass: HomeAssistant, entry: MockConfigEntry, empty: FakeServer
) -> None:
    hass.config.units = METRIC_SYSTEM
    empty.add_record(DEVICE_A, "Formula", _id(), when=TODAY, amount=volume(120, english=False))
    empty.add_record(DEVICE_A, "Pumped", _id(), when=TODAY, amount=volume(4))
    await _setup(hass, entry)
    state = _state(hass, "bottle_volume_today")
    assert state.attributes["unit_of_measurement"] == "mL"
    assert float(state.state) == pytest.approx(120 + 4 * ML_PER_OZ, abs=0.1)


async def test_mixed_milliliter_and_ounce_bottles_sum_in_ounces_for_us_customary(
    hass: HomeAssistant, entry: MockConfigEntry, empty: FakeServer
) -> None:
    hass.config.units = US_CUSTOMARY_SYSTEM
    empty.add_record(DEVICE_A, "Formula", _id(), when=TODAY, amount=volume(120, english=False))
    empty.add_record(DEVICE_A, "Pumped", _id(), when=TODAY, amount=volume(4))
    await _setup(hass, entry)
    state = _state(hass, "bottle_volume_today")
    assert state.attributes["unit_of_measurement"] == "fl. oz."
    assert float(state.state) == pytest.approx(120 / ML_PER_OZ + 4, abs=0.01)


async def test_one_ounce_bottle_reads_as_ounces_for_us_customary(
    hass: HomeAssistant, entry: MockConfigEntry, empty: FakeServer
) -> None:
    hass.config.units = US_CUSTOMARY_SYSTEM
    empty.add_record(DEVICE_A, "Formula", _id(), when=TODAY, amount=volume(5))
    await _setup(hass, entry)
    state = _state(hass, "bottle_volume_today")
    assert state.attributes["unit_of_measurement"] == "fl. oz."
    assert float(state.state) == pytest.approx(5, abs=0.01)


async def test_only_bottles_from_today_are_summed(
    hass: HomeAssistant, entry: MockConfigEntry, empty: FakeServer
) -> None:
    hass.config.units = METRIC_SYSTEM
    empty.add_record(DEVICE_A, "Formula", _id(), when=TODAY, amount=volume(100, english=False))
    empty.add_record(DEVICE_A, "Formula", _id(), when=YESTERDAY, amount=volume(500, english=False))
    await _setup(hass, entry)
    assert float(_value(hass, "bottle_volume_today")) == pytest.approx(100)


async def test_pump_sessions_nursing_and_other_feeds_add_no_volume(
    hass: HomeAssistant, entry: MockConfigEntry, empty: FakeServer
) -> None:
    hass.config.units = METRIC_SYSTEM
    empty.add_record(DEVICE_A, "Pump", _id(), when=TODAY, amount=volume(200, english=False))
    empty.add_record(DEVICE_A, "OtherFeed", _id(), when=TODAY, amount=50.0)
    empty.add_record(DEVICE_A, "Nursing", _id(), when=TODAY, leftDuration=5)
    empty.add_record(DEVICE_A, "Formula", _id(), when=TODAY, amount=volume(60, english=False))
    await _setup(hass, entry)
    assert float(_value(hass, "bottle_volume_today")) == pytest.approx(60)


async def test_a_bottle_with_no_readable_unit_counts_as_a_feed_but_adds_no_volume(
    hass: HomeAssistant, entry: MockConfigEntry, empty: FakeServer
) -> None:
    hass.config.units = METRIC_SYSTEM
    # A plain number with no scale has no unit, so it can't be added to anything.
    empty.add_record(DEVICE_A, "Formula", _id(), when=TODAY, amount=3)
    empty.add_record(DEVICE_A, "Formula", _id(), when=TODAY, amount=volume(60, english=False))
    await _setup(hass, entry)
    assert _value(hass, "feeds_today") == "2"
    assert float(_value(hass, "bottle_volume_today")) == pytest.approx(60)


async def test_a_deleted_bottle_is_not_summed(
    hass: HomeAssistant, entry: MockConfigEntry, empty: FakeServer
) -> None:
    hass.config.units = METRIC_SYSTEM
    gone = _id()
    empty.add_record(DEVICE_A, "Formula", gone, when=TODAY, amount=volume(100, english=False))
    empty.add_record(
        DEVICE_A, "Formula", gone, when=TODAY, amount=volume(100, english=False), op=OP_DELETE
    )
    await _setup(hass, entry)
    assert float(_value(hass, "bottle_volume_today")) == 0


async def test_a_bottle_edited_to_a_new_amount_changes_the_total(
    hass: HomeAssistant, entry: MockConfigEntry, empty: FakeServer
) -> None:
    hass.config.units = METRIC_SYSTEM
    bottle = _id()
    empty.add_record(DEVICE_A, "Formula", bottle, when=TODAY, amount=volume(100, english=False))
    await _setup(hass, entry)
    empty.add_record(
        DEVICE_A,
        "Formula",
        bottle,
        when=TODAY,
        changed=NOW,
        amount=volume(80, english=False),
        op=OP_UPDATE,
    )
    await tick(hass, minutes=1440)
    assert float(_value(hass, "bottle_volume_today")) == pytest.approx(80)


# A baby with nothing logged


async def test_a_baby_with_no_records_has_zero_counts_and_is_awake(
    hass: HomeAssistant, entry: MockConfigEntry, empty: FakeServer
) -> None:
    empty.add_payload(DEVICE_A, BABY_THREE)
    await _setup(hass, entry)
    assert _value(hass, "diapers_today", "baby_three") == "0"
    assert _value(hass, "feeds_today", "baby_three") == "0"
    assert float(_value(hass, "bottle_volume_today", "baby_three")) == 0
    assert _value(hass, "asleep", "baby_three") == "off"
    assert _breakdown(_state(hass, "diapers_today", "baby_three")) == (0, 0, 0)


async def test_a_baby_whose_records_are_all_old_has_zero_counts(
    hass: HomeAssistant, entry: MockConfigEntry, empty: FakeServer
) -> None:
    _diaper(empty, YESTERDAY)
    empty.add_record(DEVICE_A, "Formula", _id(), when=YESTERDAY, amount=volume(4))
    await _setup(hass, entry)
    assert _value(hass, "diapers_today") == "0"
    assert _value(hass, "feeds_today") == "0"


# Raw records


async def test_a_raw_record_the_library_cannot_model_is_ignored_by_the_counts(
    hass: HomeAssistant, entry: MockConfigEntry, empty: FakeServer
) -> None:
    hass.config.units = METRIC_SYSTEM
    stamp = TODAY.strftime("%Y-%m-%d %H:%M:%S %z")
    # Known types whose values the models reject are kept raw, like a type the library lacks.
    for kind, fields in (
        ("Diaper", {}),
        ("Formula", {"amount": volume(500, english=False)}),
        ("Sleep", {"duration": 0}),
        ("Hiccups", {}),
    ):
        empty.add_payload(
            DEVICE_A,
            {
                "BCObjectType": kind,
                "objectID": _id(),
                "time": stamp,
                "timestamp": "not a time",
                "baby": BABY_ONE,
                **fields,
            },
        )
    _diaper(empty, TODAY)
    await _setup(hass, entry)
    assert _value(hass, "diapers_today") == "1"
    assert _value(hass, "feeds_today") == "0"
    assert float(_value(hass, "bottle_volume_today")) == 0
    assert _value(hass, "asleep") == "off"


# Midnight


async def test_the_counts_reset_at_local_midnight_without_a_sync(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    empty: FakeServer,
    clock: FrozenDateTimeFactory,
) -> None:
    hass.config.units = METRIC_SYSTEM
    _diaper(empty, TODAY)
    empty.add_record(DEVICE_A, "Formula", _id(), when=TODAY, amount=volume(100, english=False))
    await _setup(hass, entry)
    assert _value(hass, "diapers_today") == "1"
    assert _value(hass, "feeds_today") == "1"
    reads = len(empty.transaction_reads)
    listed = empty.device_list_calls

    await _pass_midnight(hass, clock)

    assert _value(hass, "diapers_today") == "0"
    assert _value(hass, "feeds_today") == "0"
    assert float(_value(hass, "bottle_volume_today")) == 0
    assert _breakdown(_state(hass, "diapers_today")) == (0, 0, 0)
    # The reset came from the clock, not from asking the service.
    assert len(empty.transaction_reads) == reads
    assert empty.device_list_calls == listed


async def test_the_counts_do_not_reset_before_midnight(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    empty: FakeServer,
    clock: FrozenDateTimeFactory,
) -> None:
    _diaper(empty, TODAY)
    await _setup(hass, entry)
    clock.move_to(local(2026, 10, 26, 23, 59, 0))
    async_fire_time_changed(hass, local(2026, 10, 26, 23, 59, 0))
    await hass.async_block_till_done()
    assert _value(hass, "diapers_today") == "1"


async def test_the_counts_reset_again_every_midnight(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    empty: FakeServer,
    clock: FrozenDateTimeFactory,
) -> None:
    _diaper(empty, local(2026, 10, 27, 8))
    await _setup(hass, entry)
    assert _value(hass, "diapers_today") == "0"
    await _pass_midnight(hass, clock, MIDNIGHT)
    assert _value(hass, "diapers_today") == "1"
    await _pass_midnight(hass, clock, local(2026, 10, 28))
    assert _value(hass, "diapers_today") == "0"


async def test_a_record_exactly_at_midnight_belongs_to_the_new_day(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    empty: FakeServer,
    clock: FrozenDateTimeFactory,
) -> None:
    clock.move_to(local(2026, 10, 26, 12))
    _diaper(empty, local(2026, 10, 26, 0, 0, 0))  # the start of today: counted
    _diaper(empty, local(2026, 10, 25, 23, 59, 59))  # the last second of yesterday: not counted
    _diaper(empty, MIDNIGHT)  # the start of tomorrow: not counted yet
    await _setup(hass, entry)
    assert _value(hass, "diapers_today") == "1"

    await _pass_midnight(hass, clock)
    assert _value(hass, "diapers_today") == "1"  # now the record at midnight, and only it


# Daylight saving time


async def test_a_fall_back_day_still_counts_the_local_calendar_day(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    empty: FakeServer,
    clock: FrozenDateTimeFactory,
) -> None:
    # Sunday, November 1, 2026 is 25 hours long in New York: 00:00 EDT to 00:00 EST.
    clock.move_to(local(2026, 11, 1, 23, 40))
    _diaper(empty, local(2026, 11, 1, 0, 0, 0))  # the first moment of the day
    _diaper(empty, local(2026, 11, 1, 23, 30))  # more than 24 hours after the first
    _diaper(empty, local(2026, 10, 31, 23, 59, 59))  # the day before
    _diaper(empty, local(2026, 11, 2, 0, 0, 0))  # the day after
    await _setup(hass, entry)
    assert _value(hass, "diapers_today") == "2"

    await _pass_midnight(hass, clock, local(2026, 11, 2))
    assert _value(hass, "diapers_today") == "1"


async def test_a_spring_forward_day_still_counts_the_local_calendar_day(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    empty: FakeServer,
    clock: FrozenDateTimeFactory,
) -> None:
    # Sunday, March 8, 2026 is 23 hours long in New York: 00:00 EST to 00:00 EDT.
    clock.move_to(local(2026, 3, 8, 12))
    _diaper(empty, local(2026, 3, 8, 0, 0, 0))
    _diaper(empty, local(2026, 3, 8, 23, 59, 59))
    _diaper(empty, local(2026, 3, 7, 23, 59, 59))
    _diaper(empty, local(2026, 3, 9, 0, 0, 0))
    await _setup(hass, entry)
    assert _value(hass, "diapers_today") == "2"

    await _pass_midnight(hass, clock, local(2026, 3, 9))
    assert _value(hass, "diapers_today") == "1"


# Asleep


def _sleep(server: FakeServer, when: datetime, duration: int, baby=BABY_ONE, **kw) -> str:
    object_id = _id()
    server.add_record(DEVICE_A, "Sleep", object_id, when=when, duration=duration, baby=baby, **kw)
    return object_id


async def test_asleep_is_on_while_the_newest_sleep_is_running(
    hass: HomeAssistant, entry: MockConfigEntry, empty: FakeServer
) -> None:
    _sleep(empty, local(2026, 10, 26, 15, 30), 0)
    await _setup(hass, entry)
    assert _value(hass, "asleep") == "on"
    assert _value(hass, "asleep", "baby_two") == "off"


async def test_asleep_is_off_when_the_newest_sleep_has_ended(
    hass: HomeAssistant, entry: MockConfigEntry, empty: FakeServer
) -> None:
    _sleep(empty, local(2026, 10, 26, 13), 45)
    await _setup(hass, entry)
    assert _value(hass, "asleep") == "off"


async def test_asleep_is_off_with_no_sleeps(
    hass: HomeAssistant, entry: MockConfigEntry, empty: FakeServer
) -> None:
    await _setup(hass, entry)
    assert _value(hass, "asleep") == "off"


async def test_asleep_turns_on_when_a_sleep_starts_on_another_device(
    hass: HomeAssistant, entry: MockConfigEntry, empty: FakeServer
) -> None:
    await _setup(hass, entry)
    assert _value(hass, "asleep") == "off"
    empty.add_record(DEVICE_B, "Sleep", _id(), when=NOW, duration=0)
    await tick(hass, minutes=1440)
    assert _value(hass, "asleep") == "on"


async def test_asleep_turns_off_when_the_running_sleep_ends(
    hass: HomeAssistant, entry: MockConfigEntry, empty: FakeServer
) -> None:
    started = local(2026, 10, 26, 15, 0)
    sleep = _sleep(empty, started, 0)
    await _setup(hass, entry)
    assert _value(hass, "asleep") == "on"
    empty.add_record(DEVICE_A, "Sleep", sleep, when=started, changed=NOW, duration=55, op=OP_UPDATE)
    await tick(hass, minutes=1440)
    assert _value(hass, "asleep") == "off"


async def test_deleting_the_running_sleep_turns_asleep_off(
    hass: HomeAssistant, entry: MockConfigEntry, empty: FakeServer
) -> None:
    started = local(2026, 10, 26, 15, 0)
    sleep = _sleep(empty, started, 0)
    await _setup(hass, entry)
    assert _value(hass, "asleep") == "on"
    empty.add_record(DEVICE_A, "Sleep", sleep, when=started, duration=0, op=OP_DELETE)
    await tick(hass, minutes=1440)
    assert _value(hass, "asleep") == "off"


async def test_deleting_a_running_sleep_falls_back_to_the_sleep_before_it(
    hass: HomeAssistant, entry: MockConfigEntry, empty: FakeServer
) -> None:
    _sleep(empty, local(2026, 10, 26, 9), 30)
    newer = _sleep(empty, local(2026, 10, 26, 15), 0)
    await _setup(hass, entry)
    assert _value(hass, "asleep") == "on"
    empty.add_record(DEVICE_A, "Sleep", newer, when=local(2026, 10, 26, 15), op=OP_DELETE)
    await tick(hass, minutes=1440)
    assert _value(hass, "asleep") == "off"


async def test_with_several_running_sleeps_the_newest_one_decides(
    hass: HomeAssistant, entry: MockConfigEntry, empty: FakeServer
) -> None:
    _sleep(empty, local(2026, 10, 26, 9), 0)
    _sleep(empty, local(2026, 10, 26, 15), 0)
    await _setup(hass, entry)
    assert _value(hass, "asleep") == "on"


async def test_an_older_running_sleep_does_not_count_once_a_newer_one_has_ended(
    hass: HomeAssistant, entry: MockConfigEntry, empty: FakeServer
) -> None:
    # The rule: asleep follows the baby's newest sleep, so a timer that was never stopped
    # can't hold the sensor on after a later nap finished.
    _sleep(empty, local(2026, 10, 26, 9), 0)
    _sleep(empty, local(2026, 10, 26, 13), 40)
    await _setup(hass, entry)
    assert _value(hass, "asleep") == "off"


async def test_a_running_sleep_does_not_make_another_baby_asleep(
    hass: HomeAssistant, entry: MockConfigEntry, empty: FakeServer
) -> None:
    _sleep(empty, local(2026, 10, 26, 15), 0, baby=BABY_TWO)
    await _setup(hass, entry)
    assert _value(hass, "asleep", "baby_one") == "off"
    assert _value(hass, "asleep", "baby_two") == "on"


async def test_a_sleep_running_across_midnight_keeps_asleep_on_while_the_counts_reset(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    empty: FakeServer,
    clock: FrozenDateTimeFactory,
) -> None:
    clock.move_to(local(2026, 10, 26, 23, 0))
    _sleep(empty, local(2026, 10, 26, 22, 30), 0)
    _diaper(empty, local(2026, 10, 26, 22, 0))
    await _setup(hass, entry)
    assert _value(hass, "asleep") == "on"
    assert _value(hass, "diapers_today") == "1"

    await _pass_midnight(hass, clock)
    assert _value(hass, "asleep") == "on"
    assert _value(hass, "diapers_today") == "0"


async def test_asleep_is_a_binary_sensor_on_the_baby_device(
    hass: HomeAssistant, entry: MockConfigEntry, empty: FakeServer
) -> None:
    await _setup(hass, entry)
    found = er.async_get(hass).async_get("binary_sensor.baby_one_asleep")
    assert found is not None
    assert found.unique_id == f"{BABY_ONE_ID}_asleep"
    device = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, BABY_ONE_ID)})
    assert device is not None
    assert found.device_id == device.id


async def test_the_new_entities_are_unavailable_after_a_failed_sync_and_recover(
    hass: HomeAssistant, entry: MockConfigEntry, empty: FakeServer
) -> None:
    _sleep(empty, local(2026, 10, 26, 15), 0)
    _diaper(empty, TODAY)
    await _setup(hass, entry)
    empty.request_error = BabyTrackerConnectionError("down")
    await tick(hass, minutes=1440)
    assert _value(hass, "asleep") == "unavailable"
    assert _value(hass, "diapers_today") == "unavailable"
    empty.request_error = None
    await tick(hass, minutes=1440)
    assert _value(hass, "asleep") == "on"
    assert _value(hass, "diapers_today") == "1"
