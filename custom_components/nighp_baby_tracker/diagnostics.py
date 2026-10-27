"""Diagnostics: enough to explain a problem, with nothing that identifies the account or a baby."""

from collections import Counter
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant

from .const import CONF_DEVICE_UUID
from .coordinator import BabyTrackerConfigEntry

TO_REDACT = {CONF_EMAIL, CONF_PASSWORD, CONF_DEVICE_UUID}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: BabyTrackerConfigEntry
) -> dict[str, Any]:
    """Return the entry's settings and the sync state, without names, IDs, or notes.

    Babies are listed by position only, and the other devices in the account by how far their
    logs have been read.
    """
    coordinator = entry.runtime_data
    engine = coordinator.engine
    data = coordinator.data
    return {
        "entry": {
            "version": entry.version,
            "minor_version": entry.minor_version,
            "data": async_redact_data(entry.data, TO_REDACT),
            "options": dict(entry.options),
        },
        "coordinator": {
            "last_update_success": coordinator.last_update_success,
            "update_interval_seconds": (
                coordinator.update_interval.total_seconds()
                if coordinator.update_interval is not None
                else None
            ),
            "babies": len(data.babies),
            "has_last_pump": data.last_pump is not None,
            "pump_timer_running": data.pump is not None,
        },
        "engine": {
            "devices_seen": len(engine.last_seen),
            "last_sync_ids": sorted(engine.last_seen.values()),
            "records_by_type": dict(
                sorted(Counter(record.record_type for record in engine.records.values()).items())
            ),
        },
        "babies": [
            {
                "has_diaper": baby.diaper is not None,
                "has_feed": baby.feed is not None,
                "has_sleep": baby.sleep is not None,
                "sleep_running": baby.running_sleep is not None,
                "nursing_timer_running": baby.nursing is not None,
            }
            for baby in data.babies.values()
        ],
    }
