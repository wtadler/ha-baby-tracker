"""Core PR 10: log_bottle, log_nursing, and log_sleep."""

import math
from typing import Any

import probatio
import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.util.unit_system import METRIC_SYSTEM, US_CUSTOMARY_SYSTEM, UnitSystem
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.nighp_baby_tracker.pybabytracker import (
    Formula,
    Nursing,
    Pumped,
    Sleep,
    Volume,
)

from . import NOW
from .factories import volume
from .fake_server import FakeServer
from .helpers import ASLEEP, BOTTLE_TODAY, FEEDS_TODAY, LAST_SLEEP, call, only_record, posted


@pytest.fixture(autouse=True)
def _start_at_now(freezer: FrozenDateTimeFactory) -> None:
    """Freeze the clock at NOW before anything is set up.

    Timers scheduled at setup then share the clock the tests move, so moving it forward and
    ticking reaches them the way it does in core's tests.
    """
    freezer.move_to(NOW)


def key_of(raised: pytest.ExceptionInfo[ServiceValidationError]) -> str | None:
    return raised.value.translation_key


async def test_log_bottle_writes_formula_or_pumped_by_kind(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    await call(hass, "log_bottle", kind="formula", amount=4, unit="fl. oz.")
    await call(hass, "log_bottle", kind="pumped", amount=3, unit="fl. oz.")

    [(_, _, first), (_, _, second)] = posted(server)
    assert isinstance(first, Formula)
    assert isinstance(second, Pumped)
    assert first.volume == Volume(4, "oz")
    assert second.volume == Volume(3, "oz")


@pytest.mark.parametrize(
    ("action", "data", "key"),
    [
        ("log_bottle", {"kind": "formula", "amount": 120}, "amount"),
        ("log_bottle", {"kind": "pumped", "amount": 90}, "amount"),
        ("log_pump", {"left_amount": 60, "right_amount": 75}, "leftAmount"),
        ("log_pump", {"left_amount": 60}, "leftAmount"),
    ],
    ids=["formula", "pumped", "pump", "pump-one-side"],
)
async def test_a_milliliter_write_is_accepted_and_sent_as_metric(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    server: FakeServer,
    action: str,
    data: dict[str, Any],
    key: str,
) -> None:
    await call(hass, action, unit="mL", **data)

    payload = only_record(server).to_payload()
    first = data.get("amount", data.get("left_amount"))
    assert payload[key] == volume(first, english=False)


async def test_log_bottle_in_milliliters_writes_the_value_as_given(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    await call(hass, "log_bottle", kind="formula", amount=118.5, unit="mL")

    record = only_record(server)
    assert record.volume == Volume(118.5, "ml")
    assert record.to_payload()["amount"]["englishMeasure"] == "false"
    assert record.to_payload()["amount"]["value"] == 118.5


async def test_log_bottle_in_ounces_writes_english_measure_true(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    await call(hass, "log_bottle", kind="formula", amount=2.5, unit="fl. oz.")

    assert only_record(server).to_payload()["amount"] == volume(2.5)


@pytest.mark.parametrize(
    ("units", "unit", "expected"),
    [
        (US_CUSTOMARY_SYSTEM, "fl. oz.", Volume(5, "oz")),
        (METRIC_SYSTEM, "fl. oz.", Volume(5, "oz")),
    ],
    ids=["us-customary", "metric"],
)
async def test_log_bottle_without_a_unit_uses_fluid_ounces_in_any_unit_system(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    server: FakeServer,
    units: UnitSystem,
    unit: str,
    expected: Volume,
) -> None:
    hass.config.units = units

    await call(hass, "log_bottle", kind="formula", amount=5)

    assert only_record(server).volume == expected


async def test_log_bottle_time_defaults_to_now_and_reads_a_naive_time_in_the_ha_zone(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    server: FakeServer,
    freezer: FrozenDateTimeFactory,
) -> None:
    await hass.config.async_set_time_zone("America/New_York")
    freezer.move_to(NOW)

    await call(hass, "log_bottle", kind="formula", amount=4, unit="fl. oz.")
    await call(
        hass, "log_bottle", kind="formula", amount=4, unit="fl. oz.", time="2026-10-26T09:00:00"
    )

    [(_, _, now_record), (_, _, naive_record)] = posted(server)
    assert now_record.time == NOW
    assert naive_record.time.isoformat() == "2026-10-26T13:00:00+00:00"
    assert (now_record.timezone, naive_record.timezone) == (-18000, -18000)


async def test_log_nursing_with_only_one_side_writes_zero_for_the_other(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    await call(hass, "log_nursing", left_minutes=12, end_time="2026-10-26T12:30:00+00:00")

    record = only_record(server)
    assert isinstance(record, Nursing)
    assert (record.left_duration, record.right_duration, record.finish_side) == (12, 0, 0)
    assert record.time.isoformat() == "2026-10-26T12:18:00+00:00"  # the start


async def test_log_nursing_without_an_end_time_ends_now(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    server: FakeServer,
    freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to(NOW)

    await call(hass, "log_nursing", right_minutes=10)

    record = only_record(server)
    assert record.time.isoformat() == "2026-10-26T14:50:00+00:00"
    assert record.finish_side == 1


async def test_log_nursing_reads_a_naive_end_time_in_the_ha_zone(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    await hass.config.async_set_time_zone("America/New_York")

    await call(hass, "log_nursing", left_minutes=10, end_time="2026-10-26T09:10:00")

    record = only_record(server)
    assert record.time.isoformat() == "2026-10-26T13:00:00+00:00"
    assert record.timezone == -18000


async def test_log_sleep_writes_the_start_and_a_whole_minute_duration(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    await call(
        hass, "log_sleep", start="2026-10-26T01:00:00+00:00", end="2026-10-26T03:30:30+00:00"
    )

    record = only_record(server)
    assert isinstance(record, Sleep)
    assert record.time.isoformat() == "2026-10-26T01:00:00+00:00"
    assert record.duration == 151  # 150.5 minutes, rounded half up
    assert record.is_running is False


async def test_log_sleep_reads_naive_times_in_the_ha_zone(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    await hass.config.async_set_time_zone("America/New_York")

    await call(hass, "log_sleep", start="2026-10-25T20:00:00", end="2026-10-26T06:00:00")

    record = only_record(server)
    assert record.time.isoformat() == "2026-10-26T00:00:00+00:00"
    assert record.duration == 600
    assert record.timezone == -18000


@pytest.mark.parametrize("amount", [-1, 0, -0.5, math.inf, math.nan])
async def test_log_bottle_with_a_non_positive_or_non_finite_amount_is_a_validation_error(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    server: FakeServer,
    amount: float,
) -> None:
    sent = len(server.calls)
    with pytest.raises(ServiceValidationError) as raised:
        await call(hass, "log_bottle", kind="formula", amount=amount, unit="fl. oz.")
    assert key_of(raised) == "invalid_amount"
    assert server.posted == []
    assert len(server.calls) == sent  # not even a sync


async def test_log_bottle_with_a_negative_amount_gives_the_translated_amount_error(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    with pytest.raises(ServiceValidationError) as raised:
        await call(hass, "log_bottle", kind="formula", amount=-1, unit="fl. oz.")
    assert key_of(raised) == "invalid_amount"


@pytest.mark.parametrize("kind", ["milk", "", "FORMULA", "nursing"])
async def test_log_bottle_with_an_unknown_kind_is_rejected(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, kind: str
) -> None:
    with pytest.raises(probatio.Invalid):
        await call(hass, "log_bottle", kind=kind, amount=4, unit="fl. oz.")
    assert server.posted == []


@pytest.mark.parametrize("unit", ["cups", "oz", "ml", "", "L"])
async def test_log_bottle_with_an_unknown_unit_is_rejected(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, unit: str
) -> None:
    with pytest.raises(probatio.Invalid):
        await call(hass, "log_bottle", kind="formula", amount=4, unit=unit)
    assert server.posted == []


async def test_log_bottle_needs_a_kind_and_an_amount(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    with pytest.raises(probatio.Invalid):
        await call(hass, "log_bottle", amount=4)
    with pytest.raises(probatio.Invalid):
        await call(hass, "log_bottle", kind="formula")


@pytest.mark.parametrize(
    ("start", "end"),
    [
        ("2026-10-26T03:00:00+00:00", "2026-10-26T02:00:00+00:00"),
        ("2026-10-26T03:00:00+00:00", "2026-10-26T03:00:00+00:00"),
        ("2026-10-26T03:00:00+00:00", "2026-10-26T02:59:59+00:00"),
    ],
    ids=["end-before-start", "end-equals-start", "one-second-early"],
)
async def test_log_sleep_with_the_end_before_the_start_is_a_validation_error(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, start: str, end: str
) -> None:
    sent = len(server.calls)
    with pytest.raises(ServiceValidationError) as raised:
        await call(hass, "log_sleep", start=start, end=end)
    assert key_of(raised) == "end_before_start"
    assert server.posted == []
    assert len(server.calls) == sent


async def test_log_sleep_with_a_naive_end_before_a_zoned_start_is_a_validation_error(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    await hass.config.async_set_time_zone("America/New_York")
    with pytest.raises(ServiceValidationError) as raised:
        # 02:00 in New York is 06:00 UTC: before the 07:00 UTC start.
        await call(hass, "log_sleep", start="2026-10-26T07:00:00+00:00", end="2026-10-26T02:00:00")
    assert key_of(raised) == "end_before_start"


@pytest.mark.parametrize("missing", ["start", "end"])
async def test_log_sleep_needs_both_times(
    hass: HomeAssistant, init_integration: MockConfigEntry, missing: str
) -> None:
    data = {"start": "2026-10-26T01:00:00+00:00", "end": "2026-10-26T02:00:00+00:00"}
    del data[missing]
    with pytest.raises(probatio.Invalid):
        await call(hass, "log_sleep", **data)


async def test_log_nursing_with_no_minutes_is_a_validation_error(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    with pytest.raises(ServiceValidationError) as raised:
        await call(hass, "log_nursing")
    assert key_of(raised) == "no_duration"
    with pytest.raises(ServiceValidationError):
        await call(hass, "log_nursing", left_minutes=0, right_minutes=0)
    assert server.posted == []


@pytest.mark.parametrize("minutes", [-1, 2.5, "ten"])
async def test_log_nursing_rejects_negative_or_fractional_minutes(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, minutes: float | str
) -> None:
    with pytest.raises(probatio.Invalid):
        await call(hass, "log_nursing", left_minutes=minutes)
    assert server.posted == []


async def test_log_nursing_rejects_an_unknown_finish_side(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    with pytest.raises(probatio.Invalid):
        await call(hass, "log_nursing", left_minutes=5, finish_side="both")


async def test_a_sleep_updates_the_last_sleep_sensor_and_leaves_the_baby_awake(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    assert hass.states.get(ASLEEP).state == "off"

    await call(
        hass, "log_sleep", start="2026-10-26T01:00:00+00:00", end="2026-10-26T02:30:00+00:00"
    )

    state = hass.states.get(LAST_SLEEP)
    assert state.state == "2026-10-26T01:00:00+00:00"
    assert state.attributes["duration_minutes"] == 90
    assert hass.states.get(ASLEEP).state == "off"


async def test_nursing_counts_as_a_feed_today_and_adds_no_bottle_volume(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    server: FakeServer,
    freezer: FrozenDateTimeFactory,
) -> None:
    await hass.config.async_set_time_zone("America/New_York")
    freezer.move_to(NOW)

    await call(hass, "log_nursing", left_minutes=10)

    assert hass.states.get(FEEDS_TODAY).state == "1"
    assert float(hass.states.get(BOTTLE_TODAY).state) == 0
