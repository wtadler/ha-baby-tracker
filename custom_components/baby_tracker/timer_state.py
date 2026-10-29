"""The nursing and pump timers Home Assistant keeps itself: their stores, changes, and loaders."""

import logging
from collections.abc import Callable, Container
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.storage import Store
from pybabytracker import NursingTimer, PumpTimer

from .const import (
    DOMAIN,
    STORAGE_VERSION,
)
from .timers import load_pump_timer, load_timers

_LOGGER = logging.getLogger(__name__)


def nursing_storage_key(entry_id: str) -> str:
    """Return the storage key for an entry's nursing timers."""
    return f"{DOMAIN}.{entry_id}.nursing"


def pump_storage_key(entry_id: str) -> str:
    """Return the storage key for an entry's pump timer."""
    return f"{DOMAIN}.{entry_id}.pump"


class TimerState:
    """Home Assistant's own nursing timers (by baby ID) and pump timer (the account's).

    Each is saved with ``Store`` so it survives a restart. A change publishes through the
    callback it is given, so the entities show it at once.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        publish: Callable[[], None],
    ) -> None:
        """Set up empty timers and their stores for one entry."""
        self._hass = hass
        self._entry = entry
        self._publish = publish
        self.nursing: dict[str, NursingTimer] = {}
        self._nursing_store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, nursing_storage_key(entry.entry_id)
        )
        self.pump: PumpTimer | None = None
        self._pump_store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, pump_storage_key(entry.entry_id)
        )

    async def async_load(self) -> None:
        """Load the saved nursing and pump timers. A saved copy that can't be read is dropped."""
        try:
            self.nursing = load_timers(await self._nursing_store.async_load())
        except HomeAssistantError as err:
            _LOGGER.warning(
                "The saved nursing timers couldn't be read (%s); starting with none", err
            )
        try:
            self.pump = load_pump_timer(await self._pump_store.async_load())
        except HomeAssistantError as err:
            _LOGGER.warning("The saved pump timer couldn't be read (%s); starting with none", err)

    async def async_set_nursing(self, baby_id: str, timer: NursingTimer | None) -> None:
        """Start, change, or (with ``None``) clear a baby's nursing timer, publish it, and save it.

        Hold the coordinator's ``exclusive()``. The change applies in memory and on the entities
        even if the save fails; the failure then raises ``HomeAssistantError``
        (``nursing_not_saved``), because the timer won't survive a restart.
        """
        if timer is None:
            self.nursing.pop(baby_id, None)
        else:
            self.nursing[baby_id] = timer
        self._publish()
        if not await self._async_save_nursing():
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="nursing_not_saved")

    async def _async_save_nursing(self) -> bool:
        try:
            await self._nursing_store.async_save(
                {baby: saved.to_dict() for baby, saved in self.nursing.items()}
            )
        except Exception:
            _LOGGER.exception("Couldn't save the nursing timers")
            return False
        return True

    def drop_orphaned_nursing(self, known_babies: Container[str]) -> None:
        """Drop the nursing timers of babies no longer in the account.

        A saved timer for a baby no longer in the account can't be stopped. The saved copy is
        dropped in a task the config entry tracks.
        """
        if orphaned := [baby_id for baby_id in self.nursing if baby_id not in known_babies]:
            for baby_id in orphaned:
                _LOGGER.warning("Dropping the nursing timer of baby %s, no longer known", baby_id)
                del self.nursing[baby_id]
            self._entry.async_create_task(self._hass, self._async_save_nursing())

    async def async_set_pump(self, timer: PumpTimer | None) -> None:
        """Start or (with ``None``) clear the pump timer, publish it, and save it.

        Hold the coordinator's ``exclusive()``. The change applies in memory and on the entities
        even if the save fails; the failure then raises ``HomeAssistantError``
        (``pump_not_saved``), because the timer won't survive a restart.
        """
        self.pump = timer
        self._publish()
        try:
            await self._pump_store.async_save(timer.to_dict() if timer is not None else {})
        except Exception as err:
            _LOGGER.exception("Couldn't save the pump timer")
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="pump_not_saved"
            ) from err
