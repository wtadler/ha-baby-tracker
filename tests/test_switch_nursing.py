"""Core PR 20: the nursing side switches, and the forgotten-timer repair through them."""

from typing import Any
from unittest.mock import patch

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.storage import Store
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.baby_tracker.const import DOMAIN
from custom_components.baby_tracker.pybabytracker import (
    BabyTrackerConnectionError,
)

from . import NOW
from .factories import BABY_ONE_ID, BABY_TWO_ID
from .fake_server import FakeServer
from .helpers import (
    DISCARD_NURSING,
    NURSE_LEFT,
    NURSE_RIGHT,
    NURSING_TIMER,
    TWO_NURSE_LEFT,
    TWO_NURSE_RIGHT,
    minutes,
    nursing_posts,
    posted,
    press,
    state,
    toggle,
    turn_off,
    turn_on,
)

TWO_NURSING_TIMER = "sensor.baby_two_nursing_timer"


@pytest.fixture(autouse=True)
def _start_at_now(freezer: FrozenDateTimeFactory) -> None:
    """Freeze the clock at NOW before anything is set up."""
    freezer.move_to(NOW)


def _issue(hass: HomeAssistant, entry: MockConfigEntry, baby_id: str = BABY_ONE_ID) -> Any:
    return ir.async_get(hass).async_get_issue(
        DOMAIN, f"nursing_timer_long_{entry.entry_id}_{baby_id}"
    )


async def _expect_error(
    hass: HomeAssistant, entity_id: str, service: str, key: str, kind: type[Exception]
) -> None:
    with pytest.raises(kind) as err:
        await hass.services.async_call("switch", service, {"entity_id": entity_id}, blocking=True)
    assert getattr(err.value, "translation_key", None) == key
    assert getattr(err.value, "translation_domain", None) == DOMAIN


