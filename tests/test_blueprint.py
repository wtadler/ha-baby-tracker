"""HACS only: the live activity blueprint, its schema, and its behavior driven through a real
automation. Blueprints do not go to core.

The sensors are plain states (the timer sensors' own tests live in ``test_timers``), and the
Companion app's notify service is a recorder, so the assertions are on the payloads the
documented Live Activity API would receive.
"""

import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from homeassistant.components.blueprint import (
    BLUEPRINT_SCHEMA,
    Blueprint,
    BlueprintInputs,
)
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.setup import async_setup_component
from homeassistant.util import yaml as yaml_util
from pytest_homeassistant_custom_component.common import async_mock_service

BLUEPRINT = (
    Path(__file__).parent.parent
    / "blueprints"
    / "automation"
    / "baby_tracker"
    / "live_activity.yaml"
)
BLUEPRINT_PATH = "baby_tracker/live_activity.yaml"

SLEEP = "sensor.baby_one_sleep_timer"
NURSING = "sensor.baby_one_nursing_timer"
TWO_SLEEP = "sensor.baby_two_sleep_timer"
TWO_NURSING = "sensor.baby_two_nursing_timer"
NOTIFY = "notify.mobile_app_test_phone"

STARTED = datetime(2026, 10, 1, 3, 0, tzinfo=UTC)
TAG_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_")


def nursing_attributes(
    side: str, *, left: int = 0, right: int = 0, side_started: datetime = STARTED
):
    return {
        "side": side,
        "side_started": side_started.isoformat(),
        "left_seconds": left,
        "right_seconds": right,
    }


def inputs(**extra: Any) -> dict[str, Any]:
    return {
        "sleep_timer": SLEEP,
        "nursing_timer": NURSING,
        "notify_service": NOTIFY,
        **extra,
    }


def load_blueprint() -> Blueprint:
    return Blueprint(yaml_util.load_yaml(str(BLUEPRINT)), schema=BLUEPRINT_SCHEMA)


# --- the file itself -------------------------------------------------------------------------


def test_the_blueprint_passes_home_assistants_blueprint_schema() -> None:
    blueprint = load_blueprint()
    assert blueprint.domain == "automation"
    assert {"sleep_timer", "nursing_timer", "notify_service"} <= set(blueprint.inputs)


def test_the_timer_inputs_are_entity_selectors_limited_to_this_integrations_sensors() -> None:
    blueprint = load_blueprint()
    for name in ("sleep_timer", "nursing_timer"):
        selector = blueprint.inputs[name]["selector"]["entity"]
        assert selector["domain"] == ["sensor"]
        assert selector["integration"] == "baby_tracker"
        assert selector["device_class"] == ["timestamp"]


def test_the_titles_are_optional_with_defaults() -> None:
    blueprint = load_blueprint()
    for name in ("sleep_title", "nursing_title"):
        assert blueprint.inputs[name]["default"]


def test_the_blueprint_runs_queued_so_one_timers_events_stay_in_order() -> None:
    raw = yaml_util.load_yaml(str(BLUEPRINT))
    assert raw["mode"] == "queued"


def test_a_blueprint_instance_missing_an_input_is_refused() -> None:
    blueprint = load_blueprint()
    bad = BlueprintInputs(
        blueprint,
        {"use_blueprint": {"path": BLUEPRINT_PATH, "input": {"sleep_timer": SLEEP}}},
    )
    with pytest.raises(Exception, match="nursing_timer|notify_service"):
        bad.validate()


# --- behavior --------------------------------------------------------------------------------


@pytest.fixture
def calls(hass: HomeAssistant) -> list[ServiceCall]:
    """Every notify call the Companion app's service would receive."""
    return async_mock_service(hass, "notify", "mobile_app_test_phone")


async def install(hass: HomeAssistant, *instances: dict[str, Any]) -> None:
    """Copy the blueprint into the test config and set up one automation per input set."""
    target = Path(hass.config.path("blueprints", "automation", BLUEPRINT_PATH))
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(BLUEPRINT, target)
    for sensor in (SLEEP, NURSING, TWO_SLEEP, TWO_NURSING):
        hass.states.async_set(sensor, "unknown")
    config = [
        {
            "alias": f"live activity {index}",
            "use_blueprint": {"path": BLUEPRINT_PATH, "input": instance},
        }
        for index, instance in enumerate(instances)
    ]
    assert await async_setup_component(hass, "automation", {"automation": config})
    await hass.async_block_till_done()


