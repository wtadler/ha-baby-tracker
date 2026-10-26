"""Small helpers shared by the test modules."""

from __future__ import annotations

from datetime import timedelta

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed


async def tick(hass: HomeAssistant, minutes: float = 5) -> None:
    """Let the clock reach the next scheduled sync (and a second past it), then settle."""
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=minutes, seconds=1))
    await hass.async_block_till_done()
