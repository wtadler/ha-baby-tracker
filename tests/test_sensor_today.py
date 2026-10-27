"""Core PR 7: the today sensors (counts and volume), with the midnight rollover."""

from collections.abc import Iterator
from datetime import datetime
from typing import Any

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.util.unit_system import METRIC_SYSTEM, US_CUSTOMARY_SYSTEM
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from . import BABY_ONE, BABY_TWO, NOW, get_baby_device, tick
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
from .fake_server import FakeServer
from .helpers import (
    LOCAL_NOW,
    MIDNIGHT,
    TODAY,
    baby_state,
    baby_value,
    local,
    pass_midnight,
    setup_loaded,
)

BABY_THREE = baby_payload("Baby Three", BABY_THREE_ID)


ML_PER_OZ = 29.5735295625


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


def _diaper(
    server: FakeServer, when: datetime, status: int = 0, baby: dict[str, Any] = BABY_ONE, **kw: Any
) -> str:
    object_id = _id()
    server.add_record(DEVICE_A, "Diaper", object_id, when=when, status=status, baby=baby, **kw)
    return object_id


def _breakdown(state: State) -> tuple[int, int, int]:
    attributes = state.attributes
    return attributes["wet"], attributes["dirty"], attributes["mixed"]


@pytest.fixture(autouse=True)
def _start_at_now(freezer: FrozenDateTimeFactory) -> None:
    """Freeze the clock at NOW before anything is set up.

    Timers scheduled at setup then share the clock the tests move, so moving it forward and
    ticking reaches them the way it does in core's tests.
    """
    freezer.move_to(NOW)


async def test_each_baby_gets_the_four_new_entities(
    hass: HomeAssistant, entry: MockConfigEntry, empty: FakeServer
) -> None:
    await setup_loaded(hass, entry)
    registry = er.async_get(hass)
    devices = dr.async_get(hass)
    for baby_id in (BABY_ONE_ID, BABY_TWO_ID):
        device = get_baby_device(devices, baby_id)
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
    await setup_loaded(hass, entry)
    names = {
        "diapers_today": "Diapers today",
        "feeds_today": "Feeds today",
        "bottle_volume_today": "Bottle volume today",
        "asleep": "Asleep",
    }
    for baby, name in (("baby_one", "Baby One"), ("baby_two", "Baby Two")):
        for key, label in names.items():
            assert baby_state(hass, key, baby).attributes["friendly_name"] == f"{name} {label}"


async def test_the_volume_sensor_is_a_volume(
    hass: HomeAssistant, entry: MockConfigEntry, empty: FakeServer
) -> None:
    await setup_loaded(hass, entry)
    assert baby_state(hass, "bottle_volume_today").attributes["device_class"] == "volume"


async def test_three_diapers_today_and_two_yesterday_count_three(
    hass: HomeAssistant, entry: MockConfigEntry, empty: FakeServer
) -> None:
    for status in (0, 1, 2):
        _diaper(empty, TODAY, status)
    for status in (0, 1):
        _diaper(empty, YESTERDAY, status)
    await setup_loaded(hass, entry)
    state = baby_state(hass, "diapers_today")
    assert state.state == "3"
    assert _breakdown(state) == (1, 1, 1)


async def test_the_diaper_attributes_break_the_count_down_by_type(
    hass: HomeAssistant, entry: MockConfigEntry, empty: FakeServer
) -> None:
    for status in (0, 0, 0, 1, 1, 2):
        _diaper(empty, TODAY, status)
    await setup_loaded(hass, entry)
    state = baby_state(hass, "diapers_today")
    assert state.state == "6"
    assert _breakdown(state) == (3, 2, 1)


async def test_an_unrecognized_diaper_status_counts_in_the_total_only(
    hass: HomeAssistant, entry: MockConfigEntry, empty: FakeServer
) -> None:
    _diaper(empty, TODAY, 0)
    _diaper(empty, TODAY, 9)
    await setup_loaded(hass, entry)
    state = baby_state(hass, "diapers_today")
    assert state.state == "2"
    assert _breakdown(state) == (1, 0, 0)


async def test_babies_do_not_share_counts(
    hass: HomeAssistant, entry: MockConfigEntry, empty: FakeServer
) -> None:
    _diaper(empty, TODAY, 0)
    _diaper(empty, TODAY, 1, baby=BABY_TWO)
    _diaper(empty, TODAY, 2, baby=BABY_TWO)
    await setup_loaded(hass, entry)
    assert baby_value(hass, "diapers_today", "baby_one") == "1"
    assert baby_value(hass, "diapers_today", "baby_two") == "2"


