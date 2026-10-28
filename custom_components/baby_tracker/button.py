"""Diaper, Bottle, and Discard nursing buttons, one set per baby."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import override

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import BabyTrackerConfigEntry, BabyTrackerCoordinator
from .entity import BabyTrackerBabyEntity
from .writes import async_discard_nursing, async_log_configured_bottle, async_log_diaper

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
    # For a nursing timer left running by mistake: drops it without writing anything.
    BabyTrackerButtonDescription(
        key="discard_nursing",
        translation_key="discard_nursing",
        press_fn=async_discard_nursing,
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
        # A baby that left (its device is removed with it) is new again if it's added back.
        known.intersection_update(coordinator.data.babies)
        new = [baby_id for baby_id in coordinator.data.babies if baby_id not in known]
        known.update(new)
        async_add_entities(
            BabyTrackerButton(coordinator, description, baby_id)
            for baby_id in new
            for description in BUTTONS
        )

    _add_new_babies()
    entry.async_on_unload(coordinator.async_add_listener(_add_new_babies))


class BabyTrackerButton(BabyTrackerBabyEntity, ButtonEntity):
    """Logs one thing, or drops the nursing timer, right now, for one baby."""

    entity_description: BabyTrackerButtonDescription

    @override
    async def async_press(self) -> None:
        """Run the button's action for its baby."""
        await self.entity_description.press_fn(self.coordinator, self._baby_id)
