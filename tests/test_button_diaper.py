"""Core PR 14: the wet, dirty, and mixed diaper buttons, including the write path through them."""

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import patch

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.storage import Store
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.baby_tracker.const import (
    DOMAIN,
)
from custom_components.baby_tracker.pybabytracker import (
    OP_ADD,
    AuthenticationError,
    BabyTrackerConnectionError,
    Diaper,
    NoResponseError,
)

from . import NOW, OWN_DEVICE
from .factories import (
    BABY_TWO_ID,
    DEVICE_B,
    at,
)
from .fake_server import FakeServer
from .helpers import (
    LAST_DIAPER,
    button,
    fail_to_write,
    gap_issue,
    posted,
    press,
    reauth_flows,
    sensor_states,
)


@pytest.fixture(autouse=True)
def _start_at_now(freezer: FrozenDateTimeFactory) -> None:
    """Freeze the clock at NOW before anything is set up.

    Timers scheduled at setup then share the clock the tests move, so moving it forward and
    ticking reaches them the way it does in core's tests.
    """
    freezer.move_to(NOW)


@pytest.mark.parametrize(
    ("kind", "status"), [("wet", 0), ("dirty", 1), ("mixed", 2)], ids=["wet", "dirty", "mixed"]
)
async def test_a_button_press_adds_that_kind_of_diaper_for_that_baby_now(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    server: FakeServer,
    freezer: FrozenDateTimeFactory,
    kind: str,
    status: int,
) -> None:
    await hass.config.async_set_time_zone("America/New_York")
    freezer.move_to(NOW)

    await press(hass, button("baby_two", kind))

    [(number, op, record)] = posted(server)
    assert (number, op) == (1, OP_ADD)
    assert isinstance(record, Diaper)
    assert record.status == status
    assert record.baby.object_id == BABY_TWO_ID
    assert record.time == NOW
    assert record.timezone == -18000  # standard, though late October is daylight time
    assert not record.note
    assert server.logs[OWN_DEVICE][0]["SyncID"] == 1