async def set_state(
    hass: HomeAssistant, entity_id: str, state: str, attributes: dict[str, Any] | None = None
) -> None:
    hass.states.async_set(entity_id, state, attributes or {})
    await hass.async_block_till_done()


def payload(call: ServiceCall) -> dict[str, Any]:
    """The nested ``data:`` block the Companion app reads."""
    return call.data["data"]


async def test_a_sleep_start_starts_a_live_activity_counting_up_from_the_start(
    hass: HomeAssistant, calls: list[ServiceCall]
) -> None:
    await install(hass, inputs())

    await set_state(hass, SLEEP, STARTED.isoformat())

    assert len(calls) == 1
    data = payload(calls[0])
    assert data["live_update"] is True
    assert data["chronometer"] is True
    assert data["when"] == STARTED.timestamp()
    assert not data.get("when_relative")
    assert calls[0].data["title"]
    assert calls[0].data["message"]


async def test_the_sleep_activity_tag_is_per_baby_and_uses_only_allowed_characters(
    hass: HomeAssistant, calls: list[ServiceCall]
) -> None:
    await install(hass, inputs())

    await set_state(hass, SLEEP, STARTED.isoformat())

    tag = payload(calls[0])["tag"]
    assert "sleep" in tag
    assert "baby_one" in tag
    assert set(tag) <= TAG_CHARS
    assert len(tag) <= 64


async def test_a_sleep_stop_ends_the_activity_with_clear_notification_and_the_same_tag(
    hass: HomeAssistant, calls: list[ServiceCall]
) -> None:
    await install(hass, inputs())
    await set_state(hass, SLEEP, STARTED.isoformat())

    await set_state(hass, SLEEP, "unknown")

    assert len(calls) == 2
    assert calls[1].data["message"] == "clear_notification"
    assert payload(calls[1]) == {"tag": payload(calls[0])["tag"]}


async def test_a_nursing_start_shows_the_side_and_counts_from_the_session_start(
    hass: HomeAssistant, calls: list[ServiceCall]
) -> None:
    await install(hass, inputs())

    await set_state(hass, NURSING, STARTED.isoformat(), nursing_attributes("left"))

    assert len(calls) == 1, (
        "the state and side changing together is one start, not a start and a switch"
    )
    data = payload(calls[0])
    assert data["live_update"] is True
    assert data["chronometer"] is True
    assert data["when"] == STARTED.timestamp()
    assert "nursing" in data["tag"]
    assert "left" in calls[0].data["message"].lower()


async def test_a_side_switch_updates_the_same_activity_with_the_new_side(
    hass: HomeAssistant, calls: list[ServiceCall]
) -> None:
    await install(hass, inputs())
    await set_state(hass, NURSING, STARTED.isoformat(), nursing_attributes("left"))

    switched = STARTED + timedelta(minutes=5)
    await set_state(
        hass,
        NURSING,
        STARTED.isoformat(),
        nursing_attributes("right", left=300, side_started=switched),
    )

    assert len(calls) == 2
    first, update = calls
    assert payload(update)["tag"] == payload(first)["tag"]
    assert payload(update)["live_update"] is True
    assert "right" in update.data["message"].lower()
    assert "left" not in update.data["message"].lower()
    # The elapsed time is the whole session's, so it keeps counting through the switch.
    assert payload(update)["when"] == STARTED.timestamp()


async def test_switching_back_updates_again(hass: HomeAssistant, calls: list[ServiceCall]) -> None:
    await install(hass, inputs())
    await set_state(hass, NURSING, STARTED.isoformat(), nursing_attributes("left"))
    await set_state(hass, NURSING, STARTED.isoformat(), nursing_attributes("right", left=300))
    await set_state(
        hass, NURSING, STARTED.isoformat(), nursing_attributes("left", left=300, right=120)
    )

    assert [("left" in c.data["message"].lower()) for c in calls] == [True, False, True]