async def test_a_diaper_from_another_device_is_counted_after_the_next_sync(
    hass: HomeAssistant, entry: MockConfigEntry, empty: FakeServer
) -> None:
    await setup_loaded(hass, entry)
    assert baby_value(hass, "diapers_today") == "0"
    empty.add_record(DEVICE_B, "Diaper", _id(), when=TODAY, status=1)
    await tick(hass, minutes=1440)
    state = baby_state(hass, "diapers_today")
    assert state.state == "1"
    assert _breakdown(state) == (0, 1, 0)


@pytest.mark.engine
async def test_a_diaper_dated_today_but_deleted_is_not_counted(
    hass: HomeAssistant, entry: MockConfigEntry, empty: FakeServer
) -> None:
    _diaper(empty, TODAY, 0)
    gone = _diaper(empty, TODAY, 1)
    empty.add_record(DEVICE_A, "Diaper", gone, when=TODAY, status=1, op=OP_DELETE)
    await setup_loaded(hass, entry)
    state = baby_state(hass, "diapers_today")
    assert state.state == "1"
    assert _breakdown(state) == (1, 0, 0)


@pytest.mark.engine
async def test_a_diaper_deleted_after_the_first_sync_drops_out_of_the_count(
    hass: HomeAssistant, entry: MockConfigEntry, empty: FakeServer
) -> None:
    gone = _diaper(empty, TODAY, 1)
    await setup_loaded(hass, entry)
    assert baby_value(hass, "diapers_today") == "1"
    empty.add_record(DEVICE_A, "Diaper", gone, when=TODAY, status=1, op=OP_DELETE)
    await tick(hass, minutes=1440)
    assert baby_value(hass, "diapers_today") == "0"


@pytest.mark.engine
async def test_a_diaper_edited_to_another_day_leaves_the_count(
    hass: HomeAssistant, entry: MockConfigEntry, empty: FakeServer
) -> None:
    object_id = _diaper(empty, TODAY, 0)
    await setup_loaded(hass, entry)
    assert baby_value(hass, "diapers_today") == "1"
    empty.add_record(
        DEVICE_A, "Diaper", object_id, when=YESTERDAY, changed=LOCAL_NOW, status=0, op=OP_UPDATE
    )
    await tick(hass, minutes=1440)
    assert baby_value(hass, "diapers_today") == "0"


async def test_the_feed_count_covers_nursing_formula_and_pumped(
    hass: HomeAssistant, entry: MockConfigEntry, empty: FakeServer
) -> None:
    empty.add_record(DEVICE_A, "Nursing", _id(), when=TODAY, leftDuration=5, rightDuration=3)
    empty.add_record(DEVICE_A, "Formula", _id(), when=TODAY, amount=volume(4))
    empty.add_record(DEVICE_A, "Pumped", _id(), when=TODAY, amount=volume(90, english=False))
    empty.add_record(DEVICE_A, "Nursing", _id(), when=YESTERDAY, leftDuration=5)
    await setup_loaded(hass, entry)
    assert baby_value(hass, "feeds_today") == "3"


async def test_other_feeds_pump_sessions_and_diapers_are_not_feeds(
    hass: HomeAssistant, entry: MockConfigEntry, empty: FakeServer
) -> None:
    empty.add_record(DEVICE_A, "OtherFeed", _id(), when=TODAY, amount=2.5)
    empty.add_record(DEVICE_A, "Pump", _id(), when=TODAY, amount=volume(100, english=False))
    _diaper(empty, TODAY)
    empty.add_record(DEVICE_A, "Sleep", _id(), when=TODAY, duration=30)
    await setup_loaded(hass, entry)
    assert baby_value(hass, "feeds_today") == "0"


@pytest.mark.engine
async def test_a_deleted_feed_is_not_counted(
    hass: HomeAssistant, entry: MockConfigEntry, empty: FakeServer
) -> None:
    gone = _id()
    empty.add_record(DEVICE_A, "Formula", gone, when=TODAY, amount=volume(4))
    empty.add_record(DEVICE_A, "Formula", gone, when=TODAY, amount=volume(4), op=OP_DELETE)
    empty.add_record(DEVICE_A, "Nursing", _id(), when=TODAY, leftDuration=5)
    await setup_loaded(hass, entry)
    assert baby_value(hass, "feeds_today") == "1"


async def test_the_feed_count_does_not_mix_babies(
    hass: HomeAssistant, entry: MockConfigEntry, empty: FakeServer
) -> None:
    empty.add_record(DEVICE_A, "Nursing", _id(), when=TODAY, leftDuration=5)
    empty.add_record(DEVICE_A, "Nursing", _id(), when=TODAY, baby=BABY_TWO, leftDuration=5)
    empty.add_record(DEVICE_A, "Nursing", _id(), when=TODAY, baby=BABY_TWO, leftDuration=5)
    await setup_loaded(hass, entry)
    assert baby_value(hass, "feeds_today", "baby_one") == "1"
    assert baby_value(hass, "feeds_today", "baby_two") == "2"


