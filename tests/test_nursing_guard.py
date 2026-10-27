"""The guard on Home Assistant's own nursing timer, and the discard_nursing action.

A running sleep is synced, so every phone shows it and no cap applies. A nursing timer lives only
in Home Assistant until it stops, so one left running is flagged after an hour with a repair, and
stops counting toward the one-minute polling. Everything here is invented fixture data.
"""

import asyncio
from datetime import timedelta
from typing import Any
from unittest.mock import patch

import pytest
import voluptuous as vol
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.storage import Store
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.nighp_baby_tracker.const import DOMAIN
from custom_components.nighp_baby_tracker.pybabytracker import BabyTrackerConnectionError

from . import BABY_TWO
from .factories import BABY_ONE_ID, BABY_TWO_ID, DEVICE_B, OP_DELETE
from .fake_server import FakeServer
from .helpers import (
    ASLEEP,
    NOW,
    NURSE_LEFT,
    NURSING_TIMER,
    account_device,
    call,
    device_id,
    minutes,
    nursing_posts,
    phone_sleep,
    posted,
    state,
    sync_interval,
    tick,
    toggle,
)

ISSUE = "nursing_timer_long_{}_{}"


def _issue_id(entry: MockConfigEntry, baby_id: str = BABY_ONE_ID) -> str:
    return ISSUE.format(entry.entry_id, baby_id)


def _issue(hass: HomeAssistant, entry: MockConfigEntry, baby_id: str = BABY_ONE_ID) -> Any:
    return ir.async_get(hass).async_get_issue(DOMAIN, _issue_id(entry, baby_id))


def _guard_issues(hass: HomeAssistant) -> list[ir.IssueEntry]:
    return [
        issue
        for (domain, _), issue in ir.async_get(hass).issues.items()
        if domain == DOMAIN and issue.translation_key == "nursing_timer_long"
    ]


async def _at(hass: HomeAssistant, freezer: Any, minute: float) -> None:
    """Move the clock to ``minute`` after NOW and let what was scheduled for then run."""
    freezer.move_to(minutes(minute))
    async_fire_time_changed(hass, minutes(minute))
    await hass.async_block_till_done()


def _saved_timer(
    started_minutes_ago: float,
    side: str = "left",
    object_id: str = "AB000000-0000-4000-8000-0000000000A1",
) -> dict[str, Any]:
    started = NOW - timedelta(minutes=started_minutes_ago)
    return {
        "object_id": object_id,
        "started": started.isoformat(),
        "side": side,
        "side_started": started.isoformat(),
        "left_seconds": 0,
        "right_seconds": 0,
    }


async def _setup_with_saved(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    hass_storage: dict[str, Any],
    saved: dict[str, Any],
) -> None:
    key = f"{DOMAIN}.{entry.entry_id}.nursing"
    hass_storage[key] = {"version": 1, "minor_version": 1, "key": key, "data": saved}
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED


# --- the repair --------------------------------------------------------------------------------


async def test_no_repair_at_59_minutes_and_the_repair_exists_at_exactly_60(
    hass: HomeAssistant, init_integration: MockConfigEntry, freezer: Any
) -> None:
    freezer.move_to(NOW)
    await toggle(hass, NURSE_LEFT)

    await _at(hass, freezer, 59)
    assert _issue(hass, init_integration) is None

    await _at(hass, freezer, 59.9)
    assert _issue(hass, init_integration) is None

    await _at(hass, freezer, 60)
    assert _issue(hass, init_integration) is not None


async def test_the_repair_is_there_at_61_minutes_too(
    hass: HomeAssistant, init_integration: MockConfigEntry, freezer: Any
) -> None:
    freezer.move_to(NOW)
    await toggle(hass, NURSE_LEFT)

    await _at(hass, freezer, 61)

    assert _issue(hass, init_integration) is not None


