"""Timer switches: a baby's Sleep, Nursing left, and Nursing right, and the account's Pump timer.

A switch reads on while its timer runs, so a dashboard shows the timer without a separate
sensor. Every control calls the same function in ``writes.py`` as the matching action, so it
takes the coordinator's lock and raises the same translated errors.

The Sleep switch follows the baby's newest sleep, from any device. The two nursing switches
follow Home Assistant's own nursing timer only: the app doesn't sync a running nursing timer
(PROTOCOL.md, "Timers"), so one started on a phone shows when it stops.

The Pump timer belongs to the account, not a baby, so it sits on the account's device. It follows
Home Assistant's own pump timer only (the app doesn't sync a running pump). Its description,
``BabyTrackerAccountSwitchDescription``, is the baby one's counterpart: its functions take the
coordinator alone, and the entity is built on ``BabyTrackerAccountEntity``, as the last-pump sensor
is. Turning it on or off is one call to the same function as ``start_pump`` and ``stop_pump``;
the stop has no amount (the ``stop_pump`` action takes one).
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, override

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from pybabytracker.timers import LEFT, RIGHT

from .coordinator import (
    BabyData,
    BabyTrackerConfigEntry,
    BabyTrackerCoordinator,
    BabyTrackerData,
)
from .entity import BabyTrackerAccountEntity, BabyTrackerBabyEntity
from .writes import (
    async_start_nursing,
    async_start_pump,
    async_start_sleep,
    async_stop_pump,
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


type AccountAction = Callable[[BabyTrackerCoordinator], Awaitable[None]]


@dataclass(frozen=True, kw_only=True)
class BabyTrackerAccountSwitchDescription(SwitchEntityDescription):
    """A switch for a timer that belongs to the account."""

    is_on_fn: Callable[[BabyTrackerData], bool]
    turn_on_fn: AccountAction
    turn_off_fn: AccountAction


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


async def _stop_pump(coordinator: BabyTrackerCoordinator) -> None:
    await async_stop_pump(coordinator)


ACCOUNT_SWITCHES: tuple[BabyTrackerAccountSwitchDescription, ...] = (
    # On while Home Assistant's pump timer runs. Turning it off logs the minutes with no amount.
    BabyTrackerAccountSwitchDescription(
        key="pump",
        translation_key="pump",
        is_on_fn=lambda data: data.pump is not None,
        turn_on_fn=async_start_pump,
        turn_off_fn=_stop_pump,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BabyTrackerConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the account's switch and every baby's, and the babies' that appear in later syncs."""
    coordinator = entry.runtime_data
    async_add_entities(
        BabyTrackerAccountSwitch(coordinator, description) for description in ACCOUNT_SWITCHES
    )
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
    @override
    def is_on(self) -> bool | None:
        """Return whether the timer runs."""
        baby = self._baby
        return self.entity_description.is_on_fn(baby) if baby is not None else None

    @override
    async def async_turn_on(self, **kwargs: Any) -> None:
        """Start the timer (a nursing side switches to it from the other side)."""
        await self.entity_description.turn_on_fn(self.coordinator, self._baby_id)

    @override
    async def async_turn_off(self, **kwargs: Any) -> None:
        """Stop the timer: a sleep ends, a nursing session is logged."""
        await self.entity_description.turn_off_fn(self.coordinator, self._baby_id)


class BabyTrackerAccountSwitch(BabyTrackerAccountEntity, SwitchEntity):
    """The account's pump timer, on while it runs."""

    entity_description: BabyTrackerAccountSwitchDescription

    @property
    @override
    def is_on(self) -> bool:
        """Return whether the timer runs."""
        return self.entity_description.is_on_fn(self.coordinator.data)

    @override
    async def async_turn_on(self, **kwargs: Any) -> None:
        """Start the timer."""
        await self.entity_description.turn_on_fn(self.coordinator)

    @override
    async def async_turn_off(self, **kwargs: Any) -> None:
        """Stop the timer and log the session, with no amount."""
        await self.entity_description.turn_off_fn(self.coordinator)
