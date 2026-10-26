"""A fake client that can also post, for the write tests.

``FakeClient`` (conftest) only reads. This subclass adds ``post_transaction``, which appends to
the posting device's log the way the service does, and switches to make a post fail, hang for an
interleaving test, let another device write just before it, or report a log shorter than it is.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any

from custom_components.nighp_baby_tracker.pybabytracker import Device

from .conftest import FakeClient, FakeServer


class WriteClient(FakeClient):
    async def get_devices(self) -> list[Device]:
        server: Any = self.server
        server.calls.append("devices")
        await asyncio.sleep(0)  # a real await point, so concurrent callers can interleave
        devices = await super().get_devices()
        listed = {device.device_uuid for device in devices}
        devices += [
            Device(uuid, last, "Home Assistant", "test")
            for uuid, last in server.last_sync_override.items()
            if uuid not in listed
        ]
        return [
            replace(device, last_sync_id=server.last_sync_override[device.device_uuid])
            if device.device_uuid in server.last_sync_override
            else device
            for device in devices
        ]

    async def post_transaction(self, op_code: int, sync_id: int, transaction: str) -> None:
        server: Any = self.server
        server.calls.append("post")
        server.active_posts += 1
        server.max_active_posts = max(server.max_active_posts, server.active_posts)
        try:
            await asyncio.sleep(0)
            while server.before_post:  # another device writes between the sync and this post
                server.before_post.pop(0)()
            if server.post_errors:
                error, lands = server.post_errors.pop(0)
                if lands:
                    self._append(op_code, sync_id, transaction)
                raise error
            self._append(op_code, sync_id, transaction)
        finally:
            server.active_posts -= 1

    def _append(self, op_code: int, sync_id: int, transaction: str) -> None:
        log = self.server.logs.setdefault(self.device_uuid, [])
        log.append({"SyncID": sync_id, "OPCode": op_code, "Transaction": transaction})
        self.server.names.setdefault(self.device_uuid, "Home Assistant")
        self.server.posted.append((sync_id, op_code, transaction))  # type: ignore[attr-defined]


def add_write_state(server: FakeServer) -> FakeServer:
    """Give a ``FakeServer`` the switches ``WriteClient`` reads."""
    server.calls = []  # type: ignore[attr-defined]
    server.posted = []  # type: ignore[attr-defined]
    server.post_errors = []  # type: ignore[attr-defined]  # (error, whether the post landed)
    server.last_sync_override = {}  # type: ignore[attr-defined]
    server.before_post = []  # type: ignore[attr-defined]  # callables run as the next post starts
    server.active_posts = 0  # type: ignore[attr-defined]
    server.max_active_posts = 0  # type: ignore[attr-defined]
    return server
