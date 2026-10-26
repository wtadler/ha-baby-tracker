"""A shared base for the entities that read one baby's current totals."""

from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity import EntityDescription
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import BabyData, BabyTrackerCoordinator


class BabyTrackerBabyEntity(CoordinatorEntity[BabyTrackerCoordinator]):
    """An entity on a baby's device, built from that baby's data."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: BabyTrackerCoordinator,
        description: EntityDescription,
        baby_id: str,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._baby_id = baby_id
        # Same scheme as the last-activity sensors: the baby's ID, then the entity's key.
        self._attr_unique_id = f"{baby_id}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, baby_id)},
            name=coordinator.data[baby_id].name or "Baby",
            manufacturer="Nighp",
            model="Baby Tracker",
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def _baby(self) -> BabyData | None:
        return self.coordinator.data.get(self._baby_id)

    @property
    def available(self) -> bool:
        return super().available and self._baby is not None