async def test_mixed_milliliter_and_ounce_bottles_sum_in_milliliters_for_metric(
    hass: HomeAssistant, entry: MockConfigEntry, empty: FakeServer
) -> None:
    hass.config.units = METRIC_SYSTEM
    empty.add_record(DEVICE_A, "Formula", _id(), when=TODAY, amount=volume(120, english=False))
    empty.add_record(DEVICE_A, "Pumped", _id(), when=TODAY, amount=volume(4))
    await setup_loaded(hass, entry)
    state = baby_state(hass, "bottle_volume_today")
    assert state.attributes["unit_of_measurement"] == "mL"
    assert float(state.state) == pytest.approx(120 + 4 * ML_PER_OZ, abs=0.1)


async def test_mixed_milliliter_and_ounce_bottles_sum_in_ounces_for_us_customary(
    hass: HomeAssistant, entry: MockConfigEntry, empty: FakeServer
) -> None:
    hass.config.units = US_CUSTOMARY_SYSTEM
    empty.add_record(DEVICE_A, "Formula", _id(), when=TODAY, amount=volume(120, english=False))
    empty.add_record(DEVICE_A, "Pumped", _id(), when=TODAY, amount=volume(4))
    await setup_loaded(hass, entry)
    state = baby_state(hass, "bottle_volume_today")
    assert state.attributes["unit_of_measurement"] == "fl. oz."
    assert float(state.state) == pytest.approx(120 / ML_PER_OZ + 4, abs=0.01)


async def test_one_ounce_bottle_reads_as_ounces_for_us_customary(
    hass: HomeAssistant, entry: MockConfigEntry, empty: FakeServer
) -> None:
    hass.config.units = US_CUSTOMARY_SYSTEM
    empty.add_record(DEVICE_A, "Formula", _id(), when=TODAY, amount=volume(5))
    await setup_loaded(hass, entry)
    state = baby_state(hass, "bottle_volume_today")
    assert state.attributes["unit_of_measurement"] == "fl. oz."
    assert float(state.state) == pytest.approx(5, abs=0.01)


async def test_only_bottles_from_today_are_summed(
    hass: HomeAssistant, entry: MockConfigEntry, empty: FakeServer
) -> None:
    hass.config.units = METRIC_SYSTEM
    empty.add_record(DEVICE_A, "Formula", _id(), when=TODAY, amount=volume(100, english=False))
    empty.add_record(DEVICE_A, "Formula", _id(), when=YESTERDAY, amount=volume(500, english=False))
    await setup_loaded(hass, entry)
    assert float(baby_value(hass, "bottle_volume_today")) == pytest.approx(100)


async def test_pump_sessions_nursing_and_other_feeds_add_no_volume(
    hass: HomeAssistant, entry: MockConfigEntry, empty: FakeServer
) -> None:
    hass.config.units = METRIC_SYSTEM
    empty.add_record(DEVICE_A, "Pump", _id(), when=TODAY, amount=volume(200, english=False))
    empty.add_record(DEVICE_A, "OtherFeed", _id(), when=TODAY, amount=50.0)
    empty.add_record(DEVICE_A, "Nursing", _id(), when=TODAY, leftDuration=5)
    empty.add_record(DEVICE_A, "Formula", _id(), when=TODAY, amount=volume(60, english=False))
    await setup_loaded(hass, entry)
    assert float(baby_value(hass, "bottle_volume_today")) == pytest.approx(60)


async def test_a_bottle_with_no_readable_unit_counts_as_a_feed_but_adds_no_volume(
    hass: HomeAssistant, entry: MockConfigEntry, empty: FakeServer
) -> None:
    hass.config.units = METRIC_SYSTEM
    # A plain number with no scale has no unit, so it can't be added to anything.
    empty.add_record(DEVICE_A, "Formula", _id(), when=TODAY, amount=3)
    empty.add_record(DEVICE_A, "Formula", _id(), when=TODAY, amount=volume(60, english=False))
    await setup_loaded(hass, entry)
    assert baby_value(hass, "feeds_today") == "2"
    assert float(baby_value(hass, "bottle_volume_today")) == pytest.approx(60)


@pytest.mark.engine
async def test_a_deleted_bottle_is_not_summed(
    hass: HomeAssistant, entry: MockConfigEntry, empty: FakeServer
) -> None:
    hass.config.units = METRIC_SYSTEM
    gone = _id()
    empty.add_record(DEVICE_A, "Formula", gone, when=TODAY, amount=volume(100, english=False))
    empty.add_record(
        DEVICE_A, "Formula", gone, when=TODAY, amount=volume(100, english=False), op=OP_DELETE
    )
    await setup_loaded(hass, entry)
    assert float(baby_value(hass, "bottle_volume_today")) == 0