async def test_a_change_that_keeps_the_side_sends_nothing(
    hass: HomeAssistant, calls: list[ServiceCall]
) -> None:
    await install(hass, inputs())
    await set_state(hass, NURSING, STARTED.isoformat(), nursing_attributes("left"))

    await set_state(hass, NURSING, STARTED.isoformat(), nursing_attributes("left", left=0, right=0))
    await set_state(hass, NURSING, STARTED.isoformat(), {**nursing_attributes("left"), "x": 1})

    assert len(calls) == 1


async def test_a_nursing_stop_ends_the_activity(
    hass: HomeAssistant, calls: list[ServiceCall]
) -> None:
    await install(hass, inputs())
    await set_state(hass, NURSING, STARTED.isoformat(), nursing_attributes("left"))

    await set_state(hass, NURSING, "unknown")

    assert len(calls) == 2
    assert calls[1].data["message"] == "clear_notification"
    assert payload(calls[1])["tag"] == payload(calls[0])["tag"]


async def test_sleep_and_nursing_use_distinct_tags(
    hass: HomeAssistant, calls: list[ServiceCall]
) -> None:
    await install(hass, inputs())

    await set_state(hass, SLEEP, STARTED.isoformat())
    await set_state(hass, NURSING, STARTED.isoformat(), nursing_attributes("left"))

    assert len(calls) == 2
    assert payload(calls[0])["tag"] != payload(calls[1])["tag"]


async def test_ending_one_timer_leaves_the_other_activity_running(
    hass: HomeAssistant, calls: list[ServiceCall]
) -> None:
    await install(hass, inputs())
    await set_state(hass, SLEEP, STARTED.isoformat())
    await set_state(hass, NURSING, STARTED.isoformat(), nursing_attributes("left"))

    await set_state(hass, NURSING, "unknown")

    assert [c.data["message"] == "clear_notification" for c in calls] == [False, False, True]
    assert payload(calls[2])["tag"] == payload(calls[1])["tag"]
    assert payload(calls[2])["tag"] != payload(calls[0])["tag"]


async def test_two_babies_get_distinct_tags(hass: HomeAssistant, calls: list[ServiceCall]) -> None:
    await install(hass, inputs(), inputs(sleep_timer=TWO_SLEEP, nursing_timer=TWO_NURSING))

    await set_state(hass, SLEEP, STARTED.isoformat())
    await set_state(hass, TWO_SLEEP, STARTED.isoformat())
    await set_state(hass, NURSING, STARTED.isoformat(), nursing_attributes("left"))
    await set_state(hass, TWO_NURSING, STARTED.isoformat(), nursing_attributes("left"))

    tags = [payload(call)["tag"] for call in calls]
    assert len(tags) == 4
    assert len(set(tags)) == 4


async def test_a_second_babys_stop_clears_only_that_babys_activity(
    hass: HomeAssistant, calls: list[ServiceCall]
) -> None:
    await install(hass, inputs(), inputs(sleep_timer=TWO_SLEEP, nursing_timer=TWO_NURSING))
    await set_state(hass, SLEEP, STARTED.isoformat())
    await set_state(hass, TWO_SLEEP, STARTED.isoformat())

    await set_state(hass, TWO_SLEEP, "unknown")

    assert calls[-1].data["message"] == "clear_notification"
    assert payload(calls[-1])["tag"] == payload(calls[1])["tag"]
    assert payload(calls[-1])["tag"] != payload(calls[0])["tag"]


async def test_custom_titles_are_used_for_each_activity(
    hass: HomeAssistant, calls: list[ServiceCall]
) -> None:
    await install(hass, inputs(sleep_title="Nap time", nursing_title="Feeding"))

    await set_state(hass, SLEEP, STARTED.isoformat())
    await set_state(hass, NURSING, STARTED.isoformat(), nursing_attributes("left"))

    assert calls[0].data["title"] == "Nap time"
    assert calls[1].data["title"] == "Feeding"


async def test_default_titles_differ_between_sleep_and_nursing(
    hass: HomeAssistant, calls: list[ServiceCall]
) -> None:
    await install(hass, inputs())

    await set_state(hass, SLEEP, STARTED.isoformat())
    await set_state(hass, NURSING, STARTED.isoformat(), nursing_attributes("left"))

    assert calls[0].data["title"] != calls[1].data["title"]


async def test_going_unavailable_does_not_end_a_running_activity(
    hass: HomeAssistant, calls: list[ServiceCall]
) -> None:
    await install(hass, inputs())
    await set_state(hass, SLEEP, STARTED.isoformat())

    await set_state(hass, SLEEP, "unavailable")

    assert len(calls) == 1


