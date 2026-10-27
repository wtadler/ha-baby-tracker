"""Timer switches: Sleep, Nursing left, and Nursing right, one set per baby.

A switch reads on while its timer runs, so a dashboard shows the timer without a separate
sensor. Every control calls the same function in ``writes.py`` as the matching action, so it
takes the coordinator's lock and raises the same translated errors.

The Sleep switch follows the baby's newest sleep, from any device. The two nursing switches
follow Home Assistant's own nursing timer only: the app doesn't sync a running nursing timer
(PROTOCOL.md, "Timers"), so one started on a phone shows when it stops.

To add a switch for the account (a pump timer), give it a description like
``BabyTrackerSwitchDescription`` whose functions take the coordinator alone, and build it on
``BabyTrackerAccountEntity``, as the last-pump sensor is.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import BabyData, BabyTrackerConfigEntry, BabyTrackerCoordinator
from .entity import BabyTrackerBabyEntity
from .timers import LEFT, RIGHT
from .writes import (
    async_start_nursing,
    async_start_sleep,
    async_stop_sleep,
    async_turn_off_nursing_side,
)

# The coordinator's lock serializes writes, so Home Assistant needn't.
PARALLEL_UPDATES = 0

type BabyAction = Callable[[BabyTrackerCoordinator, str], Awaitable[None]]


@dataclass(frozen=True, kw_only=True)
class BabyTrackerSwitchDescription(SwitchEntityDescription):
    """A switch for one of a baby's timers."""

    is_on_fn: Callable[[BabyData], bool]
    turn_on_fn: BabyAction
    turn_off_fn: BabyAction


def _nursing_side(side: str) -> tuple[Callable[[BabyData], bool], BabyAction, BabyAction]:
    """Return what a nursing side's switch reads, and does when turned on and off."""

    def is_on(baby: BabyData) -> bool:
        return baby.nursing is not None and baby.nursing.side == side

    async def turn_on(coordinator: BabyTrackerCoordinator, baby_id: str) -> None:
        await async_start_nursing(coordinator, baby_id, side)

    async def turn_off(coordinator: BabyTrackerCoordinator, baby_id: str) -> None:
        await async_turn_off_nursing_side(coordinator, baby_id, side)

    return is_on, turn_on, turn_off


def _nursing_description(side: str) -> BabyTrackerSwitchDescription:
    is_on, turn_on, turn_off = _nursing_side(side)
    return BabyTrackerSwitchDescription(
        key=f"nursing_{side}",
        translation_key=f"nursing_{side}",
        is_on_fn=is_on,
        turn_on_fn=turn_on,
        turn_off_fn=turn_off,
    )


async def _start_sleep(coordinator: BabyTrackerCoordinator, baby_id: str) -> None:
    await async_start_sleep(coordinator, baby_id)


async def _stop_sleep(coordinator: BabyTrackerCoordinator, baby_id: str) -> None:
    await async_stop_sleep(coordinator, baby_id)


SWITCHES: tuple[BabyTrackerSwitchDescription, ...] = (
    # On while the newest sleep runs, whichever device started it.
    BabyTrackerSwitchDescription(
        key="sleep",
        translation_key="sleep",
        is_on_fn=lambda baby: baby.running_sleep is not None,
        turn_on_fn=_start_sleep,
        turn_off_fn=_stop_sleep,
    ),
    # On while Home Assistant's nursing timer runs on that side: at most one of the two.
    _nursing_description(LEFT),
    _nursing_description(RIGHT),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BabyTrackerConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the switches for every baby, and for babies that appear in later syncs."""
    coordinator = entry.runtime_data
    known: set[str] = set()

    @callback
    def _add_new_babies() -> None:
        # A baby that left (its device is removed with it) is new again if it's added back.
        known.intersection_update(coordinator.data.babies)
        new = [baby_id for baby_id in coordinator.data.babies if baby_id not in known]
        known.update(new)
        async_add_entities(
            BabyTrackerSwitch(coordinator, description, baby_id)
            for baby_id in new
            for description in SWITCHES
        )

    _add_new_babies()
    entry.async_on_unload(coordinator.async_add_listener(_add_new_babies))


class BabyTrackerSwitch(BabyTrackerBabyEntity, SwitchEntity):
    """A baby's sleep or nursing timer, on while it runs."""

    entity_description: BabyTrackerSwitchDescription

    @property
    def is_on(self) -> bool | None:
        """Return whether the timer runs."""
        baby = self._baby
        return self.entity_description.is_on_fn(baby) if baby is not None else None

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Start the timer (a nursing side switches to it from the other side)."""
        await self.entity_description.turn_on_fn(self.coordinator, self._baby_id)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Stop the timer: a sleep ends, a nursing session is logged."""
        await self.entity_description.turn_off_fn(self.coordinator, self._baby_id)
