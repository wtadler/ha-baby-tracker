"""The asleep sensor, one per baby."""

from homeassistant.components.binary_sensor import (
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import BabyTrackerConfigEntry
from .entity import BabyTrackerBabyEntity
from .pybabytracker import Sleep

PARALLEL_UPDATES = 0

ASLEEP = BinarySensorEntityDescription(key="asleep", translation_key="asleep")


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BabyTrackerConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add an asleep sensor for every baby, and for babies that appear in later syncs."""
    coordinator = entry.runtime_data
    known: set[str] = set()

    @callback
    def _add_new_babies() -> None:
        # A baby that left (its device is removed with it) is new again if it's added back.
        known.intersection_update(coordinator.data.babies)
        new = [baby_id for baby_id in coordinator.data.babies if baby_id not in known]
        known.update(new)
        async_add_entities(BabyTrackerAsleepSensor(coordinator, ASLEEP, baby_id) for baby_id in new)

    _add_new_babies()
    entry.async_on_unload(coordinator.async_add_listener(_add_new_babies))


class BabyTrackerAsleepSensor(BabyTrackerBabyEntity, BinarySensorEntity):
    """On while the baby's newest sleep is still running.

    One rule covers several running sleeps: only the newest sleep counts, so a timer that was
    never stopped can't keep the baby asleep after a later sleep ended.
    """

    @property
    def is_on(self) -> bool | None:
        """Return whether the baby's newest sleep is running."""
        baby = self._baby
        if baby is None:
            return None
        return isinstance(baby.sleep, Sleep) and baby.sleep.is_running