async def test_the_repair_is_a_translated_warning_that_names_the_baby_and_is_not_fixable(
    hass: HomeAssistant, init_integration: MockConfigEntry, freezer: Any
) -> None:
    freezer.move_to(NOW)
    await toggle(hass, NURSE_LEFT)

    await _at(hass, freezer, 61)

    issue = _issue(hass, init_integration)
    assert issue.translation_key == "nursing_timer_long"
    assert issue.translation_placeholders == {"baby": "Baby One"}
    assert issue.severity is ir.IssueSeverity.WARNING
    assert issue.is_fixable is False


async def test_a_side_switch_does_not_restart_the_hour(
    hass: HomeAssistant, init_integration: MockConfigEntry, freezer: Any
) -> None:
    freezer.move_to(NOW)
    await toggle(hass, NURSE_LEFT)
    freezer.move_to(minutes(50))
    await toggle(hass, "switch.baby_one_nursing_right")

    await _at(hass, freezer, 59)
    assert _issue(hass, init_integration) is None
    await _at(hass, freezer, 60)

    assert _issue(hass, init_integration) is not None


async def test_the_repair_is_raised_by_the_polling_sync_too(
    hass: HomeAssistant, init_integration: MockConfigEntry, freezer: Any
) -> None:
    freezer.move_to(NOW)
    await toggle(hass, NURSE_LEFT)
    freezer.move_to(minutes(61))

    # No scheduled callback is fired by hand: the next one-minute sync notices.
    async_fire_time_changed(hass, minutes(62))
    await hass.async_block_till_done()

    assert _issue(hass, init_integration) is not None


async def test_the_repair_is_raised_once_and_stays_one_issue(
    hass: HomeAssistant, init_integration: MockConfigEntry, freezer: Any
) -> None:
    freezer.move_to(NOW)
    await toggle(hass, NURSE_LEFT)
    await _at(hass, freezer, 61)
    await _at(hass, freezer, 70)
    await _at(hass, freezer, 80)

    assert [issue.issue_id for issue in _guard_issues(hass)] == [_issue_id(init_integration)]


async def test_a_dismissed_repair_stays_dismissed(
    hass: HomeAssistant, init_integration: MockConfigEntry, freezer: Any
) -> None:
    freezer.move_to(NOW)
    await toggle(hass, NURSE_LEFT)
    await _at(hass, freezer, 61)
    ir.async_ignore_issue(hass, DOMAIN, _issue_id(init_integration), True)

    await _at(hass, freezer, 75)

    assert _issue(hass, init_integration).dismissed_version is not None


async def test_each_baby_has_their_own_repair_and_clock(
    hass: HomeAssistant, init_integration: MockConfigEntry, freezer: Any
) -> None:
    freezer.move_to(NOW)
    await toggle(hass, NURSE_LEFT)
    freezer.move_to(minutes(30))
    await toggle(hass, "switch.baby_two_nursing_left")

    await _at(hass, freezer, 61)
    assert _issue(hass, init_integration, BABY_ONE_ID) is not None
    assert _issue(hass, init_integration, BABY_TWO_ID) is None

    await _at(hass, freezer, 90)
    assert _issue(hass, init_integration, BABY_TWO_ID) is not None
    assert _issue(hass, init_integration, BABY_TWO_ID).translation_placeholders == {
        "baby": "Baby Two"
    }


# --- polling ----------------------------------------------------------------------------------


async def test_a_nursing_timer_well_under_an_hour_still_polls_every_minute(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, freezer: Any
) -> None:
    freezer.move_to(NOW)
    await toggle(hass, NURSE_LEFT)

    await _at(hass, freezer, 50)  # the probe's own ticks run up to seven minutes ahead

    assert await sync_interval(hass, server) == timedelta(minutes=1)


async def test_past_an_hour_polling_falls_back_to_five_minutes(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, freezer: Any
) -> None:
    freezer.move_to(NOW)
    await toggle(hass, NURSE_LEFT)

    await _at(hass, freezer, 61)

    assert await sync_interval(hass, server) == timedelta(minutes=5)