@pytest.mark.engine
async def test_a_bottle_edited_to_a_new_amount_changes_the_total(
    hass: HomeAssistant, entry: MockConfigEntry, empty: FakeServer
) -> None:
    hass.config.units = METRIC_SYSTEM
    bottle = _id()
    empty.add_record(DEVICE_A, "Formula", bottle, when=TODAY, amount=volume(100, english=False))
    await setup_loaded(hass, entry)
    empty.add_record(
        DEVICE_A,
        "Formula",
        bottle,
        when=TODAY,
        changed=LOCAL_NOW,
        amount=volume(80, english=False),
        op=OP_UPDATE,
    )
    await tick(hass, minutes=1440)
    assert float(baby_value(hass, "bottle_volume_today")) == pytest.approx(80)


async def test_a_baby_with_no_records_has_zero_counts_and_is_awake(
    hass: HomeAssistant, entry: MockConfigEntry, empty: FakeServer
) -> None:
    empty.add_payload(DEVICE_A, BABY_THREE)
    await setup_loaded(hass, entry)
    assert baby_value(hass, "diapers_today", "baby_three") == "0"
    assert baby_value(hass, "feeds_today", "baby_three") == "0"
    assert float(baby_value(hass, "bottle_volume_today", "baby_three")) == 0
    assert baby_value(hass, "asleep", "baby_three") == "off"
    assert _breakdown(baby_state(hass, "diapers_today", "baby_three")) == (0, 0, 0)


async def test_a_baby_whose_records_are_all_old_has_zero_counts(
    hass: HomeAssistant, entry: MockConfigEntry, empty: FakeServer
) -> None:
    _diaper(empty, YESTERDAY)
    empty.add_record(DEVICE_A, "Formula", _id(), when=YESTERDAY, amount=volume(4))
    await setup_loaded(hass, entry)
    assert baby_value(hass, "diapers_today") == "0"
    assert baby_value(hass, "feeds_today") == "0"


@pytest.mark.engine
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
    await setup_loaded(hass, entry)
    assert baby_value(hass, "diapers_today") == "1"
    assert baby_value(hass, "feeds_today") == "0"
    assert float(baby_value(hass, "bottle_volume_today")) == 0
    assert baby_value(hass, "asleep") == "off"


async def test_the_counts_reset_at_local_midnight_without_a_sync(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    empty: FakeServer,
    clock: FrozenDateTimeFactory,
) -> None:
    hass.config.units = METRIC_SYSTEM
    _diaper(empty, TODAY)
    empty.add_record(DEVICE_A, "Formula", _id(), when=TODAY, amount=volume(100, english=False))
    await setup_loaded(hass, entry)
    assert baby_value(hass, "diapers_today") == "1"
    assert baby_value(hass, "feeds_today") == "1"
    reads = len(empty.transaction_reads)
    listed = empty.device_list_calls

    await pass_midnight(hass, clock)

    assert baby_value(hass, "diapers_today") == "0"
    assert baby_value(hass, "feeds_today") == "0"
    assert float(baby_value(hass, "bottle_volume_today")) == 0
    assert _breakdown(baby_state(hass, "diapers_today")) == (0, 0, 0)
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
    await setup_loaded(hass, entry)
    clock.move_to(local(2026, 10, 26, 23, 59, 0))
    async_fire_time_changed(hass, local(2026, 10, 26, 23, 59, 0))
    await hass.async_block_till_done()
    assert baby_value(hass, "diapers_today") == "1"


async def test_the_counts_reset_again_every_midnight(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    empty: FakeServer,
    clock: FrozenDateTimeFactory,
) -> None:
    _diaper(empty, local(2026, 10, 27, 8))
    await setup_loaded(hass, entry)
    assert baby_value(hass, "diapers_today") == "0"
    await pass_midnight(hass, clock, MIDNIGHT)
    assert baby_value(hass, "diapers_today") == "1"
    await pass_midnight(hass, clock, local(2026, 10, 28))
    assert baby_value(hass, "diapers_today") == "0"


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
    await setup_loaded(hass, entry)
    assert baby_value(hass, "diapers_today") == "1"

    await pass_midnight(hass, clock)
    assert baby_value(hass, "diapers_today") == "1"  # now the record at midnight, and only it


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
    await setup_loaded(hass, entry)
    assert baby_value(hass, "diapers_today") == "2"

    await pass_midnight(hass, clock, local(2026, 11, 2))
    assert baby_value(hass, "diapers_today") == "1"


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
    await setup_loaded(hass, entry)
    assert baby_value(hass, "diapers_today") == "2"

    await pass_midnight(hass, clock, local(2026, 3, 9))
    assert baby_value(hass, "diapers_today") == "1"
