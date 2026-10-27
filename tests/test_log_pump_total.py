"""log_pump with a total: ``amount`` (with ``unit``) and ``minutes``, the way most pumps are logged.

The app writes most pumps with only a total ``amount`` and no per-side keys, and a timed one with
equal ``leftDuration`` and ``rightDuration``. Everything here is invented fixture data.
"""

from typing import Any

import probatio
import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.nighp_baby_tracker.pybabytracker import OP_ADD, Pump, Volume

from .fake_server import FakeServer
from .helpers import LAST_PUMP, NOW, log_pump, posted, state

# The keys of a total-only pump: the live shape, with no per-side amounts and no baby.
TOTAL_ONLY_KEYS = {
    "BCObjectType",
    "amount",
    "finishSide",
    "leftDuration",
    "newFlage",
    "note",
    "objectID",
    "pictureLoaded",
    "pictureNote",
    "rightDuration",
    "sides",
    "time",
    "timestamp",
    "timezone",
}


@pytest.fixture(autouse=True)
def _start_at_now(freezer: Any) -> None:
    freezer.move_to(NOW)


def only_pump(server: FakeServer) -> Pump:
    [(_, op, record)] = posted(server)
    assert op == OP_ADD
    assert isinstance(record, Pump)
    return record


# --- amount -----------------------------------------------------------------------------------


