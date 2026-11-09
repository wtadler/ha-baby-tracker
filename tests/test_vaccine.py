"""Core PR 25: the last vaccine sensor."""

from typing import Any

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.baby_tracker.const import DOMAIN

from . import BABY_TWO, NOW, tick
from .factories import DEVICE_A, at
from .fake_server import FakeServer

LAST = "sensor.baby_one_last_vaccine"
DTAP = "EasyLogSeed003effe4a50-8805-11e2-9e96-0800200c9a66"


@pytest.fixture(autouse=True)
def _start_at_now(freezer: FrozenDateTimeFactory) -> None:
    """Freeze the clock at NOW before anything is set up."""
    freezer.move_to(NOW)


def _dtap() -> dict[str, Any]:
    """The built-in vaccine as a phone embeds it in a record."""
    return {
        "name": "DTaP",
        "desc": "Diphtheria, Tetanus, Pertussis",
        "forAge": 2,
        "used": False,
        "newFlage": False,
        "objectID": DTAP,
        "timestamp": "1970-01-01 00:00:00 +0000",
    }


def _vaccine(server: FakeServer, number: int, minute: float, **fields: Any) -> None:
    server.add_record(
        DEVICE_A,
        "Vaccine",
        f"V0000000-0000-4000-8000-{number:012d}",
        when=at(minute),
        **fields,
    )


def _attributes(hass: HomeAssistant) -> dict[str, Any]:
    state = hass.states.get(LAST)
    assert state is not None
    return dict(state.attributes)


async def test_the_last_vaccine_shows_its_name_and_details(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    _vaccine(server, 1, 30, note="2-month visit", vaccineType=_dtap())
    await tick(hass)

    state = hass.states.get(LAST)
    assert state is not None
    assert state.state == "DTaP"
    assert state.attributes["description"] == "Diphtheria, Tetanus, Pertussis"
    assert state.attributes["vaccine_id"] == DTAP
    assert state.attributes["time"] == at(30).isoformat()
    assert state.attributes["note"] == "2-month visit"


async def test_a_baby_without_vaccines_has_an_unknown_last_vaccine(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    state = hass.states.get(LAST)
    assert state is not None
    assert state.state == "unknown"
    assert state.attributes["time"] is None


async def test_the_newest_vaccine_wins(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    _vaccine(server, 1, 30, vaccineType=_dtap())
    _vaccine(server, 2, 10, vaccineType={**_dtap(), "name": "HepB"})
    await tick(hass)

    state = hass.states.get(LAST)
    assert state is not None
    assert state.state == "DTaP"


async def test_another_babys_vaccine_is_not_shown(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    _vaccine(server, 1, 30, baby=BABY_TWO, vaccineType=_dtap())
    await tick(hass)

    state = hass.states.get(LAST)
    assert state is not None
    assert state.state == "unknown"


@pytest.mark.parametrize(
    "vaccine_type",
    [None, "DTaP", ["DTaP"], 5, True, {}, {"name": None}, {"name": 5}],
    ids=["null", "string", "list", "int", "bool", "empty", "null-name", "int-name"],
)
async def test_a_vaccine_without_a_usable_name_is_an_unknown_state(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    server: FakeServer,
    vaccine_type: Any,
) -> None:
    _vaccine(server, 1, 30, vaccineType=vaccine_type)
    await tick(hass)

    state = hass.states.get(LAST)
    assert state is not None
    assert state.state == "unknown"
    assert _attributes(hass)["time"] == at(30).isoformat()


async def test_a_vaccine_missing_its_type_key_still_has_a_time(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    _vaccine(server, 1, 30)
    await tick(hass)

    attributes = _attributes(hass)
    assert attributes["time"] == at(30).isoformat()
    assert attributes["description"] is None
    assert attributes["vaccine_id"] is None


@pytest.mark.parametrize("description", [None, "", 5, True, ["x"], {"a": 1}])
async def test_a_description_that_is_not_text_reads_as_no_description(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, description: Any
) -> None:
    _vaccine(server, 1, 30, vaccineType={**_dtap(), "desc": description})
    await tick(hass)

    assert _attributes(hass)["description"] is None


@pytest.mark.parametrize("object_id", [None, 5, True, ["x"]])
async def test_an_id_that_is_not_text_reads_as_no_id(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer, object_id: Any
) -> None:
    _vaccine(server, 1, 30, vaccineType={**_dtap(), "objectID": object_id})
    await tick(hass)

    assert _attributes(hass)["vaccine_id"] is None


async def test_there_is_no_action_to_log_a_vaccine(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """Test logging vaccines is out of scope: only the sensor is offered."""
    assert not hass.services.has_service(DOMAIN, "log_vaccine")