async def test_two_presses_make_two_records_with_different_ids(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    server: FakeServer,
    freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to(NOW)
    await press(hass, button("baby_one", "wet"))
    await press(hass, button("baby_one", "wet"))

    records = [record for _, _, record in posted(server)]
    assert len({record.object_id for record in records}) == 2
    assert [n for n, _, _ in posted(server)] == [1, 2]


async def test_a_write_saves_what_it_synced_and_posted(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    server: FakeServer,
    hass_storage: dict[str, Any],
) -> None:
    server.add_record(
        DEVICE_B, "Diaper", "D0000000-0000-4000-8000-0000000000C1", when=at(30), status=1
    )

    await press(hass, button("baby_one", "wet"))

    saved = hass_storage[f"{DOMAIN}.{init_integration.entry_id}"]["data"]
    assert saved["last_seen"][DEVICE_B] == 1  # the sync before the post
    assert saved["last_seen"][OWN_DEVICE] == 1  # and the post itself
    assert [op for _, op, _ in posted(server)] == [OP_ADD]


async def test_a_failed_sync_before_a_write_posts_nothing(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    server.request_error = BabyTrackerConnectionError("down")
    before = sensor_states(hass)

    with pytest.raises(HomeAssistantError) as raised:
        await press(hass, button("baby_one", "wet"))

    assert raised.value.translation_key == "sync_failed"
    assert server.posted == []
    assert sensor_states(hass) == before


async def test_a_failed_write_raises_a_translated_error_and_changes_no_sensor(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    before = sensor_states(hass)
    server.post_errors.append((BabyTrackerConnectionError("HTTP 500"), False))

    with pytest.raises(HomeAssistantError) as raised:
        await press(hass, button("baby_one", "wet"))

    assert type(raised.value) is HomeAssistantError
    assert raised.value.translation_domain == DOMAIN
    assert raised.value.translation_key == "write_failed"
    assert sensor_states(hass) == before


async def test_a_press_that_got_no_answer_but_landed_succeeds(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    server.post_errors.append((NoResponseError("timed out"), True))

    await press(hass, button("baby_one", "dirty"))

    assert len(posted(server)) == 1
    assert hass.states.get(LAST_DIAPER).attributes["type"] == "dirty"


async def test_a_write_right_after_an_unanswered_one_says_it_is_still_being_confirmed(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    server: FakeServer,
    freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to(NOW)
    server.post_errors.append((NoResponseError("timed out"), False))
    with pytest.raises(HomeAssistantError) as first:
        await press(hass, button("baby_one", "wet"))
    assert first.value.translation_key == "write_failed"

    with pytest.raises(HomeAssistantError) as second:
        await press(hass, button("baby_one", "wet"))

    assert type(second.value) is HomeAssistantError
    assert second.value.translation_domain == DOMAIN
    assert second.value.translation_key == "write_outcome_unknown"
    assert server.posted == []  # nothing was resent


async def test_the_unknown_outcome_clears_once_the_grace_window_passes(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    server: FakeServer,
    freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to(NOW)
    server.post_errors.append((NoResponseError("timed out"), False))
    with pytest.raises(HomeAssistantError):
        await press(hass, button("baby_one", "wet"))

    freezer.move_to(datetime(2026, 10, 26, 15, 5, 0, tzinfo=UTC))
    await press(hass, button("baby_one", "wet"))

    assert [n for n, _, _ in posted(server)] == [1]  # the free number was reused


async def test_a_failed_write_still_saves_the_engine_state(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    server: FakeServer,
    hass_storage: dict[str, Any],
    freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to(NOW)
    server.post_errors.append((NoResponseError("timed out"), False))
    with pytest.raises(HomeAssistantError):
        await press(hass, button("baby_one", "wet"))

    saved = hass_storage[f"{DOMAIN}.{init_integration.entry_id}"]["data"]
    assert saved["pending"]["sync_id"] == 1
    assert saved["pending"]["unknown_since"] is not None


async def test_a_successful_write_saves_the_new_record(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    server: FakeServer,
    hass_storage: dict[str, Any],
) -> None:
    await press(hass, button("baby_one", "wet"))

    saved = hass_storage[f"{DOMAIN}.{init_integration.entry_id}"]["data"]
    [(_, _, record)] = posted(server)
    assert saved["created"] == [record.object_id]
    assert saved["last_posted"] == 1
    assert saved["pending"] is None


async def test_a_gap_that_clears_before_the_hour_starts_the_clock_over(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    server: FakeServer,
    freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to(NOW)
    server.last_sync_override[OWN_DEVICE] = 5
    await fail_to_write(hass)
    freezer.tick(timedelta(minutes=30))
    server.last_sync_override.clear()
    await press(hass, button("baby_one", "wet"))  # the write works again

    freezer.tick(timedelta(minutes=45))  # 75 minutes after the first failure
    server.last_sync_override[OWN_DEVICE] = 6
    await fail_to_write(hass)
    freezer.tick(timedelta(minutes=45))  # 45 minutes after the second gap began
    await fail_to_write(hass)

    assert gap_issue(hass, init_integration) is None


async def test_a_second_gap_after_a_clean_stretch_can_still_become_a_repair(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    server: FakeServer,
    freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to(NOW)
    server.last_sync_override[OWN_DEVICE] = 5
    await fail_to_write(hass)
    server.last_sync_override.clear()
    await press(hass, button("baby_one", "wet"))

    freezer.tick(timedelta(hours=3))
    server.last_sync_override[OWN_DEVICE] = 9
    await fail_to_write(hass)
    freezer.tick(timedelta(hours=1, minutes=1))
    await fail_to_write(hass)

    assert gap_issue(hass, init_integration) is not None


async def test_each_account_times_its_own_gap(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    second_entry: MockConfigEntry,
    server: FakeServer,
    freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to(NOW)
    server.last_sync_override[OWN_DEVICE] = 5
    await fail_to_write(hass)
    freezer.tick(timedelta(hours=1, minutes=30))

    # The first account's gap has lasted long enough, the second account's just began.
    await fail_to_write(hass)
    await press(hass, "button.baby_three_wet_diaper")

    assert gap_issue(hass, init_integration) is not None
    assert gap_issue(hass, second_entry) is None


async def test_the_repair_issue_goes_away_after_a_write_succeeds(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    server: FakeServer,
    freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to(NOW)
    server.last_sync_override[OWN_DEVICE] = 5
    await fail_to_write(hass)
    freezer.tick(timedelta(hours=2))
    await fail_to_write(hass)
    assert gap_issue(hass, init_integration) is not None
    server.last_sync_override.clear()

    await press(hass, button("baby_one", "wet"))

    assert gap_issue(hass, init_integration) is None


async def test_a_gap_in_the_own_log_still_saves_state(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    server.last_sync_override[OWN_DEVICE] = 5
    with (
        patch.object(Store, "async_save", autospec=True) as save,
        pytest.raises(HomeAssistantError),
    ):
        await press(hass, button("baby_one", "wet"))
    assert save.call_count == 1


@pytest.mark.parametrize("where", ["post", "sync"])
async def test_an_auth_error_during_a_write_starts_reauth(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, where: str
) -> None:
    if where == "post":
        server.post_errors.append((AuthenticationError("session rejected"), False))
    else:
        server.request_error = AuthenticationError("session rejected")

    with pytest.raises(HomeAssistantError) as raised:
        await press(hass, button("baby_one", "wet"))

    assert raised.value.translation_key == "auth_failed"
    assert reauth_flows(hass) == ["reauth"]
    assert server.posted == []


async def test_concurrent_presses_are_serialized(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    server.calls.clear()

    await asyncio.gather(
        press(hass, button("baby_one", "wet")),
        press(hass, button("baby_one", "dirty")),
        press(hass, button("baby_two", "mixed")),
    )

    assert [n for n, _, _ in posted(server)] == [1, 2, 3]
    assert server.max_active_posts == 1
    # Each write finishes (sync, prepare, post) before the next starts.
    assert server.calls == ["devices", "devices", "post"] * 3


async def test_buttons_for_each_account_post_to_their_own_account(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    second_entry: MockConfigEntry,
    server: FakeServer,
    second_server: FakeServer,
) -> None:
    await press(hass, button("baby_three", "dirty"))
    assert server.posted == []
    assert len(second_server.posted) == 1