async def test_amount_and_unit_alone_post_one_total_only_pump(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    await log_pump(hass, config_entry_id=init_integration.entry_id, amount=5, unit="fl. oz.")

    record = only_pump(server)
    payload = record.to_payload()
    assert set(payload) == TOTAL_ONLY_KEYS
    assert "leftAmount" not in payload
    assert "rightAmount" not in payload
    assert "baby" not in payload
    assert record.volume == Volume(5, "oz")
    assert (payload["leftDuration"], payload["rightDuration"]) == (0, 0)
    assert payload["sides"] == 0


async def test_a_millilitre_total_is_written_in_millilitres(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    await log_pump(hass, config_entry_id=init_integration.entry_id, amount=150, unit="mL")

    record = only_pump(server)
    assert record.volume == Volume(150, "ml")
    assert record.to_payload()["amount"]["englishMeasure"] == "false"
    assert set(record.to_payload()) == TOTAL_ONLY_KEYS


async def test_the_unit_defaults_to_fluid_ounces_for_a_total(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    await log_pump(hass, config_entry_id=init_integration.entry_id, amount=4.25)

    record = only_pump(server)
    assert record.volume == Volume(4.25, "oz")
    assert record.to_payload()["amount"]["englishMeasure"] == "true"


async def test_a_total_shows_on_the_last_pump_sensor_without_side_amounts(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    await log_pump(
        hass,
        config_entry_id=init_integration.entry_id,
        amount=6,
        unit="fl. oz.",
        time="2026-10-26T08:00:00+00:00",
    )

    last = state(hass, LAST_PUMP)
    assert last.state == "2026-10-26T08:00:00+00:00"
    assert last.attributes["amount"] == 6
    assert last.attributes["unit"] == "fl. oz."
    assert last.attributes["left_amount"] is None
    assert last.attributes["right_amount"] is None


# --- minutes ----------------------------------------------------------------------------------


@pytest.mark.parametrize("unit", ["fl. oz.", "mL"])
async def test_amount_and_minutes_post_equal_durations(
    unit: str, hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    await log_pump(hass, config_entry_id=init_integration.entry_id, amount=4, unit=unit, minutes=17)

    payload = only_pump(server).to_payload()
    assert (payload["leftDuration"], payload["rightDuration"]) == (17, 17)
    assert set(payload) == TOTAL_ONLY_KEYS
    assert payload["amount"]["englishMeasure"] == ("true" if unit == "fl. oz." else "false")


async def test_minutes_of_zero_are_allowed_and_write_zero_durations(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    await log_pump(hass, config_entry_id=init_integration.entry_id, amount=3, minutes=0)

    payload = only_pump(server).to_payload()
    assert (payload["leftDuration"], payload["rightDuration"]) == (0, 0)
    assert "leftAmount" not in payload


async def test_the_largest_minutes_are_accepted_and_one_more_is_refused(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    await log_pump(hass, config_entry_id=init_integration.entry_id, amount=3, minutes=240)
    assert only_pump(server).left_duration == 240
    server.posted.clear()

    with pytest.raises(ServiceValidationError) as raised:
        await log_pump(hass, config_entry_id=init_integration.entry_id, amount=3, minutes=241)

    assert raised.value.translation_key == "minutes_too_long"
    assert server.posted == []


async def test_the_start_time_and_finish_side_still_apply_to_a_total(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    await log_pump(
        hass,
        config_entry_id=init_integration.entry_id,
        amount=3,
        minutes=10,
        finish_side="left",
        time="2026-10-26T08:00:00+00:00",
        note="invented note",
    )

    record = only_pump(server)
    assert record.time.isoformat() == "2026-10-26T08:00:00+00:00"
    assert record.finish_side == 0
    assert record.note == "invented note"


async def test_a_retry_with_the_same_record_id_posts_once(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    for _ in range(2):
        await log_pump(
            hass,
            config_entry_id=init_integration.entry_id,
            amount=3,
            minutes=5,
            object_id="AB000000-0000-4000-8000-0000000000A2",
        )

    assert only_pump(server).object_id == "AB000000-0000-4000-8000-0000000000A2"


async def test_minutes_alone_still_need_an_amount(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    with pytest.raises(ServiceValidationError) as raised:
        await log_pump(hass, config_entry_id=init_integration.entry_id, minutes=10)

    assert raised.value.translation_key == "no_amount"
    assert server.posted == []


# --- conflicts --------------------------------------------------------------------------------


@pytest.mark.parametrize("side", ["left_amount", "right_amount"])
async def test_amount_with_a_side_amount_is_a_validation_error_and_posts_nothing(
    side: str, hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    with pytest.raises(ServiceValidationError) as raised:
        await log_pump(hass, config_entry_id=init_integration.entry_id, amount=5, **{side: 2})

    assert raised.value.translation_key == "amount_with_sides"
    assert server.posted == []


@pytest.mark.parametrize("side", ["left_minutes", "right_minutes"])
@pytest.mark.parametrize("value", [0, 5])
async def test_minutes_with_a_side_minutes_is_a_validation_error_even_at_zero(
    side: str,
    value: int,
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    server: FakeServer,
) -> None:
    with pytest.raises(ServiceValidationError) as raised:
        await log_pump(
            hass,
            config_entry_id=init_integration.entry_id,
            amount=5,
            minutes=10,
            **{side: value},
        )

    assert raised.value.translation_key == "minutes_with_sides"
    assert server.posted == []


async def test_minutes_with_side_minutes_is_refused_for_side_amounts_too(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    with pytest.raises(ServiceValidationError) as raised:
        await log_pump(
            hass,
            config_entry_id=init_integration.entry_id,
            left_amount=2,
            minutes=10,
            left_minutes=4,
        )

    assert raised.value.translation_key == "minutes_with_sides"
    assert server.posted == []


async def test_amount_with_side_amounts_and_minutes_with_side_minutes_gives_the_amount_error(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    with pytest.raises(ServiceValidationError) as raised:
        await log_pump(
            hass,
            config_entry_id=init_integration.entry_id,
            amount=5,
            left_amount=2,
            minutes=10,
            left_minutes=4,
        )

    assert raised.value.translation_key == "amount_with_sides"
    assert server.posted == []


# --- bad values -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("amount", "key"),
    [
        (0, "invalid_amount"),
        (-1, "invalid_amount"),
        (float("nan"), "invalid_amount"),
        (float("inf"), "invalid_amount"),
        (0.004, "amount_too_small"),
    ],
)
async def test_a_bad_total_is_refused_and_posts_nothing(
    amount: float,
    key: str,
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    server: FakeServer,
) -> None:
    with pytest.raises(ServiceValidationError) as raised:
        await log_pump(hass, config_entry_id=init_integration.entry_id, amount=amount)

    assert raised.value.translation_key == key
    assert server.posted == []


@pytest.mark.parametrize("minutes", [-1, 2.5, "ten"])
async def test_bad_minutes_are_refused_by_the_schema(
    minutes: Any, hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    with pytest.raises(probatio.MultipleInvalid):
        await log_pump(hass, config_entry_id=init_integration.entry_id, amount=3, minutes=minutes)

    assert server.posted == []


async def test_an_unknown_unit_is_refused_with_a_total(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    with pytest.raises(probatio.MultipleInvalid):
        await log_pump(hass, config_entry_id=init_integration.entry_id, amount=3, unit="cups")

    assert server.posted == []


# --- the per-side fields work as they did --------------------------------------------------


async def test_per_side_amounts_and_minutes_still_write_per_side_keys(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    await log_pump(
        hass,
        config_entry_id=init_integration.entry_id,
        left_amount=2,
        right_amount=3,
        unit="fl. oz.",
        left_minutes=8,
        right_minutes=9,
    )

    payload = only_pump(server).to_payload()
    assert payload["amount"]["value"] == 5
    assert payload["leftAmount"]["value"] == 2
    assert payload["rightAmount"]["value"] == 3
    assert (payload["leftDuration"], payload["rightDuration"]) == (8, 9)


async def test_side_minutes_with_a_total_amount_are_allowed_when_minutes_is_not_given(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    await log_pump(
        hass,
        config_entry_id=init_integration.entry_id,
        amount=5,
        left_minutes=8,
        right_minutes=9,
    )

    payload = only_pump(server).to_payload()
    assert (payload["leftDuration"], payload["rightDuration"]) == (8, 9)
    assert "leftAmount" not in payload
