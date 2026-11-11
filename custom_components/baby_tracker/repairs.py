"""Repairs: starting over from a sync group that was reset in the app."""

from contextlib import AbstractAsyncContextManager, nullcontext
from uuid import uuid4

import probatio
from homeassistant.components.repairs import RepairsFlow, RepairsFlowResult
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import CONF_DEVICE_UUID, STORAGE_VERSION
from .coordinator import storage_key
from .writes import forget_gap


class SyncGroupRepairFlow(RepairsFlow):
    """Clear the saved sync state and join the account's new sync group from scratch."""

    def __init__(self, entry_id: str) -> None:
        """Set up the flow for one config entry."""
        self._entry_id = entry_id

    async def async_step_init(self, user_input: dict[str, str] | None = None) -> RepairsFlowResult:
        """Go straight to the confirmation."""
        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, str] | None = None
    ) -> RepairsFlowResult:
        """Ask first. On confirm, delete the saved state and join again as a new device."""
        entry = self.hass.config_entries.async_get_known_entry(self._entry_id)
        if user_input is None:
            return self.async_show_form(
                step_id="confirm",
                data_schema=probatio.Schema({}),
                description_placeholders={"account": entry.title},
            )
        # A write that started before this finishes first: it saves the old state on its way out,
        # and that must not come back after the delete.
        lock: AbstractAsyncContextManager[object] = (
            entry.runtime_data.exclusive()
            if entry.state is ConfigEntryState.LOADED
            else nullcontext()
        )
        async with lock:
            await Store(self.hass, STORAGE_VERSION, storage_key(entry.entry_id)).async_remove()
        # A gap in the old device's own log is moot once the entry joins as a new device.
        forget_gap(self.hass, entry.entry_id)
        # Join as a new device, as the app does whenever it joins: after a reset the service
        # refuses every device from before it, whatever the password. The new ID has the same
        # format as the one the config flow makes.
        self.hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_DEVICE_UUID: str(uuid4()).upper()}
        )
        await self.hass.config_entries.async_reload(entry.entry_id)
        if entry.state is not ConfigEntryState.LOADED:
            # The issue stays, and goes when a later sync works.
            return self.async_abort(reason="reload_failed")
        return self.async_create_entry(data={})


async def async_create_fix_flow(
    hass: HomeAssistant,
    issue_id: str,
    data: dict[str, str | int | float | None] | None,
) -> RepairsFlow:
    """Create the flow for the sync group repair, the only fixable issue."""
    return SyncGroupRepairFlow(str((data or {})["entry_id"]))
