"""Core PR 18: the nursing timers saved with a Store and restored after a restart."""

from datetime import timedelta
from typing import Any

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.nighp_baby_tracker.const import (
    DOMAIN,
)

from . import NOW, sync_interval
from .factories import (
    BABY_ONE_ID,
    BABY_TWO_ID,
)
from .fake_server import FakeServer
from .helpers import NURSING_TIMER, nurse, nursing_posts, state


@pytest.fixture(autouse=True)
def _start_at_now(freezer: FrozenDateTimeFactory) -> None:
    """Freeze the clock at NOW before anything is set up.

    Timers scheduled at setup then share the clock the tests move, so moving it forward and
    ticking reaches them the way it does in core's tests.
    """
    freezer.move_to(NOW)


_GOOD_TIMER = {
    "object_id": "C0000000-0000-4000-8000-000000000001",
    "started": "2026-10-26T14:50:00+00:00",
    "side": "left",
    "side_started": "2026-10-26T14:55:00+00:00",
    "left_seconds": 120.0,
    "right_seconds": 0.0,
}


async def test_a_nursing_timer_survives_a_restart(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    server: FakeServer,
    freezer: FrozenDateTimeFactory,
) -> None:
    await nurse(hass, freezer, (0, "left"), (4, "right"))

    await hass.config_entries.async_reload(init_integration.entry_id)
    await hass.async_block_till_done()
    assert init_integration.state is ConfigEntryState.LOADED
    timer = state(hass, NURSING_TIMER)
    assert timer.state == NOW.isoformat()
    assert timer.attributes["side"] == "right"
    assert timer.attributes["left_seconds"] == 240

    await nurse(hass, freezer, (10, "right"))  # the time across the restart counts

    [record] = nursing_posts(server)
    assert (record.left_duration, record.right_duration, record.finish_side) == (4, 6, 1)


async def test_a_corrupt_saved_nursing_timer_is_ignored(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    store: Store = Store(hass, 1, f"{DOMAIN}.{mock_config_entry.entry_id}.nursing")
    await store.async_save({BABY_ONE_ID: {"side": "middle"}})
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.LOADED
    assert state(hass, NURSING_TIMER).state == "unknown"


@pytest.mark.parametrize(
    "damage",
    [
        pytest.param({"started": "not a time"}, id="started-unparseable"),
        pytest.param({"side_started": "not a time"}, id="side-started-unparseable"),
        pytest.param({"started": "2026-10-26T14:50:00"}, id="started-without-timezone"),
        pytest.param({"side_started": "2026-10-26T14:55:00"}, id="side-started-without-timezone"),
        pytest.param({"left_seconds": -5.0}, id="negative-left"),
        pytest.param({"right_seconds": -1.0}, id="negative-right"),
        pytest.param({"left_seconds": float("inf")}, id="infinite-left"),
        pytest.param({"right_seconds": float("nan")}, id="not-a-number-right"),
        pytest.param({"left_seconds": "many"}, id="seconds-not-a-number"),
        pytest.param({"object_id": None, "started": None}, id="missing-values"),
    ],
)
async def test_a_saved_nursing_timer_with_bad_data_is_dropped_and_the_others_survive(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
    damage: dict[str, Any],
) -> None:
    store: Store = Store(hass, 1, f"{DOMAIN}.{mock_config_entry.entry_id}.nursing")
    await store.async_save({BABY_ONE_ID: {**_GOOD_TIMER, **damage}, BABY_TWO_ID: dict(_GOOD_TIMER)})
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.LOADED
    assert state(hass, NURSING_TIMER).state == "unknown"
    assert state(hass, "sensor.baby_two_nursing_timer").attributes["left_seconds"] == 120
    assert "Dropping a saved nursing timer" in caplog.text


async def test_a_saved_nursing_timer_store_that_is_not_a_mapping_is_ignored(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    store: Store = Store(hass, 1, f"{DOMAIN}.{mock_config_entry.entry_id}.nursing")
    await store.async_save([_GOOD_TIMER])
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.LOADED
    assert state(hass, NURSING_TIMER).state == "unknown"


async def test_removing_the_entry_deletes_the_saved_nursing_timers(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
    hass_storage: dict[str, Any],
) -> None:
    await nurse(hass, freezer, (0, "left"))
    key = f"{DOMAIN}.{init_integration.entry_id}.nursing"
    await hass.async_block_till_done()
    assert key in hass_storage

    await hass.config_entries.async_remove(init_integration.entry_id)
    await hass.async_block_till_done()

    assert key not in hass_storage


async def test_a_saved_nursing_timer_for_a_baby_no_longer_in_the_account_is_dropped(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    server: FakeServer,
    hass_storage: dict[str, Any],
) -> None:
    key = f"{DOMAIN}.{mock_config_entry.entry_id}.nursing"
    gone = "99999999-0000-4000-8000-000000000099"
    timer = {
        "object_id": "AB000000-0000-4000-8000-000000000001",
        "started": NOW.isoformat(),
        "side": "left",
        "side_started": NOW.isoformat(),
        "left_seconds": 0,
        "right_seconds": 0,
    }
    hass_storage[key] = {"version": 1, "minor_version": 1, "key": key, "data": {gone: timer}}
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.LOADED
    assert state(hass, NURSING_TIMER).state == "unknown"  # no timer runs for anyone
    assert gone not in hass_storage[key]["data"]
    assert await sync_interval(hass, server) == timedelta(minutes=5)
