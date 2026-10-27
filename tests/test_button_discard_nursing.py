"""Core PR 20: the Discard button for a nursing timer."""

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.nighp_baby_tracker.const import (
    DOMAIN,
)

from .factories import (
    BABY_ONE_ID,
)
from .fake_server import FakeServer
from .helpers import (
    DISCARD_NURSING,
    NOW,
    NURSE_LEFT,
    NURSING_TIMER,
    get_baby_device,
    nurse,
    nursing_posts,
    posted,
    press,
    state,
    toggle,
)

TWO_NURSING_TIMER = "sensor.baby_two_nursing_timer"


@pytest.fixture(autouse=True)
def _start_at_now(freezer: FrozenDateTimeFactory) -> None:
    """Freeze the clock at NOW before anything is set up.

    Timers scheduled at setup then share the clock the tests move, so moving it forward and
    ticking reaches them the way it does in core's tests.
    """
    freezer.move_to(NOW)


async def test_the_discard_button_drops_the_timer_and_posts_nothing(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    server: FakeServer,
    freezer: FrozenDateTimeFactory,
) -> None:
    await nurse(hass, freezer, (0, "left"), (3, "right"))

    await press(hass, DISCARD_NURSING)

    assert posted(server) == []
    assert state(hass, NURSING_TIMER).state == "unknown"
    assert state(hass, NURSE_LEFT).state == "off"
    assert state(hass, "switch.baby_one_nursing_right").state == "off"


async def test_the_discard_button_with_no_timer_raises_the_translated_error(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    with pytest.raises(ServiceValidationError) as err:
        await press(hass, DISCARD_NURSING)

    assert err.value.translation_key == "no_nursing_timer"
    assert err.value.translation_domain == DOMAIN
    assert posted(server) == []


async def test_the_discard_button_leaves_the_other_babys_timer_running(
    hass: HomeAssistant, init_integration: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    await nurse(hass, freezer, (0, "left"))
    await toggle(hass, "switch.baby_two_nursing_right")

    await press(hass, DISCARD_NURSING)

    assert state(hass, "switch.baby_two_nursing_right").state == "on"
    assert state(hass, TWO_NURSING_TIMER).state == NOW.isoformat()


async def test_a_discarded_session_can_start_again_and_logs_only_the_new_one(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    server: FakeServer,
    freezer: FrozenDateTimeFactory,
) -> None:
    await nurse(hass, freezer, (0, "left"), (5, "left"))
    await nurse(hass, freezer, (6, "right"))
    await press(hass, DISCARD_NURSING)
    await nurse(hass, freezer, (7, "right"), (9, "right"))

    [record] = [r for r in nursing_posts(server) if r.right_duration]
    assert (record.left_duration, record.right_duration) == (0, 2)


async def test_discard_is_the_discard_nursing_action(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    server: FakeServer,
    freezer: FrozenDateTimeFactory,
) -> None:
    await nurse(hass, freezer, (0, "left"))
    await press(hass, DISCARD_NURSING)

    with pytest.raises(ServiceValidationError) as via_action:
        await hass.services.async_call(
            DOMAIN,
            "discard_nursing",
            {"device_id": get_baby_device(dr.async_get(hass), BABY_ONE_ID).id},  # type: ignore[union-attr]
            blocking=True,
        )
    assert via_action.value.translation_key == "no_nursing_timer"
