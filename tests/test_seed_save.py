"""Core PR 27: the sync that reads the account's seed saves it, so a restart doesn't read it
again."""

import io
import sqlite3
import zipfile
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from pybabytracker import BabyTrackerConnectionError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.baby_tracker.const import DOMAIN

from . import BABY_ONE_ID, tick
from .factories import DEVICE_A, at
from .fake_server import FakeClient, FakeServer

OLD_DIAPER = "D0000000-0000-4000-8000-0000000000AA"


def _seed_zip(object_id: str, when: float) -> bytes:
    """A seed: a zip with a database holding one baby and one diaper, in the app's tables."""
    database = sqlite3.connect(":memory:")
    database.execute(
        'CREATE TABLE "Baby" ("ID" TEXT PRIMARY KEY, "Timestamp" REAL, "Name" TEXT, "DOB" REAL)'
    )
    database.execute(
        'CREATE TABLE "Diaper" ("ID" TEXT PRIMARY KEY, "Timestamp" REAL, "Time" REAL, '
        '"Note" TEXT, "BabyID" TEXT, "Status" INTEGER)'
    )
    database.execute('INSERT INTO "Baby" VALUES (?, ?, ?, ?)', (BABY_ONE_ID, when, "Homer", when))
    database.execute(
        'INSERT INTO "Diaper" VALUES (?, ?, ?, ?, ?, ?)',
        (object_id, when, when, "", BABY_ONE_ID, 0),
    )
    database.commit()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zipped:
        zipped.writestr("EasyLog.db", database.serialize())
    database.close()
    return buffer.getvalue()


@pytest.fixture
def seed_downloads(init_integration: MockConfigEntry, monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Give every fake client a seed, and count its downloads.

    The entry is set up first, with no seed, so its first sync moves a log forward and saves.
    The seed is then read by a later sync that moves no log: the case these tests cover.
    """
    seed = _seed_zip(OLD_DIAPER, at(-600).timestamp())
    downloads: list[int] = []

    async def get_seed(_client: FakeClient) -> bytes:
        downloads.append(1)
        return seed

    monkeypatch.setattr(FakeClient, "get_seed", get_seed, raising=False)
    return downloads


def _saved(hass_storage: dict[str, Any], entry: MockConfigEntry) -> dict[str, Any]:
    data: dict[str, Any] = hass_storage[f"{DOMAIN}.{entry.entry_id}"]["data"]
    return data


async def test_a_sync_that_reads_the_seed_saves_it(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    hass_storage: dict[str, Any],
    seed_downloads: list[int],
) -> None:
    """Test the seed's records are saved though no log moved forward."""
    await tick(hass)
    # The seed is decoded on a worker thread.
    await hass.async_block_till_done(wait_background_tasks=True)

    saved = _saved(hass_storage, init_integration)
    assert seed_downloads == [1]
    assert saved["seeded"] == 1
    assert saved["last_seen"] == {DEVICE_A: 2}
    assert OLD_DIAPER in {entry["payload"]["objectID"] for entry in saved["records"]}


async def test_a_restart_after_the_seed_was_saved_does_not_read_it_again(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    hass_storage: dict[str, Any],
    seed_downloads: list[int],
) -> None:
    """Test a reload syncs from the saved state without downloading the seed."""
    await tick(hass)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert await hass.config_entries.async_reload(init_integration.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    await tick(hass)

    assert seed_downloads == [1]
    assert _saved(hass_storage, init_integration)["seeded"] == 1


async def test_a_sync_that_fails_after_reading_the_seed_still_saves_it(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    server: FakeServer,
    hass_storage: dict[str, Any],
    seed_downloads: list[int],
) -> None:
    """Test the seed is saved when the device list fails after it was read."""
    real_get_seed = FakeClient.get_seed  # type: ignore[attr-defined]

    async def get_seed_then_fail(client: FakeClient) -> bytes:
        seed: bytes = await real_get_seed(client)
        server.request_error = BabyTrackerConnectionError("down")
        return seed

    with patch.object(FakeClient, "get_seed", get_seed_then_fail, create=True):
        await tick(hass)
        await hass.async_block_till_done(wait_background_tasks=True)

    saved = _saved(hass_storage, init_integration)
    assert seed_downloads == [1]
    assert saved["seeded"] == 1
    assert saved["last_seen"] == {DEVICE_A: 2}
    assert OLD_DIAPER in {entry["payload"]["objectID"] for entry in saved["records"]}
