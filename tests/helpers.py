"""Small helpers shared by the test modules."""

from datetime import UTC, datetime, timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.nighp_baby_tracker.const import DOMAIN
from custom_components.nighp_baby_tracker.pybabytracker import decode_transaction

from .factories import BABY_ONE_ID  # noqa: F401
from .fake_server import FakeServer

NOW = datetime(2026, 10, 26, 15, 0, 0, tzinfo=UTC)
OTHER_DEVICES_RECORD = "D0000000-0000-4000-8000-000000000001"  # baby one's diaper from device A


async def tick(hass: HomeAssistant, minutes: float = 5) -> None:
    """Let the clock reach the next scheduled sync (and a second past it), then settle."""
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=minutes, seconds=1))
    await hass.async_block_till_done()


async def sync_interval(hass: HomeAssistant, server: FakeServer) -> timedelta:
    """How often the integration syncs, found by moving the clock and counting the reads.

    A sync already scheduled at the old interval runs first, so the first minute is let go by.
    """
    await tick(hass, minutes=1)
    calls = server.device_list_calls
    await tick(hass, minutes=1)
    if server.device_list_calls > calls:
        return timedelta(minutes=1)
    await tick(hass, minutes=5)
    if server.device_list_calls > calls:
        return timedelta(minutes=5)
    raise AssertionError("no sync within five minutes")


def get_baby_device(registry: dr.DeviceRegistry, baby_id: str) -> dr.DeviceEntry | None:
    """Return the device registered for a baby, or None."""
    devices = registry.async_get_devices(identifiers={(DOMAIN, baby_id)})
    return devices[0] if devices else None


def button(baby: str, kind: str) -> str:
    return f"button.{baby}_{kind}_diaper"


async def press(hass: HomeAssistant, entity_id: str) -> None:
    await hass.services.async_call("button", "press", {"entity_id": entity_id}, blocking=True)


def device_id(hass: HomeAssistant, baby_id: str) -> str:
    device = get_baby_device(dr.async_get(hass), baby_id)
    assert device is not None
    return device.id


async def log_diaper(hass: HomeAssistant, baby_id: str, kind: str = "wet", **extra: Any) -> None:
    data = {"device_id": device_id(hass, baby_id), "type": kind, **extra}
    await hass.services.async_call(DOMAIN, "log_diaper", data, blocking=True)


async def undo(hass: HomeAssistant, **data: Any) -> None:
    await hass.services.async_call(DOMAIN, "undo", data, blocking=True)


def posted(server: FakeServer) -> list[tuple[int, int, Any]]:
    """Everything the fake service received: (number, opcode, decoded record)."""
    return [(n, op, decode_transaction(t)) for n, op, t in server.posted]  # type: ignore[attr-defined]


def sensor_states(hass: HomeAssistant) -> dict[str, tuple[str, dict[str, Any]]]:
    return {
        state.entity_id: (state.state, dict(state.attributes))
        for state in hass.states.async_all("sensor")
    }


def reauth_flows(hass: HomeAssistant) -> list[str]:
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    return [flow["context"]["source"] for flow in flows]


def pump_entity(hass: HomeAssistant, entry: MockConfigEntry) -> str:
    """The entity ID of an entry's account-level last-pump sensor."""
    entity_id = er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, f"{entry.entry_id}_last_pump"
    )
    assert entity_id is not None, "the account's last-pump sensor doesn't exist"
    return entity_id


def account_device(hass: HomeAssistant, entry: MockConfigEntry) -> dr.DeviceEntry:
    entity = er.async_get(hass).async_get(pump_entity(hass, entry))
    assert entity is not None and entity.device_id is not None
    device = dr.async_get(hass).async_get(entity.device_id)
    assert device is not None
    return device
