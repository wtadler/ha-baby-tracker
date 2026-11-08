"""Core PR 24: the last milestone sensor."""

from typing import Any

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.baby_tracker.const import DOMAIN

from . import BABY_TWO, NOW, tick
from .factories import DEVICE_A, DEVICE_B, at
from .fake_server import FakeServer

LAST = "sensor.baby_one_last_milestone"
BACK_HOME = "EasyLogSeed3b250600-894f-11e2-9e96-0800200c9a66"
FIRST_SMILE = "EasyLogSeed56cc56b0-894f-11e2-9e96-0800200c9a66"


@pytest.fixture(autouse=True)
def _start_at_now(freezer: FrozenDateTimeFactory) -> None:
    """Freeze the clock at NOW before anything is set up."""
    freezer.move_to(NOW)


def _back_home() -> dict[str, Any]:
    """The built-in milestone as a phone embeds it in a record."""
    return {
        "forAge": 0,
        "used": False,
        "desc": " came home",
        "name": "Back home",
        "newFlage": False,
        "objectID": BACK_HOME,
        "timestamp": "1970-01-01 00:00:00 +0000",
    }


def _milestone(server: FakeServer, number: int, minute: float, **fields: Any) -> None:
    server.add_record(
        DEVICE_A,
        "Milestone",
        f"M0000000-0000-4000-8000-{number:012d}",
        when=at(minute),
        **fields,
    )


def _attributes(hass: HomeAssistant) -> dict[str, Any]:
    state = hass.states.get(LAST)
    assert state is not None
    return dict(state.attributes)


async def test_the_last_milestone_shows_its_name_and_details(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    _milestone(server, 1, 30, note="invented note", milestoneType=_back_home(), hasNewType=False)
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
    assert state.attributes["time"] is None
    assert state.attributes["description"] is None
    assert state.attributes["milestone_id"] is None


async def test_the_newest_milestone_wins(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    _milestone(server, 1, 30, milestoneType=_back_home())
    _milestone(
        server,
        2,
        10,
        milestoneType={**_back_home(), "name": "First smile", "objectID": FIRST_SMILE},
    )
    await tick(hass)

    state = hass.states.get(LAST)
    assert state is not None
    assert state.state == "Back home"


async def test_another_babys_milestone_is_not_shown(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    _milestone(server, 1, 30, baby=BABY_TWO, milestoneType=_back_home())
    await tick(hass)

    state = hass.states.get(LAST)
    assert state is not None
    assert state.state == "unknown"
    two = hass.states.get("sensor.baby_two_last_milestone")
    assert two is not None
    assert two.state == "Back home"


async def test_a_milestone_from_another_device_is_shown(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    server.add_record(
        DEVICE_B,
        "Milestone",
        "M0000000-0000-4000-8000-0000000000B1",
        when=at(5),
        milestoneType=_back_home(),
    )
    await tick(hass)

    state = hass.states.get(LAST)
    assert state is not None
    assert state.state == "Back home"


@pytest.mark.parametrize(
    "milestone_type",
    [None, "Back home", ["Back home"], 5, True, {}, {"name": None}, {"name": 5}, {"name": ["x"]}],
    ids=["null", "string", "list", "int", "bool", "empty", "null-name", "int-name", "list-name"],
)
async def test_a_milestone_without_a_usable_name_is_an_unknown_state(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    server: FakeServer,
    milestone_type: Any,
) -> None:
    _milestone(server, 1, 30, milestoneType=milestone_type)
    await tick(hass)

    state = hass.states.get(LAST)
    assert state is not None
    assert state.state == "unknown"
    assert _attributes(hass)["time"] == at(30).isoformat()


async def test_a_milestone_missing_its_type_key_still_has_a_time(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    _milestone(server, 1, 30)
    await tick(hass)

    attributes = _attributes(hass)
    assert attributes["time"] == at(30).isoformat()
    assert attributes["description"] is None
    assert attributes["milestone_id"] is None


@pytest.mark.parametrize("description", [None, "", "   ", 5, True, ["x"], {"a": 1}])
async def test_a_description_that_is_not_text_reads_as_no_description(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, description: Any
) -> None:
    _milestone(server, 1, 30, milestoneType={**_back_home(), "desc": description})
    await tick(hass)

    assert _attributes(hass)["description"] is None


@pytest.mark.parametrize("object_id", [None, 5, True, ["x"], {"a": 1}])
async def test_an_id_that_is_not_text_reads_as_no_id(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, object_id: Any
) -> None:
    _milestone(server, 1, 30, milestoneType={**_back_home(), "objectID": object_id})
    await tick(hass)

    assert _attributes(hass)["milestone_id"] is None


async def test_a_description_is_the_babys_name_then_the_wording_with_spaces_tidied(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    _milestone(server, 1, 30, milestoneType={**_back_home(), "desc": "'s   first  smile "})
    await tick(hass)

    assert _attributes(hass)["description"] == "Baby One's first smile"


@pytest.mark.parametrize("note", [5, ["x"], {"a": 1}, True])
async def test_a_note_that_is_not_text_reads_as_no_note(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, note: Any
) -> None:
    _milestone(server, 1, 30, milestoneType=_back_home(), note=note)
    await tick(hass)

    assert _attributes(hass)["note"] is None


async def test_there_is_no_action_to_log_a_milestone(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """Test logging milestones is out of scope: only the sensor is offered."""
    assert not hass.services.has_service(DOMAIN, "log_milestone")