async def test_turning_a_side_on_with_no_session_starts_a_timer_on_it_and_posts_nothing(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    await turn_on(hass, NURSE_LEFT)

    assert posted(server) == []
    assert state(hass, NURSE_LEFT).state == "on"
    assert state(hass, NURSE_RIGHT).state == "off"
    timer = state(hass, NURSING_TIMER)
    assert timer.state == NOW.isoformat()
    assert timer.attributes["side"] == "left"


async def test_turning_the_other_side_on_switches_sides_and_the_first_reads_off(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    server: FakeServer,
    freezer: FrozenDateTimeFactory,
) -> None:
    await turn_on(hass, NURSE_LEFT)
    freezer.move_to(minutes(5))

    await turn_on(hass, NURSE_RIGHT)

    assert posted(server) == []
    assert state(hass, NURSE_LEFT).state == "off"
    assert state(hass, NURSE_RIGHT).state == "on"
    timer = state(hass, NURSING_TIMER)
    assert timer.state == NOW.isoformat()  # one session, still
    assert timer.attributes["side"] == "right"
    assert timer.attributes["left_seconds"] == 300


async def test_turning_off_the_running_side_stops_the_session_and_logs_both_sides(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    server: FakeServer,
    freezer: FrozenDateTimeFactory,
) -> None:
    await turn_on(hass, NURSE_LEFT)
    freezer.move_to(minutes(5))
    await turn_on(hass, NURSE_RIGHT)
    freezer.move_to(minutes(12))

    await turn_off(hass, NURSE_RIGHT)

    [record] = nursing_posts(server)
    assert len(posted(server)) == 1
    assert (record.left_duration, record.right_duration, record.finish_side) == (5, 7, 1)
    assert record.time == NOW
    assert record.baby.object_id == BABY_ONE_ID
    assert state(hass, NURSE_LEFT).state == "off"
    assert state(hass, NURSE_RIGHT).state == "off"
    assert state(hass, NURSING_TIMER).state == "unknown"


async def test_turning_off_the_left_side_ends_on_the_left(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    server: FakeServer,
    freezer: FrozenDateTimeFactory,
) -> None:
    await turn_on(hass, NURSE_RIGHT)
    freezer.move_to(minutes(4))
    await turn_on(hass, NURSE_LEFT)
    freezer.move_to(minutes(6))

    await turn_off(hass, NURSE_LEFT)

    [record] = nursing_posts(server)
    assert (record.left_duration, record.right_duration, record.finish_side) == (2, 4, 0)


async def test_turning_on_the_side_already_running_does_nothing(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    server: FakeServer,
    freezer: FrozenDateTimeFactory,
) -> None:
    await turn_on(hass, NURSE_LEFT)
    before = state(hass, NURSING_TIMER)
    freezer.move_to(minutes(3))

    await turn_on(hass, NURSE_LEFT)

    assert posted(server) == []
    after = state(hass, NURSING_TIMER)
    assert after.attributes == before.attributes  # side_started is unchanged
    assert state(hass, NURSE_LEFT).state == "on"


async def test_turning_off_a_side_with_no_session_does_nothing(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    await turn_off(hass, NURSE_LEFT)
    await turn_off(hass, NURSE_RIGHT)

    assert posted(server) == []
    assert state(hass, NURSING_TIMER).state == "unknown"
    assert state(hass, NURSE_LEFT).state == "off"


async def test_turning_off_the_idle_side_leaves_the_running_session_alone(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    server: FakeServer,
    freezer: FrozenDateTimeFactory,
) -> None:
    await turn_on(hass, NURSE_LEFT)
    freezer.move_to(minutes(3))

    await turn_off(hass, NURSE_RIGHT)

    assert posted(server) == []
    assert state(hass, NURSE_LEFT).state == "on"
    assert state(hass, NURSING_TIMER).attributes["side"] == "left"
    freezer.move_to(minutes(6))
    await turn_off(hass, NURSE_LEFT)
    [record] = nursing_posts(server)
    assert (record.left_duration, record.right_duration) == (6, 0)


async def test_the_two_nursing_switches_are_never_both_on(
    hass: HomeAssistant, init_integration: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    for minute, entity_id in ((0, NURSE_LEFT), (1, NURSE_RIGHT), (2, NURSE_RIGHT), (3, NURSE_LEFT)):
        freezer.move_to(minutes(minute))
        await turn_on(hass, entity_id)
        states = {state(hass, NURSE_LEFT).state, state(hass, NURSE_RIGHT).state}
        assert states == {"on", "off"}
        assert state(hass, entity_id).state == "on"


async def test_a_session_can_start_again_after_a_stop(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    server: FakeServer,
    freezer: FrozenDateTimeFactory,
) -> None:
    await toggle(hass, NURSE_LEFT)
    freezer.move_to(minutes(2))
    await toggle(hass, NURSE_LEFT)
    freezer.move_to(minutes(10))
    await toggle(hass, NURSE_RIGHT)
    freezer.move_to(minutes(13))
    await toggle(hass, NURSE_RIGHT)

    first, second = nursing_posts(server)
    assert first.object_id != second.object_id
    assert (first.left_duration, first.right_duration) == (2, 0)
    assert (second.left_duration, second.right_duration) == (0, 3)


async def test_two_babies_nursing_switches_run_independently(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    server: FakeServer,
    freezer: FrozenDateTimeFactory,
) -> None:
    await turn_on(hass, NURSE_LEFT)
    freezer.move_to(minutes(1))
    await turn_on(hass, TWO_NURSE_RIGHT)
    freezer.move_to(minutes(6))

    await turn_off(hass, NURSE_LEFT)

    [record] = nursing_posts(server)
    assert record.baby.object_id == BABY_ONE_ID
    assert record.left_duration == 6
    assert state(hass, NURSE_LEFT).state == "off"
    assert state(hass, TWO_NURSE_RIGHT).state == "on"
    assert state(hass, TWO_NURSE_LEFT).state == "off"
    two = state(hass, TWO_NURSING_TIMER)
    assert two.state == minutes(1).isoformat()
    assert two.attributes["side"] == "right"


async def test_a_failed_nursing_write_keeps_the_side_on_and_the_retry_lands_once(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    server: FakeServer,
    freezer: FrozenDateTimeFactory,
) -> None:
    await turn_on(hass, NURSE_LEFT)
    server.post_errors.append((BabyTrackerConnectionError("down"), False))
    freezer.move_to(minutes(5))

    with pytest.raises(HomeAssistantError):
        await turn_off(hass, NURSE_LEFT)
    assert state(hass, NURSE_LEFT).state == "on"
    await turn_off(hass, NURSE_LEFT)

    [record] = nursing_posts(server)
    assert record.left_duration == 5
    assert state(hass, NURSE_LEFT).state == "off"


async def test_stopping_during_a_failing_sync_raises_sync_failed_and_keeps_the_timer(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    server: FakeServer,
    freezer: FrozenDateTimeFactory,
) -> None:
    await turn_on(hass, NURSE_LEFT)
    freezer.move_to(minutes(5))
    server.request_error = BabyTrackerConnectionError("down")

    await _expect_error(hass, NURSE_LEFT, "turn_off", "sync_failed", HomeAssistantError)

    assert posted(server) == []
    server.request_error = None
    await turn_off(hass, NURSE_LEFT)
    [record] = nursing_posts(server)
    assert record.left_duration == 5


async def test_switching_sides_works_while_a_sync_is_failing(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    server: FakeServer,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A start or a switch never syncs or posts, the same as the actions."""
    await turn_on(hass, NURSE_LEFT)
    server.request_error = BabyTrackerConnectionError("down")
    freezer.move_to(minutes(2))

    await turn_on(hass, NURSE_RIGHT)

    assert state(hass, NURSING_TIMER).attributes["side"] == "right"


async def test_a_timer_started_with_a_switch_raises_the_repair_at_an_hour(
    hass: HomeAssistant, init_integration: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    await turn_on(hass, NURSE_LEFT)

    freezer.move_to(minutes(59))
    async_fire_time_changed(hass, minutes(59))
    await hass.async_block_till_done()
    assert _issue(hass, init_integration) is None

    freezer.move_to(minutes(60))
    async_fire_time_changed(hass, minutes(60))
    await hass.async_block_till_done()
    assert _issue(hass, init_integration) is not None


@pytest.mark.parametrize("how", ["turn_off", "discard", "switch_then_off"])
async def test_the_repair_clears_when_the_timer_stops_or_is_discarded(
    hass: HomeAssistant, init_integration: MockConfigEntry, freezer: FrozenDateTimeFactory, how: str
) -> None:
    await turn_on(hass, NURSE_LEFT)
    freezer.move_to(minutes(61))
    async_fire_time_changed(hass, minutes(61))
    await hass.async_block_till_done()
    assert _issue(hass, init_integration) is not None

    if how == "turn_off":
        await turn_off(hass, NURSE_LEFT)
    elif how == "discard":
        await press(hass, DISCARD_NURSING)
    else:
        await turn_on(hass, NURSE_RIGHT)  # a side switch doesn't restart the hour
        assert _issue(hass, init_integration) is not None
        await turn_off(hass, NURSE_RIGHT)

    assert _issue(hass, init_integration) is None


async def test_the_repair_for_one_baby_stays_when_the_other_babys_timer_stops(
    hass: HomeAssistant, init_integration: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    await turn_on(hass, NURSE_LEFT)
    freezer.move_to(minutes(30))
    await turn_on(hass, TWO_NURSE_LEFT)
    freezer.move_to(minutes(61))
    async_fire_time_changed(hass, minutes(61))
    await hass.async_block_till_done()
    assert _issue(hass, init_integration) is not None
    assert _issue(hass, init_integration, BABY_TWO_ID) is None

    await turn_off(hass, TWO_NURSE_LEFT)

    assert _issue(hass, init_integration) is not None


async def test_a_timer_that_cannot_be_saved_still_turns_the_switch_on_and_says_so(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    real_save = Store.async_save

    async def save(self: Store[dict[str, Any]], data: dict[str, Any]) -> None:
        if self.key.endswith(".nursing"):
            raise OSError("disk full")
        await real_save(self, data)

    with patch.object(Store, "async_save", save):
        await _expect_error(hass, NURSE_LEFT, "turn_on", "nursing_not_saved", HomeAssistantError)

    assert state(hass, NURSE_LEFT).state == "on"