async def test_a_synced_running_sleep_keeps_one_minute_polling_past_the_nursing_cap(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, freezer: Any
) -> None:
    freezer.move_to(NOW)
    await toggle(hass, NURSE_LEFT)
    phone_sleep(server, start=NOW - timedelta(minutes=5))
    await _at(hass, freezer, 61)
    # The sync that finds the running sleep is on the five-minute schedule by now.
    async_fire_time_changed(hass, minutes(61) + timedelta(minutes=6))
    await hass.async_block_till_done()
    assert state(hass, ASLEEP).state == "on"

    assert await sync_interval(hass, server) == timedelta(minutes=1)
    # The sleep isn't capped or flagged: only the nursing timer is.
    assert _issue(hass, init_integration) is not None


async def test_a_running_sleep_alone_is_never_flagged_however_long_it_runs(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, freezer: Any
) -> None:
    freezer.move_to(NOW)
    phone_sleep(server, start=NOW - timedelta(hours=3))
    await _at(hass, freezer, 10)
    async_fire_time_changed(hass, minutes(10) + timedelta(minutes=6))
    await hass.async_block_till_done()
    assert state(hass, ASLEEP).state == "on"
    await _at(hass, freezer, 600)

    assert _guard_issues(hass) == []
    assert await sync_interval(hass, server) == timedelta(minutes=1)


async def test_discarding_an_overdue_timer_leaves_polling_at_five_minutes(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, freezer: Any
) -> None:
    freezer.move_to(NOW)
    await toggle(hass, NURSE_LEFT)
    await _at(hass, freezer, 61)

    await call(hass, "discard_nursing")

    assert await sync_interval(hass, server) == timedelta(minutes=5)


async def test_discarding_a_young_timer_returns_polling_to_five_minutes(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, freezer: Any
) -> None:
    freezer.move_to(NOW)
    await toggle(hass, NURSE_LEFT)
    assert await sync_interval(hass, server) == timedelta(minutes=1)

    await call(hass, "discard_nursing")

    assert await sync_interval(hass, server) == timedelta(minutes=5)


async def test_a_new_timer_after_an_overdue_one_polls_every_minute_again(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, freezer: Any
) -> None:
    freezer.move_to(NOW)
    await toggle(hass, NURSE_LEFT)
    await _at(hass, freezer, 61)
    await call(hass, "discard_nursing")
    assert _issue(hass, init_integration) is None

    await toggle(hass, NURSE_LEFT)

    assert await sync_interval(hass, server) == timedelta(minutes=1)
    assert _issue(hass, init_integration) is None


async def test_a_timer_started_after_a_discard_gets_its_own_full_hour(
    hass: HomeAssistant, init_integration: MockConfigEntry, freezer: Any
) -> None:
    freezer.move_to(NOW)
    await toggle(hass, NURSE_LEFT)
    await _at(hass, freezer, 61)
    await call(hass, "discard_nursing")
    freezer.move_to(minutes(62))
    await toggle(hass, NURSE_LEFT)

    await _at(hass, freezer, 62 + 59)
    assert _issue(hass, init_integration) is None
    await _at(hass, freezer, 62 + 60)

    assert _issue(hass, init_integration) is not None


# --- clearing the repair ----------------------------------------------------------------------


async def test_stopping_the_timer_clears_the_repair_and_still_writes_the_record(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, freezer: Any
) -> None:
    freezer.move_to(NOW)
    await toggle(hass, NURSE_LEFT)
    await _at(hass, freezer, 61)
    assert _issue(hass, init_integration) is not None

    await call(hass, "stop_nursing")

    assert _issue(hass, init_integration) is None
    [record] = nursing_posts(server)
    assert (record.left_duration, record.right_duration) == (61, 0)


async def test_pressing_the_nurse_button_to_stop_clears_the_repair(
    hass: HomeAssistant, init_integration: MockConfigEntry, freezer: Any
) -> None:
    freezer.move_to(NOW)
    await toggle(hass, NURSE_LEFT)
    await _at(hass, freezer, 61)

    await toggle(hass, NURSE_LEFT)

    assert _issue(hass, init_integration) is None


