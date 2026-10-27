"""The asleep binary sensor."""

from collections.abc import Iterator
from datetime import datetime
from unittest.mock import patch

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    snapshot_platform,
)
from syrupy.assertion import SnapshotAssertion

from custom_components.nighp_baby_tracker.pybabytracker import BabyTrackerConnectionError

from . import BABY_ONE, BABY_TWO, setup_integration
from .factories import (
    BABY_ONE_ID,
    DEVICE_A,
    DEVICE_B,
    OP_DELETE,
    OP_UPDATE,
    at,
)
from .fake_server import FakeServer
from .helpers import (
    LOCAL_NOW,
    TODAY,
    baby_value,
    get_baby_device,
    local,
    pass_midnight,
    setup_loaded,
    tick,
)

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


def _diaper(server: FakeServer, when: datetime, status: int = 0, baby=BABY_ONE, **kw) -> str:
    object_id = _id()
    server.add_record(DEVICE_A, "Diaper", object_id, when=when, status=status, baby=baby, **kw)
    return object_id


def _sleep(server: FakeServer, when: datetime, duration: int, baby=BABY_ONE, **kw) -> str:
    object_id = _id()
    server.add_record(DEVICE_A, "Sleep", object_id, when=when, duration=duration, baby=baby, **kw)
    return object_id


async def test_binary_sensors(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    snapshot: SnapshotAssertion,
    freezer: FrozenDateTimeFactory,
    mock_config_entry: MockConfigEntry,
    populated_server: FakeServer,
) -> None:
    freezer.move_to(at(180))
    with patch("custom_components.nighp_baby_tracker.PLATFORMS", [Platform.BINARY_SENSOR]):
        await setup_integration(hass, mock_config_entry)

    await snapshot_platform(hass, entity_registry, snapshot, mock_config_entry.entry_id)


# Asleep


async def test_asleep_is_on_while_the_newest_sleep_is_running(
    hass: HomeAssistant, entry: MockConfigEntry, empty: FakeServer
) -> None:
    _sleep(empty, local(2026, 10, 26, 15, 30), 0)
    await setup_loaded(hass, entry)
    assert baby_value(hass, "asleep") == "on"
    assert baby_value(hass, "asleep", "baby_two") == "off"


async def test_asleep_is_off_when_the_newest_sleep_has_ended(
    hass: HomeAssistant, entry: MockConfigEntry, empty: FakeServer
) -> None:
    _sleep(empty, local(2026, 10, 26, 13), 45)
    await setup_loaded(hass, entry)
    assert baby_value(hass, "asleep") == "off"


async def test_asleep_is_off_with_no_sleeps(
    hass: HomeAssistant, entry: MockConfigEntry, empty: FakeServer
) -> None:
    await setup_loaded(hass, entry)
    assert baby_value(hass, "asleep") == "off"


async def test_asleep_turns_on_when_a_sleep_starts_on_another_device(
    hass: HomeAssistant, entry: MockConfigEntry, empty: FakeServer
) -> None:
    await setup_loaded(hass, entry)
    assert baby_value(hass, "asleep") == "off"
    empty.add_record(DEVICE_B, "Sleep", _id(), when=LOCAL_NOW, duration=0)
    await tick(hass, minutes=1440)
    assert baby_value(hass, "asleep") == "on"


async def test_asleep_turns_off_when_the_running_sleep_ends(
    hass: HomeAssistant, entry: MockConfigEntry, empty: FakeServer
) -> None:
    started = local(2026, 10, 26, 15, 0)
    sleep = _sleep(empty, started, 0)
    await setup_loaded(hass, entry)
    assert baby_value(hass, "asleep") == "on"
    empty.add_record(
        DEVICE_A, "Sleep", sleep, when=started, changed=LOCAL_NOW, duration=55, op=OP_UPDATE
    )
    await tick(hass, minutes=1440)
    assert baby_value(hass, "asleep") == "off"


async def test_deleting_the_running_sleep_turns_asleep_off(
    hass: HomeAssistant, entry: MockConfigEntry, empty: FakeServer
) -> None:
    started = local(2026, 10, 26, 15, 0)
    sleep = _sleep(empty, started, 0)
    await setup_loaded(hass, entry)
    assert baby_value(hass, "asleep") == "on"
    empty.add_record(DEVICE_A, "Sleep", sleep, when=started, duration=0, op=OP_DELETE)
    await tick(hass, minutes=1440)
    assert baby_value(hass, "asleep") == "off"