async def test_recovering_from_unavailable_to_a_running_timer_starts_the_activity(
    hass: HomeAssistant, calls: list[ServiceCall]
) -> None:
    await install(hass, inputs())
    await set_state(hass, SLEEP, "unavailable")

    await set_state(hass, SLEEP, STARTED.isoformat())

    assert len(calls) == 1
    assert payload(calls[0])["live_update"] is True


async def test_a_timer_that_goes_unavailable_then_unknown_still_ends_the_activity(
    hass: HomeAssistant, calls: list[ServiceCall]
) -> None:
    await install(hass, inputs())
    await set_state(hass, SLEEP, STARTED.isoformat())
    await set_state(hass, SLEEP, "unavailable")

    await set_state(hass, SLEEP, "unknown")

    assert len(calls) == 2
    assert calls[1].data["message"] == "clear_notification"
    assert payload(calls[1])["tag"] == payload(calls[0])["tag"]


async def test_a_nursing_timer_that_goes_unavailable_then_unknown_still_ends_the_activity(
    hass: HomeAssistant, calls: list[ServiceCall]
) -> None:
    await install(hass, inputs())
    await set_state(hass, NURSING, STARTED.isoformat(), nursing_attributes("left"))
    await set_state(hass, NURSING, "unavailable")

    await set_state(hass, NURSING, "unknown")

    assert calls[-1].data["message"] == "clear_notification"
    assert payload(calls[-1])["tag"] == payload(calls[0])["tag"]


async def test_unavailable_and_back_to_the_same_running_timer_neither_restarts_nor_clears(
    hass: HomeAssistant, calls: list[ServiceCall]
) -> None:
    await install(hass, inputs())
    await set_state(hass, SLEEP, STARTED.isoformat())

    await set_state(hass, SLEEP, "unavailable")
    await set_state(hass, SLEEP, STARTED.isoformat())

    assert not any(c.data["message"] == "clear_notification" for c in calls)
    # Any re-send is an in-place update of the same activity: same tag, same start.
    assert {payload(c)["tag"] for c in calls} == {payload(calls[0])["tag"]}
    assert {payload(c)["when"] for c in calls} == {STARTED.timestamp()}


async def test_a_timer_that_is_unavailable_then_unknown_with_nothing_running_sends_a_harmless_clear(
    hass: HomeAssistant, calls: list[ServiceCall]
) -> None:
    await install(hass, inputs())
    await set_state(hass, SLEEP, "unavailable")

    await set_state(hass, SLEEP, "unknown")

    assert [c.data["message"] for c in calls] == ["clear_notification"]


async def test_an_already_running_timer_at_setup_sends_nothing_until_it_changes(
    hass: HomeAssistant, calls: list[ServiceCall]
) -> None:
    hass.states.async_set(SLEEP, STARTED.isoformat())
    await install_without_resetting(hass)

    assert calls == []


async def install_without_resetting(hass: HomeAssistant) -> None:
    target = Path(hass.config.path("blueprints", "automation", BLUEPRINT_PATH))
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(BLUEPRINT, target)
    hass.states.async_set(NURSING, "unknown")
    config = [{"use_blueprint": {"path": BLUEPRINT_PATH, "input": inputs()}}]
    assert await async_setup_component(hass, "automation", {"automation": config})
    await hass.async_block_till_done()


async def test_rapid_start_then_stop_reach_the_notify_service_in_order(
    hass: HomeAssistant, calls: list[ServiceCall]
) -> None:
    await install(hass, inputs())

    hass.states.async_set(SLEEP, STARTED.isoformat())
    hass.states.async_set(SLEEP, "unknown")
    await hass.async_block_till_done()

    assert [c.data["message"] == "clear_notification" for c in calls] == [False, True]


async def test_a_nursing_start_while_sleeping_is_independent_of_the_sleep_activity(
    hass: HomeAssistant, calls: list[ServiceCall]
) -> None:
    await install(hass, inputs())
    await set_state(hass, SLEEP, STARTED.isoformat())

    await set_state(hass, NURSING, STARTED.isoformat(), nursing_attributes("right"))

    assert len(calls) == 2
    assert "right" in calls[1].data["message"].lower()
