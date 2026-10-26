"""Fixtures for Baby Tracker (Nighp) tests.

The library's client is faked at its public interface (``sign_in``, ``get_devices``,
``get_transactions``, ``close``). The real sync engine runs on top of the fake, so the tests
exercise the real merging of logs.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from typing import Any
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.nighp_baby_tracker.const import (
    CONF_DEVICE_UUID,
    CONF_EMAIL,
    CONF_PASSWORD,
    DOMAIN,
)
from custom_components.nighp_baby_tracker.pybabytracker import (
    AuthenticationError,
    Device,
    RawTransaction,
)

from .factories import (
    BABY_ONE_ID,
    BABY_TWO_ID,
    DEVICE_A,
    OP_ADD,
    at,
    baby_payload,
    record_payload,
    wire,
)

EMAIL = "parent@example.com"
PASSWORD = "test-password"
NEW_PASSWORD = "test-new-password"
PAGE_SIZE = 500

BABY_ONE = baby_payload("Baby One", BABY_ONE_ID)
BABY_TWO = baby_payload("Baby Two", BABY_TWO_ID)


class FakeServer:
    """The state behind every fake client: devices, their logs, and failure switches."""

    def __init__(self) -> None:
        self.password = PASSWORD
        self.logs: dict[str, list[dict[str, Any]]] = {}
        self.names: dict[str, str] = {}
        self.sign_in_error: Exception | None = None
        self.request_error: Exception | None = None
        self.clients: list[FakeClient] = []
        self.sign_ins: list[dict[str, str]] = []
        self.device_list_calls = 0
        self.transaction_reads: list[tuple[str, int]] = []

    def add_record(
        self,
        device: str,
        type_name: str,
        object_id: str,
        *,
        when: datetime,
        baby: dict[str, Any] = BABY_ONE,
        op: int = OP_ADD,
        changed: datetime | None = None,
        note: str = "",
        **fields: Any,
    ) -> int:
        payload = record_payload(
            type_name, object_id, when=when, baby=baby, changed=changed, note=note, **fields
        )
        return self.add_payload(device, payload, op=op)

    def add_payload(self, device: str, payload: Any, *, op: int = OP_ADD) -> int:
        log = self.logs.setdefault(device, [])
        sync_id = len(log) + 1
        log.append(wire(sync_id, op, payload))
        self.names.setdefault(device, f"Device {device[:1]}")
        return sync_id

    def add_raw(self, device: str, entry: dict[str, Any]) -> None:
        self.logs.setdefault(device, []).append(entry)
        self.names.setdefault(device, f"Device {device[:1]}")

    @property
    def open_clients(self) -> list[FakeClient]:
        return [client for client in self.clients if not client.closed]

    def reads_from_start(self) -> list[tuple[str, int]]:
        return [read for read in self.transaction_reads if read[1] == 0]


class FakeClient:
    """Stands in for ``BabyTrackerClient``."""

    def __init__(
        self,
        server: FakeServer,
        email: str,
        password: str,
        device_uuid: str,
        device_name: str,
        **kwargs: Any,
    ) -> None:
        self.server = server
        self.email = email
        self.password = password
        self.device_uuid = device_uuid
        self.device_name = device_name
        self.kwargs = kwargs
        self.closed = False
        self._signed_in = False
        server.clients.append(self)

    async def sign_in(self) -> int:
        self.server.sign_ins.append(
            {
                "email": self.email,
                "password": self.password,
                "device_uuid": self.device_uuid,
                "device_name": self.device_name,
            }
        )
        if self.server.sign_in_error is not None:
            raise self.server.sign_in_error
        if self.password != self.server.password:
            raise AuthenticationError("the service rejected the email or password")
        self._signed_in = True
        return 1

    async def _ensure_session(self) -> None:
        if self.server.request_error is not None:
            raise self.server.request_error
        if not self._signed_in or self.password != self.server.password:
            await self.sign_in()

    async def get_devices(self) -> list[Device]:
        await self._ensure_session()
        self.server.device_list_calls += 1
        return [
            Device(
                device_uuid=uuid,
                last_sync_id=len(log),
                device_name=self.server.names[uuid],
                device_os_info="test",
            )
            for uuid, log in self.server.logs.items()
        ]

    async def get_transactions(self, device_uuid: str, since_sync_id: int = 0) -> list[RawTransaction]:
        await self._ensure_session()
        self.server.transaction_reads.append((device_uuid, since_sync_id))
        entries = [e for e in self.server.logs.get(device_uuid, []) if e["SyncID"] > since_sync_id]
        return [
            RawTransaction(e["SyncID"], e["OPCode"], e["Transaction"]) for e in entries[:PAGE_SIZE]
        ]

    async def close(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> Iterator[None]:
    """Load custom_components/ in every test."""
    yield


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


@pytest.fixture(autouse=True)
def fake_client(server: FakeServer) -> Iterator[FakeServer]:
    """Replace the library's client everywhere the integration builds one."""

    def factory(*args: Any, **kwargs: Any) -> FakeClient:
        return FakeClient(server, *args, **kwargs)

    with (
        patch("custom_components.nighp_baby_tracker.BabyTrackerClient", factory),
        patch("custom_components.nighp_baby_tracker.config_flow.BabyTrackerClient", factory),
    ):
        yield server


@pytest.fixture
def config_entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title=EMAIL,
        unique_id=EMAIL,
        data={
            CONF_EMAIL: EMAIL,
            CONF_PASSWORD: PASSWORD,
            CONF_DEVICE_UUID: "EEEEEEEE-0000-4000-8000-0000000000EE",
        },
    )


@pytest.fixture
async def setup_entry(hass, config_entry):
    """Add the entry to hass and try to set it up. Tests check the resulting state."""
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    return config_entry