async def test_discarding_the_timer_clears_the_repair(
    hass: HomeAssistant, init_integration: MockConfigEntry, freezer: Any
) -> None:
    freezer.move_to(NOW)
    await toggle(hass, NURSE_LEFT)
    await _at(hass, freezer, 61)
    assert _issue(hass, init_integration) is not None

    await call(hass, "discard_nursing")

    assert _issue(hass, init_integration) is None
    assert state(hass, NURSING_TIMER).state == "unknown"


async def test_discarding_one_babys_timer_leaves_the_other_babys_repair(
    hass: HomeAssistant, init_integration: MockConfigEntry, freezer: Any
) -> None:
    freezer.move_to(NOW)
    await toggle(hass, NURSE_LEFT)
    await toggle(hass, "switch.baby_two_nursing_left")
    await _at(hass, freezer, 61)
    assert _issue(hass, init_integration, BABY_ONE_ID) is not None
    assert _issue(hass, init_integration, BABY_TWO_ID) is not None

    await call(hass, "discard_nursing", BABY_ONE_ID)

    assert _issue(hass, init_integration, BABY_ONE_ID) is None
    assert _issue(hass, init_integration, BABY_TWO_ID) is not None


async def test_a_timer_that_ends_before_the_hour_never_raises_the_repair(
    hass: HomeAssistant, init_integration: MockConfigEntry, freezer: Any
) -> None:
    freezer.move_to(NOW)
    await toggle(hass, NURSE_LEFT)
    freezer.move_to(minutes(30))
    await toggle(hass, NURSE_LEFT)  # stops it

    await _at(hass, freezer, 61)
    await _at(hass, freezer, 120)

    assert _guard_issues(hass) == []


async def test_a_timer_discarded_before_the_hour_never_raises_the_repair(
    hass: HomeAssistant, init_integration: MockConfigEntry, freezer: Any
) -> None:
    freezer.move_to(NOW)
    await toggle(hass, NURSE_LEFT)
    freezer.move_to(minutes(30))
    await call(hass, "discard_nursing")

    await _at(hass, freezer, 61)

    assert _guard_issues(hass) == []


async def test_unloading_the_entry_removes_the_repair(
    hass: HomeAssistant, init_integration: MockConfigEntry, freezer: Any
) -> None:
    freezer.move_to(NOW)
    await toggle(hass, NURSE_LEFT)
    await _at(hass, freezer, 61)

    await hass.config_entries.async_unload(init_integration.entry_id)
    await hass.async_block_till_done()

    assert _guard_issues(hass) == []


async def test_removing_the_entry_removes_the_repair(
    hass: HomeAssistant, init_integration: MockConfigEntry, freezer: Any
) -> None:
    freezer.move_to(NOW)
    await toggle(hass, NURSE_LEFT)
    await _at(hass, freezer, 61)

    await hass.config_entries.async_remove(init_integration.entry_id)
    await hass.async_block_till_done()

    assert _guard_issues(hass) == []


async def test_an_unloaded_entry_does_not_raise_the_repair_when_the_hour_passes(
    hass: HomeAssistant, init_integration: MockConfigEntry, freezer: Any
) -> None:
    freezer.move_to(NOW)
    await toggle(hass, NURSE_LEFT)
    await hass.config_entries.async_unload(init_integration.entry_id)
    await hass.async_block_till_done()

    await _at(hass, freezer, 61)

    assert _guard_issues(hass) == []


# --- restarts ---------------------------------------------------------------------------------


async def test_a_saved_timer_older_than_an_hour_raises_the_repair_on_setup(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    server: FakeServer,
    hass_storage: dict[str, Any],
    freezer: Any,
) -> None:
    freezer.move_to(NOW)

    await _setup_with_saved(hass, mock_config_entry, hass_storage, {BABY_ONE_ID: _saved_timer(61)})

    assert _issue(hass, mock_config_entry) is not None
    assert await sync_interval(hass, server) == timedelta(minutes=5)


