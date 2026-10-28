"""Core PR 16: start_sleep and stop_sleep."""

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.baby_tracker.pybabytracker import (
    OP_ADD,
    OP_DELETE,
    OP_UPDATE,
)

from . import NOW
from .factories import (
    BABY_ONE_ID,
)
from .fake_server import FakeServer
from .helpers import (
    ASLEEP,
    PHONE_SLEEP,
    SLEEP_SWITCH,
    SLEEP_TIMER,
    call,
    device_id,
    minutes,
    phone_sleep,
    posted,
    state,
    sync,
    toggle,
    undo,
)


@pytest.fixture(autouse=True)
def _start_at_now(freezer: FrozenDateTimeFactory) -> None:
    """Freeze the clock at NOW before anything is set up.

    Timers scheduled at setup then share the clock the tests move, so moving it forward and
    ticking reaches them the way it does in core's tests.
    """
    freezer.move_to(NOW)


async def test_undo_of_a_sleep_home_assistant_started_deletes_it(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    server: FakeServer,
    freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to(NOW)
    await toggle(hass, SLEEP_SWITCH)

    await undo(hass, device_id=device_id(hass, BABY_ONE_ID))

    [(_, op1, started), (_, op2, deleted)] = posted(server)
    assert (op1, op2) == (OP_ADD, OP_DELETE)
    assert deleted.object_id == started.object_id
    assert state(hass, ASLEEP).state == "off"
    assert state(hass, SLEEP_TIMER).state == "unknown"


async def test_start_sleep_with_a_past_start_and_a_note(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    server: FakeServer,
    freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to(NOW)

    await call(hass, "start_sleep", start=minutes(-10).isoformat(), note="Crib")

    [(_, op, record)] = posted(server)
    assert op == OP_ADD
    assert record.duration == 0
    assert record.time == minutes(-10)
    assert record.note == "Crib"
    assert state(hass, SLEEP_TIMER).state == minutes(-10).isoformat()


async def test_start_sleep_with_an_object_id_retries_safely(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    server: FakeServer,
    freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to(NOW)
    object_id = "5EE90000-0000-4000-8000-0000000000AA"

    await call(hass, "start_sleep", object_id=object_id.lower())
    await call(hass, "start_sleep", object_id=object_id)

    [(_, _, record)] = posted(server)
    assert record.object_id == object_id


async def test_start_sleep_while_a_sleep_is_running_is_refused(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    server: FakeServer,
    freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to(NOW)
    phone_sleep(server, start=minutes(-40))
    await sync(hass, init_integration)

    with pytest.raises(ServiceValidationError) as err:
        await call(hass, "start_sleep")

    assert err.value.translation_key == "sleep_already_running"
    assert posted(server) == []


async def test_start_sleep_more_than_24_hours_ago_is_refused(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    server: FakeServer,
    freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to(NOW)

    with pytest.raises(ServiceValidationError) as err:
        await call(hass, "start_sleep", start=minutes(-60 * 25).isoformat())

    assert err.value.translation_key == "sleep_start_out_of_range"
    assert posted(server) == []


async def test_stop_sleep_ends_a_phone_started_sleep_at_the_given_end(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    server: FakeServer,
    freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to(NOW)
    phone_sleep(server, start=minutes(-40))
    await sync(hass, init_integration)

    await call(hass, "stop_sleep", end=minutes(-5).isoformat())

    [(_, op, record)] = posted(server)
    assert (op, record.object_id, record.duration) == (OP_UPDATE, PHONE_SLEEP, 35)
    assert state(hass, ASLEEP).state == "off"


async def test_stop_sleep_defaults_to_now(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    server: FakeServer,
    freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to(NOW)
    await call(hass, "start_sleep")
    freezer.move_to(minutes(61))

    await call(hass, "stop_sleep")

    [_, (_, op, record)] = posted(server)
    assert (op, record.duration) == (OP_UPDATE, 61)


async def test_stop_sleep_with_no_running_sleep_is_a_validation_error(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    server: FakeServer,
    freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to(NOW)
    phone_sleep(server, start=minutes(-90), duration=30)  # finished
    await sync(hass, init_integration)

    with pytest.raises(ServiceValidationError) as err:
        await call(hass, "stop_sleep")

    assert err.value.translation_key == "no_running_sleep"
    assert posted(server) == []


async def test_stop_sleep_with_an_end_before_the_start_is_refused(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    server: FakeServer,
    freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to(NOW)
    phone_sleep(server, start=minutes(-40))
    await sync(hass, init_integration)

    with pytest.raises(ServiceValidationError) as err:
        await call(hass, "stop_sleep", end=minutes(-50).isoformat())

    assert err.value.translation_key == "end_before_start"
    assert posted(server) == []


async def test_stop_sleep_past_24_hours_writes_24_hours(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    server: FakeServer,
    freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to(NOW)
    phone_sleep(server, start=minutes(-60 * 23))
    await sync(hass, init_integration)
    freezer.move_to(minutes(60 * 3))

    await call(hass, "stop_sleep")

    [(_, _, record)] = posted(server)
    assert record.duration == 1440
