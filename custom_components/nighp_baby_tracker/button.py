"""Diaper, Bottle, Sleep, and Nurse buttons, one set per baby."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import BabyTrackerConfigEntry, BabyTrackerCoordinator
from .writes import (
    async_log_configured_bottle,
    async_log_diaper,
    async_press_nurse,
    async_press_sleep,
)

# The coordinator's lock serializes writes, so Home Assistant needn't.
PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class BabyTrackerButtonDescription(ButtonEntityDescription):
    """A button that logs one thing for a baby, right now."""

    press_fn: Callable[[BabyTrackerCoordinator, str], Awaitable[None]]


def _diaper(kind: str) -> Callable[[BabyTrackerCoordinator, str], Awaitable[None]]:
    async def press(coordinator: BabyTrackerCoordinator, baby_id: str) -> None:
        await async_log_diaper(coordinator, baby_id, kind)

    return press


def _nurse(side: str) -> Callable[[BabyTrackerCoordinator, str], Awaitable[None]]:
    async def press(coordinator: BabyTrackerCoordinator, baby_id: str) -> None:
        await async_press_nurse(coordinator, baby_id, side)

    return press


BUTTONS: tuple[BabyTrackerButtonDescription, ...] = (
    BabyTrackerButtonDescription(
        key="wet_diaper", translation_key="wet_diaper", press_fn=_diaper("wet")
    ),
    BabyTrackerButtonDescription(
        key="dirty_diaper", translation_key="dirty_diaper", press_fn=_diaper("dirty")
    ),
    BabyTrackerButtonDescription(
        key="mixed_diaper", translation_key="mixed_diaper", press_fn=_diaper("mixed")
    ),
    # Logs the bottle the options describe, read when the button is pressed.
    BabyTrackerButtonDescription(
        key="bottle", translation_key="bottle", press_fn=async_log_configured_bottle
    ),
    # Starts a sleep, or ends the running one (from any device).
    BabyTrackerButtonDescription(key="sleep", translation_key="sleep", press_fn=async_press_sleep),
    # Start a side, switch to it, or stop when it's the side already running.
    BabyTrackerButtonDescription(
        key="nurse_left", translation_key="nurse_left", press_fn=_nurse("left")
    ),
    BabyTrackerButtonDescription(
        key="nurse_right", translation_key="nurse_right", press_fn=_nurse("right")
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BabyTrackerConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the buttons for every baby, and for babies that appear in later syncs."""
    coordinator = entry.runtime_data
    known: set[str] = set()

    @callback
    def _add_new_babies() -> None:
        new = [baby_id for baby_id in coordinator.data if baby_id not in known]
        known.update(new)
        async_add_entities(
            BabyTrackerButton(coordinator, description, baby_id)
            for baby_id in new
            for description in BUTTONS
        )

    _add_new_babies()
    entry.async_on_unload(coordinator.async_add_listener(_add_new_babies))


class BabyTrackerButton(CoordinatorEntity[BabyTrackerCoordinator], ButtonEntity):
    """Logs one thing, or moves a timer along, right now, for one baby."""

    _attr_has_entity_name = True
    entity_description: BabyTrackerButtonDescription

    def __init__(
        self,
        coordinator: BabyTrackerCoordinator,
        description: BabyTrackerButtonDescription,
        baby_id: str,
    ) -> None:
        """Set up a button that acts on one baby."""
        super().__init__(coordinator)
        self.entity_description = description
        self._baby_id = baby_id
        self._attr_unique_id = f"{baby_id}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, baby_id)},
            name=coordinator.data[baby_id].name or "Baby",
            manufacturer="Nighp",
            model="Baby Tracker",
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def available(self) -> bool:
        """Return whether the baby is still on the account."""
        return super().available and self._baby_id in self.coordinator.data

    async def async_press(self) -> None:
        """Run the button's action for its baby."""
        await self.entity_description.press_fn(self.coordinator, self._baby_id)