async def test_a_saved_timer_exactly_an_hour_old_raises_the_repair_on_setup(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    hass_storage: dict[str, Any],
    freezer: Any,
) -> None:
    freezer.move_to(NOW)

    await _setup_with_saved(hass, mock_config_entry, hass_storage, {BABY_ONE_ID: _saved_timer(60)})

    assert _issue(hass, mock_config_entry) is not None


async def test_a_saved_timer_under_an_hour_old_raises_no_repair_until_the_hour(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    hass_storage: dict[str, Any],
    freezer: Any,
) -> None:
    freezer.move_to(NOW)

    await _setup_with_saved(hass, mock_config_entry, hass_storage, {BABY_ONE_ID: _saved_timer(59)})

    assert _issue(hass, mock_config_entry) is None
    await _at(hass, freezer, 0.5)
    assert _issue(hass, mock_config_entry) is None
    await _at(hass, freezer, 1)
    assert _issue(hass, mock_config_entry) is not None


async def test_a_saved_timer_well_under_an_hour_old_polls_every_minute_after_a_restart(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    server: FakeServer,
    hass_storage: dict[str, Any],
    freezer: Any,
) -> None:
    freezer.move_to(NOW)

    await _setup_with_saved(hass, mock_config_entry, hass_storage, {BABY_ONE_ID: _saved_timer(30)})

    assert await sync_interval(hass, server) == timedelta(minutes=1)


async def test_a_reload_with_an_overdue_timer_raises_the_repair_again(
    hass: HomeAssistant, init_integration: MockConfigEntry, freezer: Any
) -> None:
    freezer.move_to(NOW)
    await toggle(hass, NURSE_LEFT)
    await _at(hass, freezer, 61)
    await hass.config_entries.async_unload(init_integration.entry_id)
    await hass.async_block_till_done()
    assert _guard_issues(hass) == []

    await hass.config_entries.async_setup(init_integration.entry_id)
    await hass.async_block_till_done()

    assert _issue(hass, init_integration) is not None


async def test_an_overdue_saved_timer_for_a_baby_no_longer_in_the_account_raises_nothing(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    hass_storage: dict[str, Any],
    freezer: Any,
) -> None:
    freezer.move_to(NOW)
    gone = "99999999-0000-4000-8000-000000000099"

    await _setup_with_saved(hass, mock_config_entry, hass_storage, {gone: _saved_timer(300)})

    assert _guard_issues(hass) == []


async def test_a_saved_overdue_timer_can_be_stopped_and_writes_the_capped_record(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    server: FakeServer,
    hass_storage: dict[str, Any],
    freezer: Any,
) -> None:
    freezer.move_to(NOW)
    await _setup_with_saved(hass, mock_config_entry, hass_storage, {BABY_ONE_ID: _saved_timer(300)})
    assert _issue(hass, mock_config_entry) is not None

    await call(hass, "stop_nursing")

    [record] = nursing_posts(server)
    assert record.left_duration == 240
    assert _issue(hass, mock_config_entry) is None


# --- discard_nursing --------------------------------------------------------------------------


async def test_discard_drops_the_timer_and_writes_nothing_at_all(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, freezer: Any
) -> None:
    freezer.move_to(NOW)
    await toggle(hass, NURSE_LEFT)
    freezer.move_to(minutes(20))
    calls_before = list(server.calls)

    await call(hass, "discard_nursing")

    assert state(hass, NURSING_TIMER).state == "unknown"
    assert posted(server) == []
    assert server.posted == []
    # Not even a sync: the discard never touches the network.
    assert server.calls == calls_before


async def test_a_discarded_timer_is_gone_after_a_restart(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    hass_storage: dict[str, Any],
    freezer: Any,
) -> None:
    freezer.move_to(NOW)
    await toggle(hass, NURSE_LEFT)
    await call(hass, "discard_nursing")

    await hass.config_entries.async_reload(init_integration.entry_id)
    await hass.async_block_till_done()

    assert state(hass, NURSING_TIMER).state == "unknown"
    key = f"{DOMAIN}.{init_integration.entry_id}.nursing"
    assert hass_storage[key]["data"] == {}


