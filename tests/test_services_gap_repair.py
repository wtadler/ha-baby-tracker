"""Core PR 11: the repair issue for a gap in this device's own log."""

from datetime import timedelta

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from . import EMAIL, OWN_DEVICE
from .fake_server import FakeServer
from .helpers import (
    NOW,
    fail_to_write,
    gap_issue,
)


@pytest.fixture(autouse=True)
def _start_at_now(freezer: FrozenDateTimeFactory) -> None:
    """Freeze the clock at NOW before anything is set up.

    Timers scheduled at setup then share the clock the tests move, so moving it forward and
    ticking reaches them the way it does in core's tests.
    """
    freezer.move_to(NOW)


async def test_a_gap_in_this_devices_own_log_raises_but_creates_no_repair_at_first(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    # The service says this device's log is at 5, but nothing can be read back.
    server.last_sync_override[OWN_DEVICE] = 5

    await fail_to_write(hass)

    assert server.posted == []
    assert gap_issue(hass, init_integration) is None


async def test_a_gap_that_keeps_failing_for_an_hour_creates_a_repair_issue(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    server: FakeServer,
    freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to(NOW)
    server.last_sync_override[OWN_DEVICE] = 5
    await fail_to_write(hass)

    freezer.tick(timedelta(hours=1, minutes=1))
    await fail_to_write(hass)

    issue = gap_issue(hass, init_integration)
    assert issue is not None
    assert issue.translation_key == "own_log_gap"
    assert issue.severity is ir.IssueSeverity.ERROR
    assert issue.is_fixable is False
    assert issue.translation_placeholders == {"account": EMAIL}


async def test_a_gap_still_failing_just_under_an_hour_later_creates_no_repair(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    server: FakeServer,
    freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to(NOW)
    server.last_sync_override[OWN_DEVICE] = 5
    await fail_to_write(hass)

    freezer.tick(timedelta(minutes=59))
    await fail_to_write(hass)

    assert gap_issue(hass, init_integration) is None


async def test_many_quick_failures_do_not_add_up_to_a_repair(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    server: FakeServer,
    freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to(NOW)
    server.last_sync_override[OWN_DEVICE] = 5

    for _ in range(10):
        await fail_to_write(hass)
        freezer.tick(timedelta(minutes=5))

    assert gap_issue(hass, init_integration) is None  # 45 minutes in
