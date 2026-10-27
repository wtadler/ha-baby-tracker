"""Core PR 15: the Bottle button and the options it reads."""

from datetime import UTC, datetime

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.util.unit_system import METRIC_SYSTEM, US_CUSTOMARY_SYSTEM, UnitSystem
from pybabytracker import (
    BabyTrackerConnectionError,
    Formula,
    Pumped,
    Volume,
)
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.baby_tracker.const import (
    CONF_BOTTLE_SIZE,
    CONF_BOTTLE_TYPE,
    CONF_BOTTLE_UNIT,
)

from . import NOW, get_baby_device
from .factories import (
    BABY_ONE_ID,
    BABY_TWO_ID,
    volume,
)
from .fake_server import FakeServer
from .helpers import FEEDS_TODAY, LAST_FEED, only_record, posted, press, set_options


@pytest.fixture(autouse=True)
def _start_at_now(freezer: FrozenDateTimeFactory) -> None:
    """Freeze the clock at NOW before anything is set up.

    Timers scheduled at setup then share the clock the tests move, so moving it forward and
    ticking reaches them the way it does in core's tests.
    """
    freezer.move_to(NOW)


BOTTLE_BUTTON = "button.baby_one_bottle"


async def test_each_baby_gets_a_bottle_button(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    devices = dr.async_get(hass)
    entities = er.async_get(hass)
    for baby_id in (BABY_ONE_ID, BABY_TWO_ID):
        device = get_baby_device(devices, baby_id)
        assert device is not None
        bottle = [
            e
            for e in er.async_entries_for_device(entities, device.id)
            if e.domain == "button" and e.translation_key == "bottle"
        ]
        assert len(bottle) == 1
        assert bottle[0].unique_id == f"{baby_id}_bottle"
    assert hass.states.get(BOTTLE_BUTTON).attributes["friendly_name"] == "Baby One Bottle"
    assert hass.states.get("button.baby_two_bottle") is not None


async def test_a_bottle_press_with_a_4_oz_formula_option_logs_a_4_oz_formula_bottle(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    server: FakeServer,
    freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to(NOW)
    hass.config_entries.async_update_entry(
        init_integration,
        options={CONF_BOTTLE_SIZE: 4, CONF_BOTTLE_UNIT: "fl. oz.", CONF_BOTTLE_TYPE: "formula"},
    )

    await press(hass, BOTTLE_BUTTON)

    [(number, op, record)] = posted(server)
    assert (number, op) == (1, 0)
    assert isinstance(record, Formula)
    assert record.volume == Volume(4, "oz")
    assert record.time == NOW
    assert record.baby.object_id == BABY_ONE_ID
    assert record.to_payload()["amount"]["englishMeasure"] == "true"


@pytest.mark.parametrize(
    ("units", "volume"),
    [(US_CUSTOMARY_SYSTEM, Volume(4, "oz")), (METRIC_SYSTEM, Volume(120, "ml"))],
    ids=["us-customary", "metric"],
)
async def test_a_bottle_press_with_no_options_logs_4_oz_in_us_customary_and_120_ml_in_metric(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    server: FakeServer,
    units: UnitSystem,
    volume: Volume,
) -> None:
    hass.config.units = units

    await press(hass, BOTTLE_BUTTON)

    record = only_record(server)
    assert isinstance(record, Formula)
    assert record.volume == volume


async def test_a_bottle_press_uses_the_options_as_they_are_after_a_change(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    await press(hass, BOTTLE_BUTTON)
    await set_options(
        hass,
        init_integration,
        **{CONF_BOTTLE_SIZE: 5, CONF_BOTTLE_UNIT: "fl. oz.", CONF_BOTTLE_TYPE: "pumped"},
    )
    await press(hass, BOTTLE_BUTTON)
    await set_options(
        hass,
        init_integration,
        **{CONF_BOTTLE_SIZE: 6, CONF_BOTTLE_UNIT: "fl. oz.", CONF_BOTTLE_TYPE: "formula"},
    )
    await press(hass, BOTTLE_BUTTON)

    [(_, _, first), (_, _, second), (_, _, third)] = posted(server)
    assert isinstance(first, Formula)
    assert (type(second), second.volume) == (Pumped, Volume(5, "oz"))
    assert (type(third), third.volume) == (Formula, Volume(6, "oz"))


async def test_a_bottle_press_is_for_its_own_baby_and_counts_in_todays_totals(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    server: FakeServer,
    freezer: FrozenDateTimeFactory,
) -> None:
    await hass.config.async_set_time_zone("America/New_York")
    hass.config.units = METRIC_SYSTEM
    freezer.move_to(NOW)

    await press(hass, "button.baby_two_bottle")

    assert only_record(server).baby.object_id == BABY_TWO_ID
    assert hass.states.get("sensor.baby_two_feeds_today").state == "1"
    assert hass.states.get(FEEDS_TODAY).state == "0"
    assert float(hass.states.get("sensor.baby_two_bottle_volume_today").state) == pytest.approx(
        120, abs=0.1
    )


async def test_a_bottle_press_with_bad_saved_options_falls_back_to_the_defaults(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    hass.config.units = METRIC_SYSTEM
    hass.config_entries.async_update_entry(
        init_integration,
        options={CONF_BOTTLE_SIZE: -3, CONF_BOTTLE_UNIT: "cups", CONF_BOTTLE_TYPE: "milk"},
    )

    await press(hass, BOTTLE_BUTTON)

    record = only_record(server)
    assert isinstance(record, Formula)
    assert record.volume == Volume(120, "ml")


async def test_a_bottle_press_with_milliliter_options_logs_a_metric_bottle(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    await set_options(
        hass,
        init_integration,
        **{CONF_BOTTLE_SIZE: 90, CONF_BOTTLE_UNIT: "mL", CONF_BOTTLE_TYPE: "pumped"},
    )

    await press(hass, BOTTLE_BUTTON)

    record = only_record(server)
    assert isinstance(record, Pumped)
    assert record.volume == Volume(90, "ml")
    assert record.to_payload()["amount"] == volume(90, english=False)


async def test_saved_ounce_options_stay_ounces_in_a_metric_home(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    hass.config.units = METRIC_SYSTEM
    await set_options(
        hass,
        init_integration,
        **{CONF_BOTTLE_SIZE: 4, CONF_BOTTLE_UNIT: "fl. oz.", CONF_BOTTLE_TYPE: "formula"},
    )

    await press(hass, BOTTLE_BUTTON)

    assert only_record(server).volume == Volume(4, "oz")


async def test_a_failed_bottle_press_raises_a_translated_error_and_changes_no_sensor(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    before = hass.states.get(LAST_FEED)
    server.post_errors.append((BabyTrackerConnectionError("the service answered 500"), False))

    with pytest.raises(HomeAssistantError) as raised:
        await press(hass, BOTTLE_BUTTON)

    assert raised.value.translation_key == "write_failed"
    assert hass.states.get(LAST_FEED) == before


async def test_two_bottle_presses_make_two_records(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    await press(hass, BOTTLE_BUTTON)
    await press(hass, BOTTLE_BUTTON)

    records = [record for _, _, record in posted(server)]
    assert len({record.object_id for record in records}) == 2
    assert [n for n, _, _ in posted(server)] == [1, 2]


async def test_the_bottle_button_of_each_account_uses_that_accounts_options(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    second_entry: MockConfigEntry,
    server: FakeServer,
    second_server: FakeServer,
) -> None:
    await set_options(
        hass,
        init_integration,
        **{CONF_BOTTLE_SIZE: 7, CONF_BOTTLE_UNIT: "fl. oz.", CONF_BOTTLE_TYPE: "pumped"},
    )

    await press(hass, BOTTLE_BUTTON)
    await press(hass, "button.baby_three_bottle")

    first = only_record(server)
    second = only_record(second_server)
    assert (type(first), first.volume) == (Pumped, Volume(7, "oz"))
    assert isinstance(second, Formula)
    assert second.volume != Volume(7, "oz")


@pytest.mark.parametrize(
    ("zone", "moment", "expected"),
    [
        ("America/New_York", datetime(2026, 7, 15, 16, 0, tzinfo=UTC), -18000),
        ("America/New_York", datetime(2026, 1, 15, 16, 0, tzinfo=UTC), -18000),
        ("America/Chicago", datetime(2026, 7, 15, 16, 0, tzinfo=UTC), -21600),
        ("Asia/Kolkata", datetime(2026, 7, 15, 16, 0, tzinfo=UTC), 19800),
        ("UTC", datetime(2026, 7, 15, 16, 0, tzinfo=UTC), 0),
        ("Australia/Sydney", datetime(2026, 1, 15, 16, 0, tzinfo=UTC), 36000),
    ],
    ids=["new-york-july", "new-york-january", "chicago-july", "no-dst", "utc", "southern-dst"],
)
async def test_a_press_sends_the_zones_standard_offset_year_round(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    server: FakeServer,
    freezer: FrozenDateTimeFactory,
    zone: str,
    moment: datetime,
    expected: int,
) -> None:
    await hass.config.async_set_time_zone(zone)
    freezer.move_to(moment)

    await press(hass, BOTTLE_BUTTON)

    assert only_record(server).timezone == expected