async def test_deleting_a_running_sleep_falls_back_to_the_sleep_before_it(
    hass: HomeAssistant, entry: MockConfigEntry, empty: FakeServer
) -> None:
    _sleep(empty, local(2026, 10, 26, 9), 30)
    newer = _sleep(empty, local(2026, 10, 26, 15), 0)
    await setup_loaded(hass, entry)
    assert baby_value(hass, "asleep") == "on"
    empty.add_record(DEVICE_A, "Sleep", newer, when=local(2026, 10, 26, 15), op=OP_DELETE)
    await tick(hass, minutes=1440)
    assert baby_value(hass, "asleep") == "off"


async def test_with_several_running_sleeps_the_newest_one_decides(
    hass: HomeAssistant, entry: MockConfigEntry, empty: FakeServer
) -> None:
    _sleep(empty, local(2026, 10, 26, 9), 0)
    _sleep(empty, local(2026, 10, 26, 15), 0)
    await setup_loaded(hass, entry)
    assert baby_value(hass, "asleep") == "on"


async def test_an_older_running_sleep_does_not_count_once_a_newer_one_has_ended(
    hass: HomeAssistant, entry: MockConfigEntry, empty: FakeServer
) -> None:
    # The rule: asleep follows the baby's newest sleep, so a timer that was never stopped
    # can't hold the sensor on after a later nap finished.
    _sleep(empty, local(2026, 10, 26, 9), 0)
    _sleep(empty, local(2026, 10, 26, 13), 40)
    await setup_loaded(hass, entry)
    assert baby_value(hass, "asleep") == "off"


async def test_a_running_sleep_does_not_make_another_baby_asleep(
    hass: HomeAssistant, entry: MockConfigEntry, empty: FakeServer
) -> None:
    _sleep(empty, local(2026, 10, 26, 15), 0, baby=BABY_TWO)
    await setup_loaded(hass, entry)
    assert baby_value(hass, "asleep", "baby_one") == "off"
    assert baby_value(hass, "asleep", "baby_two") == "on"


async def test_a_sleep_running_across_midnight_keeps_asleep_on_while_the_counts_reset(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    empty: FakeServer,
    clock: FrozenDateTimeFactory,
) -> None:
    clock.move_to(local(2026, 10, 26, 23, 0))
    _sleep(empty, local(2026, 10, 26, 22, 30), 0)
    _diaper(empty, local(2026, 10, 26, 22, 0))
    await setup_loaded(hass, entry)
    assert baby_value(hass, "asleep") == "on"
    assert baby_value(hass, "diapers_today") == "1"

    await pass_midnight(hass, clock)
    assert baby_value(hass, "asleep") == "on"
    assert baby_value(hass, "diapers_today") == "0"


async def test_asleep_is_a_binary_sensor_on_the_baby_device(
    hass: HomeAssistant, entry: MockConfigEntry, empty: FakeServer
) -> None:
    await setup_loaded(hass, entry)
    found = er.async_get(hass).async_get("binary_sensor.baby_one_asleep")
    assert found is not None
    assert found.unique_id == f"{BABY_ONE_ID}_asleep"
    device = get_baby_device(dr.async_get(hass), BABY_ONE_ID)
    assert device is not None
    assert found.device_id == device.id


async def test_the_new_entities_are_unavailable_after_a_failed_sync_and_recover(
    hass: HomeAssistant, entry: MockConfigEntry, empty: FakeServer
) -> None:
    _sleep(empty, local(2026, 10, 26, 15), 0)
    _diaper(empty, TODAY)
    await setup_loaded(hass, entry)
    empty.request_error = BabyTrackerConnectionError("down")
    await tick(hass, minutes=1440)
    assert baby_value(hass, "asleep") == "unavailable"
    assert baby_value(hass, "diapers_today") == "unavailable"
    empty.request_error = None
    await tick(hass, minutes=1440)
    assert baby_value(hass, "asleep") == "on"
    assert baby_value(hass, "diapers_today") == "1"
