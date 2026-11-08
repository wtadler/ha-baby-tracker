"""Core PR 24: the last milestone sensor and the log_milestone action."""

from typing import Any

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from pybabytracker import Milestone
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.baby_tracker.const import DOMAIN

from . import NOW, tick
from .factories import BABY_ONE_ID, BABY_TWO_ID, DEVICE_A, at
from .fake_server import FakeServer
from .helpers import device_id, posted

LAST = "sensor.baby_one_last_milestone"
BACK_HOME = "EasyLogSeed3b250600-894f-11e2-9e96-0800200c9a66"
FIRST_SMILE = "EasyLogSeed56cc56b0-894f-11e2-9e96-0800200c9a66"


@pytest.fixture(autouse=True)
def _start_at_now(freezer: FrozenDateTimeFactory) -> None:
    """Freeze the clock at NOW before anything is set up."""
    freezer.move_to(NOW)


def _back_home() -> dict[str, Any]:
    """The built-in milestone as an Android phone embedded it."""
    return {
        "forAge": 0,
        "used": False,
        "desc": " came home",
        "name": "Back home",
        "newFlage": False,
        "objectID": BACK_HOME,
        "timestamp": "1970-01-01 00:00:00 +0000",
    }


async def _log(hass: HomeAssistant, baby_id: str, milestone: str, **extra: Any) -> None:
    data = {"device_id": device_id(hass, baby_id), "milestone": milestone, **extra}
    await hass.services.async_call(DOMAIN, "log_milestone", data, blocking=True)


async def test_the_last_milestone_shows_its_name_and_details(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    server.add_record(
        DEVICE_A,
        "Milestone",
        "M0000000-0000-4000-8000-000000000001",
        when=at(30),
        note="invented note",
        milestoneType=_back_home(),
        hasNewType=False,
    )
    await tick(hass)

    state = hass.states.get(LAST)
    assert state is not None
    assert state.state == "Back home"
    assert state.attributes["time"] == at(30).isoformat()
    assert state.attributes["description"] == "Baby One came home"
    assert state.attributes["milestone_id"] == BACK_HOME
    assert state.attributes["note"] == "invented note"


async def test_a_baby_without_milestones_has_an_unknown_last_milestone(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    state = hass.states.get(LAST)
    assert state is not None
    assert state.state == "unknown"


async def test_log_milestone_posts_the_apps_built_in_milestone(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    await _log(hass, BABY_ONE_ID, "first smile", note="At grandma's")

    [(_, _, record)] = posted(server)
    assert isinstance(record, Milestone)
    assert record.milestone_type is not None
    assert record.milestone_type["objectID"] == FIRST_SMILE
    assert record.milestone_name == "First smile"
    assert record.note == "At grandma's"
    assert record.baby is not None
    assert record.baby.object_id == BABY_ONE_ID
    assert record.time == NOW
    state = hass.states.get(LAST)
    assert state is not None
    assert state.state == "First smile"
    assert state.attributes["description"] == "Baby One's first smile"


async def test_log_milestone_without_a_note_says_it_came_from_home_assistant(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    await _log(hass, BABY_TWO_ID, "Rolls over")

    [(_, _, record)] = posted(server)
    assert record.note == "Added from Home Assistant"


async def test_a_milestone_added_in_the_app_can_be_logged(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    custom = {
        "BCObjectType": "MilestoneSelection",
        "objectID": "C0000000-0000-4000-8000-000000000001",
        "name": "First swim",
        "desc": " swam",
        "forAge": 0,
        "used": "false",
        "newFlage": "true",
        "timestamp": "2026-10-20 08:00:00 +0000",
    }
    server.add_payload(DEVICE_A, custom)
    await tick(hass)

    await _log(hass, BABY_ONE_ID, "First swim")

    [(_, _, record)] = posted(server)
    assert record.milestone_type == custom


async def test_an_unknown_milestone_is_refused_by_name_with_nothing_posted(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    with pytest.raises(ServiceValidationError) as raised:
        await _log(hass, BABY_ONE_ID, "Learns to fly")

    assert raised.value.translation_key == "unknown_milestone"
    assert raised.value.translation_placeholders == {"milestone": "Learns to fly"}
    assert server.posted == []
