"""Sync coordinator: keeps the sync engine current and exposes each baby's latest records."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import timedelta

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL_MINUTES,
    DOMAIN,
    STORAGE_VERSION,
)
from .pybabytracker import (
    AuthenticationError,
    BabyTrackerClient,
    BabyTrackerError,
    Record,
    SyncEngine,
)

_LOGGER = logging.getLogger(__name__)

type BabyTrackerConfigEntry = ConfigEntry[BabyTrackerCoordinator]

FEED_TYPES = ("Nursing", "Formula", "Pumped")


@dataclass(frozen=True, slots=True)
class BabyData:
    """One baby's newest record of each kind the sensors show."""

    baby_id: str
    name: str | None
    diaper: Record | None = None
    feed: Record | None = None
    pump: Record | None = None
    sleep: Record | None = None


def storage_key(entry_id: str) -> str:
    return f"{DOMAIN}.{entry_id}"


def scan_interval(entry: ConfigEntry) -> timedelta:
    return timedelta(minutes=entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL_MINUTES))


def _newest(records: list[Record]) -> Record | None:
    def key(record: Record) -> tuple[float, float]:
        assert record.time is not None  # latest_records only returns records with a time
        stamp = record.timestamp.timestamp() if record.timestamp is not None else 0.0
        return (record.time.timestamp(), stamp)

    return max(records, key=key, default=None)


class BabyTrackerCoordinator(DataUpdateCoordinator[dict[str, BabyData]]):
    """Syncs with the service, saves the engine's state, and rebuilds each baby's latest records."""

    config_entry: BabyTrackerConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: BabyTrackerConfigEntry,
        client: BabyTrackerClient,
        session: aiohttp.ClientSession,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=scan_interval(entry),
        )
        self.client = client
        self.session = session
        self.engine = SyncEngine()
        # Everything that uses the engine takes this lock, so a later write path can share it.
        self.lock = asyncio.Lock()
        self._store: Store = Store(hass, STORAGE_VERSION, storage_key(entry.entry_id))
        self._baby_names: dict[str, str | None] = {}

    async def _async_setup(self) -> None:
        """Load the saved sync state, so a restart fetches only what's newer."""
        try:
            saved = await self._store.async_load()
        except HomeAssistantError as err:
            _LOGGER.warning("The saved sync state couldn't be read (%s); loading all history", err)
            return
        if saved is None:
            return
        try:
            self.engine = SyncEngine.from_dict(saved)
        except (KeyError, TypeError, ValueError, AttributeError, BabyTrackerError) as err:
            _LOGGER.warning(
                "The saved sync state couldn't be read (%r); loading all history again", err
            )

    async def _async_update_data(self) -> dict[str, BabyData]:
        async with self.lock:
            before = dict(self.engine.last_seen)
            try:
                await self.engine.sync(self.client)
            except AuthenticationError as err:
                raise ConfigEntryAuthFailed("The service rejected the email or password") from err
            except BabyTrackerError as err:
                raise UpdateFailed(f"Couldn't sync with the service: {err}") from err
            finally:
                # Keep progress even when a later device failed.
                if dict(self.engine.last_seen) != before:
                    await self._store.async_save(self.engine.to_dict())
            return self._snapshot()

    def _snapshot(self) -> dict[str, BabyData]:
        for baby_id, baby in self.engine.babies().items():
            self._baby_names[baby_id] = baby.name
        data: dict[str, BabyData] = {}
        for baby_id, name in self._baby_names.items():
            latest = self.engine.latest_records(baby_id)
            data[baby_id] = BabyData(
                baby_id=baby_id,
                name=name,
                diaper=latest.get("Diaper"),
                feed=_newest([latest[kind] for kind in FEED_TYPES if kind in latest]),
                pump=latest.get("Pump"),
                sleep=latest.get("Sleep"),
            )
        return data

    async def async_close(self) -> None:
        """Stop syncing and free the client.

        Home Assistant detaches the session when the config entry unloads (it was created during
        setup), and warns if an integration closes one itself, so this leaves the session alone.
        """
        await self.async_shutdown()
        await self.client.close()
