"""Core PR 25: the last vaccine sensor and the log_vaccine action."""

from typing import Any

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from pybabytracker import Vaccine
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.baby_tracker.const import DOMAIN

from . import NOW
from .factories import BABY_ONE_ID
from .fake_server import FakeServer
from .helpers import device_id, posted

LAST = "sensor.baby_one_last_vaccine"
DTAP = "EasyLogSeed003effe4a50-8805-11e2-9e96-0800200c9a66"


@pytest.fixture(autouse=True)
def _start_at_now(freezer: FrozenDateTimeFactory) -> None:
    """Freeze the clock at NOW before anything is set up."""
    freezer.move_to(NOW)


async def _log(hass: HomeAssistant, vaccine: str, **extra: Any) -> None:
    data = {"device_id": device_id(hass, BABY_ONE_ID), "vaccine": vaccine, **extra}
    await hass.services.async_call(DOMAIN, "log_vaccine", data, blocking=True)


async def test_log_vaccine_posts_the_apps_built_in_vaccine_and_the_sensor_shows_it(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    await _log(hass, "dtap", note="2-month visit")

    [(_, _, record)] = posted(server)
    assert isinstance(record, Vaccine)
    assert record.vaccine_type is not None
    assert record.vaccine_type["objectID"] == DTAP
    assert record.note == "2-month visit"
    state = hass.states.get(LAST)
    assert state is not None
    assert state.state == "DTaP"
    assert state.attributes["description"] == "Diphtheria, Tetanus, Pertussis"
    assert state.attributes["vaccine_id"] == DTAP
    assert state.attributes["time"] == NOW.isoformat()


async def test_a_baby_without_vaccines_has_an_unknown_last_vaccine(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    state = hass.states.get(LAST)
    assert state is not None
    assert state.state == "unknown"


async def test_an_unknown_vaccine_is_refused_by_name_with_nothing_posted(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    with pytest.raises(ServiceValidationError) as raised:
        await _log(hass, "Smallpox")

    assert raised.value.translation_key == "unknown_vaccine"
    assert raised.value.translation_placeholders == {"vaccine": "Smallpox"}
    assert server.posted == []
