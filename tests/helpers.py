"""Small helpers shared by the test modules."""

from __future__ import annotations

from datetime import timedelta

from custom_components.nighp_baby_tracker.const import DOMAIN
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed


async def tick(hass: HomeAssistant, minutes: float = 5) -> None:
    """Let the clock reach the next scheduled sync (and a second past it), then settle."""
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=minutes, seconds=1))
    await hass.async_block_till_done()


def get_baby_device(registry: dr.DeviceRegistry, baby_id: str) -> dr.DeviceEntry | None:
    """Return the device registered for a baby, or None."""
    devices = registry.async_get_devices(identifiers={(DOMAIN, baby_id)})
    return devices[0] if devices else None
