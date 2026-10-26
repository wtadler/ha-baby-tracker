"""Wet, dirty, and mixed diaper buttons, one set per baby."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import BabyTrackerConfigEntry, BabyTrackerCoordinator
from .writes import async_log_diaper

# The coordinator's lock serializes writes, so Home Assistant needn't.
PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class DiaperButtonDescription(ButtonEntityDescription):
    """A button that logs one kind of diaper."""

    kind: str


BUTTONS: tuple[DiaperButtonDescription, ...] = (
    DiaperButtonDescription(key="wet_diaper", translation_key="wet_diaper", kind="wet"),
    DiaperButtonDescription(key="dirty_diaper", translation_key="dirty_diaper", kind="dirty"),
    DiaperButtonDescription(key="mixed_diaper", translation_key="mixed_diaper", kind="mixed"),
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
            BabyTrackerDiaperButton(coordinator, description, baby_id)
            for baby_id in new
            for description in BUTTONS
        )

    _add_new_babies()
    entry.async_on_unload(coordinator.async_add_listener(_add_new_babies))


class BabyTrackerDiaperButton(CoordinatorEntity[BabyTrackerCoordinator], ButtonEntity):
    """Logs a diaper of one kind, right now, for one baby."""

    _attr_has_entity_name = True
    entity_description: DiaperButtonDescription

    def __init__(
        self,
        coordinator: BabyTrackerCoordinator,
        description: DiaperButtonDescription,
        baby_id: str,
    ) -> None:
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
        return super().available and self._baby_id in self.coordinator.data

    async def async_press(self) -> None:
        await async_log_diaper(self.coordinator, self._baby_id, self.entity_description.kind)
