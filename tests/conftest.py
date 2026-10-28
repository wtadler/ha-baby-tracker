"""Fixtures for Baby Tracker (Nighp) tests.

The library's client is faked at its public interface by ``FakeServer`` (see ``fake_server.py``).
The real sync engine runs on top of the fake, so the tests exercise the real merging of logs.
"""

from collections.abc import Generator, Iterator
from datetime import timedelta  # Core PR 2
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from freezegun.api import FrozenDateTimeFactory  # Core PR 2
from homeassistant.config_entries import ConfigEntryState  # Core PR 5
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
)

from custom_components.baby_tracker.const import CONF_DEVICE_UUID, DOMAIN

from . import (
    BABY_ONE,  # Core PR 7
    BABY_THREE,  # Core PR 5
    BABY_TWO,
    EMAIL,
    EMAIL_TWO,  # Core PR 5
    ENTRY_ID,
    OWN_DEVICE,
    OWN_DEVICE_TWO,  # Core PR 5
    PASSWORD,
    setup_integration,
)
from .factories import (
    DEVICE_A,
    DEVICE_B,  # Core PR 5
    at,
    volume,
)
from .fake_server import FakeClient, FakeServer
from .helpers import LOCAL_NOW  # Core PR 2


# HACS-only: core's tests run in core's environment, where the recorder fixtures work as they are
@pytest.fixture
def recorder_db_url() -> str:
    """An in-memory database for the recorder.

    The plugin's own fixture refuses to run once ``hass`` exists, and this suite's autouse
    fixtures create ``hass`` first. The in-memory URL is what it would return anyway.
    """
    return "sqlite://"