async def test_discard_does_not_touch_the_sync_state_or_the_other_babys_timer(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, freezer: Any
) -> None:
    freezer.move_to(NOW)
    await toggle(hass, NURSE_LEFT)
    await toggle(hass, "switch.baby_two_nursing_right")

    await call(hass, "discard_nursing", BABY_ONE_ID)

    assert state(hass, NURSING_TIMER).state == "unknown"
    other = state(hass, "sensor.baby_two_nursing_timer")
    assert other.state == NOW.isoformat()
    assert other.attributes["side"] == "right"
    assert posted(server) == []


async def test_discard_with_no_running_timer_is_a_clear_validation_error_and_posts_nothing(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    calls_before = list(server.calls)

    with pytest.raises(ServiceValidationError) as err:
        await call(hass, "discard_nursing")

    assert err.value.translation_domain == DOMAIN
    assert err.value.translation_key == "no_nursing_timer"
    assert posted(server) == []
    assert server.calls == calls_before


async def test_discard_twice_fails_the_second_time(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, freezer: Any
) -> None:
    freezer.move_to(NOW)
    await toggle(hass, NURSE_LEFT)
    await call(hass, "discard_nursing")

    with pytest.raises(ServiceValidationError) as err:
        await call(hass, "discard_nursing")

    assert err.value.translation_key == "no_nursing_timer"
    assert posted(server) == []


async def test_discard_for_another_baby_than_the_one_with_the_timer_fails_and_keeps_the_timer(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, freezer: Any
) -> None:
    freezer.move_to(NOW)
    await toggle(hass, NURSE_LEFT)  # baby one's timer runs

    with pytest.raises(ServiceValidationError) as err:
        await call(hass, "discard_nursing", BABY_TWO_ID)

    assert err.value.translation_key == "no_nursing_timer"
    assert state(hass, NURSING_TIMER).state == NOW.isoformat()
    assert posted(server) == []


async def test_discard_for_the_account_device_is_a_baby_not_found_error(
    hass: HomeAssistant, init_integration: MockConfigEntry, freezer: Any
) -> None:
    freezer.move_to(NOW)
    await toggle(hass, NURSE_LEFT)
    account = account_device(hass, init_integration)

    with pytest.raises(ServiceValidationError) as err:
        await hass.services.async_call(
            DOMAIN, "discard_nursing", {"device_id": account.id}, blocking=True
        )

    assert err.value.translation_key == "baby_not_found"
    assert state(hass, NURSING_TIMER).state == NOW.isoformat()


async def test_discard_for_an_unknown_device_is_a_baby_not_found_error(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    with pytest.raises(ServiceValidationError) as err:
        await hass.services.async_call(
            DOMAIN, "discard_nursing", {"device_id": "no-such-device"}, blocking=True
        )

    assert err.value.translation_key == "baby_not_found"


async def test_discard_needs_a_device(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    with pytest.raises(vol.Invalid):
        await hass.services.async_call(DOMAIN, "discard_nursing", {}, blocking=True)


async def test_discard_accepts_the_deprecated_baby_field_and_raises_its_repair(
    hass: HomeAssistant, init_integration: MockConfigEntry, freezer: Any
) -> None:
    freezer.move_to(NOW)
    await toggle(hass, NURSE_LEFT)

    await hass.services.async_call(
        DOMAIN, "discard_nursing", {"baby": device_id(hass, BABY_ONE_ID)}, blocking=True
    )

    assert state(hass, NURSING_TIMER).state == "unknown"
    assert ir.async_get(hass).async_get_issue(DOMAIN, "deprecated_discard_nursing_baby") is not None


async def test_a_discard_during_a_stops_write_waits_and_finds_no_timer(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, freezer: Any
) -> None:
    freezer.move_to(NOW)
    await toggle(hass, NURSE_LEFT)
    gate = asyncio.Event()
    server.post_gate = gate
    freezer.move_to(minutes(6))
    stop = hass.async_create_task(toggle(hass, NURSE_LEFT))
    for _ in range(20):
        await asyncio.sleep(0)
    assert "post" in server.calls  # the stop's post is waiting at the gate
    discard = hass.async_create_task(call(hass, "discard_nursing"))
    for _ in range(5):
        await asyncio.sleep(0)
    assert not discard.done()  # waiting on the coordinator's lock, not run in the middle
    gate.set()
    await stop

    with pytest.raises(ServiceValidationError) as err:
        await discard

    assert err.value.translation_key == "no_nursing_timer"
    [record] = nursing_posts(server)  # the stop's record was still written, once
    assert (record.left_duration, record.right_duration) == (6, 0)
    assert state(hass, NURSING_TIMER).state == "unknown"


async def test_a_stop_during_a_discards_wait_finds_no_timer_and_writes_nothing(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, freezer: Any
) -> None:
    freezer.move_to(NOW)
    await toggle(hass, NURSE_LEFT)
    lock = init_integration.runtime_data.exclusive()
    await lock.acquire()  # something else (a sync or write) holds the lock
    discard = hass.async_create_task(call(hass, "discard_nursing"))
    stop = hass.async_create_task(call(hass, "stop_nursing"))
    for _ in range(10):
        await asyncio.sleep(0)
    assert not discard.done() and not stop.done()
    lock.release()
    await discard

    with pytest.raises(ServiceValidationError) as err:
        await stop

    assert err.value.translation_key == "no_nursing_timer"
    assert posted(server) == []


async def test_a_discard_that_cannot_be_saved_still_drops_the_timer_and_says_so(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, freezer: Any
) -> None:
    freezer.move_to(NOW)
    await toggle(hass, NURSE_LEFT)
    real_save = Store.async_save

    async def save(self: Store[Any], data: Any) -> None:
        if self.key.endswith(".nursing"):
            raise OSError("disk full")
        await real_save(self, data)

    with (
        patch.object(Store, "async_save", save),
        pytest.raises(HomeAssistantError) as err,
    ):
        await call(hass, "discard_nursing")

    assert err.value.translation_key == "nursing_not_saved"
    assert state(hass, NURSING_TIMER).state == "unknown"
    assert posted(server) == []


async def test_the_guard_firing_during_a_failing_sync_leaves_entities_unavailable(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, freezer: Any
) -> None:
    freezer.move_to(NOW)
    await toggle(hass, NURSE_LEFT)
    server.request_error = BabyTrackerConnectionError("down")
    await tick(hass)
    assert state(hass, NURSING_TIMER).state == "unavailable"
    freezer.move_to(minutes(61))

    # The guard's own callback, without a poll: it must not mark the failed sync as a success.
    init_integration.runtime_data._async_guard_due(minutes(61))  # noqa: SLF001
    await hass.async_block_till_done()

    assert state(hass, NURSING_TIMER).state == "unavailable"
    assert _issue(hass, init_integration) is not None


async def test_a_stop_then_a_fresh_start_inside_the_old_window_raises_no_early_repair(
    hass: HomeAssistant, init_integration: MockConfigEntry, freezer: Any
) -> None:
    freezer.move_to(NOW)
    await toggle(hass, NURSE_LEFT)
    freezer.move_to(minutes(30))
    await toggle(hass, NURSE_LEFT)  # stops it
    freezer.move_to(minutes(40))
    await toggle(hass, NURSE_LEFT)  # a fresh timer

    await _at(hass, freezer, 60)  # the first timer's hour
    assert _guard_issues(hass) == []
    await _at(hass, freezer, 99)
    assert _guard_issues(hass) == []
    await _at(hass, freezer, 100)

    assert _issue(hass, init_integration) is not None


async def test_a_baby_deleted_while_flagged_clears_their_repair(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, freezer: Any
) -> None:
    freezer.move_to(NOW)
    await toggle(hass, "switch.baby_two_nursing_left")
    await _at(hass, freezer, 61)
    assert _issue(hass, init_integration, BABY_TWO_ID) is not None

    server.add_payload(DEVICE_B, BABY_TWO, op=OP_DELETE)
    await tick(hass)

    assert _issue(hass, init_integration, BABY_TWO_ID) is None
