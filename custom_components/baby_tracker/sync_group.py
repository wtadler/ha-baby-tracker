"""A sync reset in the app: stopping the sync and raising the repair that starts over.

When the service signs in to a different sync group than the one the saved state came from, the
saved records and progress belong to the old group, so nothing may be merged into them. The
coordinator stops syncing and this module raises a fixable repair. Fixing it (``repairs.py``)
deletes the saved state and reloads the entry, which builds a new client and a new engine.
"""

import logging
from typing import TYPE_CHECKING

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir
from pybabytracker import AccountChangedError

from .const import DOMAIN

if TYPE_CHECKING:
    from .coordinator import BabyTrackerCoordinator

_LOGGER = logging.getLogger(__name__)


def sync_group_issue_id(entry_id: str) -> str:
    """Return the repair issue ID for an account whose sync group changed."""
    return f"sync_group_changed_{entry_id}"


def forget_group_change(hass: HomeAssistant, entry_id: str) -> None:
    """Remove the repair issue of an entry, if it has one."""
    ir.async_delete_issue(hass, DOMAIN, sync_group_issue_id(entry_id))


def refuse_if_group_changed(coordinator: BabyTrackerCoordinator) -> None:
    """Refuse a write or a timer change once syncing has stopped for a changed sync group.

    Nothing is sent and nothing is saved, and the entities keep showing as unavailable: the
    old group's data must not look live again.
    """
    if coordinator.group_changed:
        raise HomeAssistantError(translation_domain=DOMAIN, translation_key="account_changed")


def stop_for_group_change(coordinator: BabyTrackerCoordinator, err: Exception) -> bool:
    """Stop the coordinator and raise the repair when ``err`` says the sync group changed.

    Polling stops and the entities go unavailable until the repair reloads the entry. Returns
    whether ``err`` was that error; any other error is left for the caller.
    """
    if not isinstance(err, AccountChangedError):
        return False
    entry = coordinator.config_entry
    if not coordinator.group_changed:
        _LOGGER.warning("Syncing stopped for %s: %s", entry.title, err)
    coordinator.group_changed = True
    coordinator.update_interval = None
    coordinator.last_update_success = False
    coordinator.async_update_listeners()
    ir.async_create_issue(
        coordinator.hass,
        DOMAIN,
        sync_group_issue_id(entry.entry_id),
        is_fixable=True,
        is_persistent=True,
        severity=ir.IssueSeverity.ERROR,
        translation_key="sync_group_changed",
        translation_placeholders={"account": entry.title},
        data={"entry_id": entry.entry_id},
    )
    return True
