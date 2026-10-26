"""Invented sync-format payloads for tests. Nothing here comes from a real account."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
from typing import Any

BABY_ONE_ID = "11111111-AAAA-4AAA-8AAA-000000000001"
BABY_TWO_ID = "22222222-BBBB-4BBB-8BBB-000000000002"
BABY_THREE_ID = "33333333-CCCC-4CCC-8CCC-000000000003"

DEVICE_A = "AAAAAAAA-0000-4000-8000-00000000000A"
DEVICE_B = "BBBBBBBB-0000-4000-8000-00000000000B"

BASE_TIME = datetime(2026, 10, 25, 12, 0, 0, tzinfo=UTC)

OP_ADD = 0
OP_UPDATE = 1
OP_DELETE = 2


def at(minutes: float = 0) -> datetime:
    """A moment, in minutes after the fixed base time."""
    return BASE_TIME + timedelta(minutes=minutes)


def fmt(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%d %H:%M:%S %z")


def volume(value: float, *, english: bool = True) -> dict[str, Any]:
    """A sync-format volume, as the app writes it."""
    return {
        "BCObjectType": "VolumeMeasure",
        "englishMeasure": "true" if english else "false",
        "value": value,
    }


def baby_payload(name: str, object_id: str) -> dict[str, Any]:
    return {
        "BCObjectType": "Baby",
        "objectID": object_id,
        "name": name,
        "dob": "2026-09-01 00:00:00 +0000",
        "gender": "true",
        "pictureName": "",
        "newFlage": "false",
        "timestamp": "1970-01-01 00:00:00 +0000",
    }


def record_payload(
    type_name: str,
    object_id: str,
    *,
    when: datetime,
    baby: dict[str, Any],
    changed: datetime | None = None,
    note: str = "",
    **fields: Any,
) -> dict[str, Any]:
    return {
        "BCObjectType": type_name,
        "objectID": object_id,
        "time": fmt(when),
        "timestamp": fmt(changed or when),
        "timezone": -18000,
        "note": note,
        "pictureNote": [],
        "newFlage": "true",
        "pictureLoaded": "true",
        **fields,
        "baby": baby,
    }


def wire(sync_id: int, op_code: int, payload: Any) -> dict[str, Any]:
    """One entry as the service returns it from a device's log."""
    text = json.dumps(payload).encode("utf-8")
    return {"SyncID": sync_id, "OPCode": op_code, "Transaction": base64.b64encode(text).decode()}