# HACS-only: see recorder_db_url
@pytest.fixture(autouse=True)
def recorder_annotations(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the recorder's annotations resolvable on Python 3.14.

    Core imports ``Recorder`` and ``Session`` in these modules only for type checking, and the
    plugin inspects their signatures when it patches them. Python 3.14 evaluates annotations
    lazily, so the missing names raise a ``NameError`` unless they are defined.
    """
    from homeassistant.components.recorder import Recorder
    from homeassistant.components.recorder import migration as recorder_migration
    from homeassistant.helpers import recorder as recorder_helpers
    from sqlalchemy.orm import Session

    monkeypatch.setattr(recorder_migration, "Recorder", Recorder, raising=False)
    monkeypatch.setattr(recorder_migration, "Session", Session, raising=False)
    monkeypatch.setattr(recorder_helpers, "Session", Session, raising=False)


# HACS-only: core's tests load core's integrations
@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> Iterator[None]:
    """Load custom_components/ in every test."""
    yield


@pytest.fixture
def mock_setup_entry() -> Generator[AsyncMock]:
    """Override async_setup_entry, so a config flow test doesn't set the integration up."""
    with patch(
        "custom_components.baby_tracker.async_setup_entry",
        return_value=True,  # Core: return_value=None,
    ) as mock_setup_entry:
        yield mock_setup_entry


@pytest.fixture
def server() -> FakeServer:
    """A fake sync service with one phone that has logged for two babies."""
    server = FakeServer()
    server.names[DEVICE_A] = "Test Phone"
    server.add_record(
        DEVICE_A,
        "Diaper",
        "D0000000-0000-4000-8000-000000000001",
        when=at(0),
        status=0,
    )
    server.add_record(
        DEVICE_A,
        "Diaper",
        "D0000000-0000-4000-8000-000000000002",
        when=at(10),
        baby=BABY_TWO,
        status=1,
    )
    return server


# Core PR 5
@pytest.fixture
def second_server() -> FakeServer:
    """A second account's service, with one phone that has logged for a third baby."""
    server = FakeServer()
    server.names[DEVICE_B] = "Other Phone"
    server.add_record(
        DEVICE_B,
        "Diaper",
        "D0000000-0000-4000-8000-0000000000B1",
        when=at(3),
        baby=BABY_THREE,
        status=2,
    )
    return server


@pytest.fixture(autouse=True)
def mock_baby_tracker_client(
    server: FakeServer,
    second_server: FakeServer,  # Core PR 5
) -> Iterator[FakeServer]:
    """Replace the library's client everywhere the integration builds one.

    It is autouse so that no test can reach the real service by accident.
    """

    def factory(email: str, *args: Any, **kwargs: Any) -> FakeClient:
        # The second account signs in to its own service, every other one to the first.
        return FakeClient(  # Core PR 5
            second_server if email == EMAIL_TWO else server, email, *args, **kwargs
        )
        # Core: return FakeClient(server, email, *args, **kwargs)

    with (
        patch("custom_components.baby_tracker.BabyTrackerClient", factory),
        patch("custom_components.baby_tracker.config_flow.BabyTrackerClient", factory),
    ):
        yield server


@pytest.fixture
def mock_config_entry() -> MockConfigEntry:
    """The default entry: one account, with its unique ID set."""
    return MockConfigEntry(
        domain=DOMAIN,
        title=EMAIL,
        unique_id=EMAIL,
        entry_id=ENTRY_ID,  # fixed, because the account sensor's unique ID is built from it
        data={
            CONF_EMAIL: EMAIL,
            CONF_PASSWORD: PASSWORD,
            CONF_DEVICE_UUID: OWN_DEVICE,
        },
    )


@pytest.fixture
async def init_integration(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> MockConfigEntry:
    """Set the default entry up. Tests check the resulting state."""
    await setup_integration(hass, mock_config_entry)
    return mock_config_entry


# Core PR 5
@pytest.fixture
async def second_entry(hass: HomeAssistant, init_integration: MockConfigEntry) -> MockConfigEntry:
    """A second account, set up next to the default one."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=EMAIL_TWO,
        unique_id=EMAIL_TWO,
        data={CONF_EMAIL: EMAIL_TWO, CONF_PASSWORD: PASSWORD, CONF_DEVICE_UUID: OWN_DEVICE_TWO},
    )
    await setup_integration(hass, entry)
    assert entry.state is ConfigEntryState.LOADED
    return entry


@pytest.fixture
def populated_server(server: FakeServer) -> FakeServer:
    """The default server plus every kind of record a baby's entities read.

    Baby one has a feed of each kind, a sleep, and a pump (which belongs to the account). Baby two
    has a sleep still running. All of it falls on the same local day as ``at(180)``.
    """
    server.add_record(
        DEVICE_A, "Formula", "F0000000-0000-4000-8000-000000000001", when=at(20), amount=volume(4)
    )
    server.add_record(
        DEVICE_A,
        "Nursing",
        "F0000000-0000-4000-8000-000000000002",
        when=at(40),
        leftDuration=7,
        rightDuration=12,
        bothDuration=0,
        finishSide=1,
        note="invented note",
    )
    server.add_record(
        DEVICE_A,
        "Pumped",
        "F0000000-0000-4000-8000-000000000003",
        when=at(60),
        amount=volume(90, english=False),
    )
    server.add_record(
        DEVICE_A,
        "Sleep",
        "F0000000-0000-4000-8000-000000000004",
        when=at(90),
        duration=45,
        note="invented nap",
    )
    server.add_record(
        DEVICE_A,
        "Sleep",
        "F0000000-0000-4000-8000-000000000005",
        when=at(120),
        baby=BABY_TWO,
        duration=0,
    )
    server.add_pump(  # Core PR 5
        DEVICE_A, "F0000000-0000-4000-8000-000000000006", when=at(150), amount=volume(5)
    )
    return server


# Core PR 2
@pytest.fixture
async def clock(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> FrozenDateTimeFactory:
    """Eastern time, frozen at the default "now"."""
    await hass.config.async_set_time_zone("America/New_York")
    freezer.move_to(LOCAL_NOW)
    return freezer


# Core PR 7
@pytest.fixture
def empty(server: FakeServer) -> FakeServer:
    """The fake service with no records: each test adds exactly what it needs."""
    server.logs.clear()
    server.names.clear()
    server.names[DEVICE_A] = "Test Phone"
    server.add_payload(DEVICE_A, BABY_ONE)
    server.add_payload(DEVICE_A, BABY_TWO)
    return server


# Core PR 2
@pytest.fixture
def entry(
    hass: HomeAssistant, clock: FrozenDateTimeFactory, monkeypatch: pytest.MonkeyPatch
) -> MockConfigEntry:
    # A day-long interval, so a sync can never be what moves a count at midnight.
    monkeypatch.setattr(
        "custom_components.baby_tracker.coordinator.SCAN_INTERVAL", timedelta(days=1)
    )
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=EMAIL,
        unique_id=EMAIL,
        data={
            CONF_EMAIL: EMAIL,
            CONF_PASSWORD: PASSWORD,
            CONF_DEVICE_UUID: "EEEEEEEE-0000-4000-8000-0000000000EE",
        },
    )
    entry.add_to_hass(hass)
    return entry
