"""Each baby's photo and newest attached picture, as image entities."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, override

from homeassistant.components.image import ImageEntity, ImageEntityDescription
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from .coordinator import BabyData, BabyTrackerConfigEntry, BabyTrackerCoordinator
from .entity import BabyTrackerBabyEntity

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class BabyTrackerImageDescription(ImageEntityDescription):
    """An image of one baby: which picture file it shows, and what to say about it."""

    file_fn: Callable[[BabyData], str | None]
    attributes_fn: Callable[[BabyData], dict[str, Any]] | None = None


def _latest_attributes(baby: BabyData) -> dict[str, Any]:
    picture = baby.latest_picture
    return {
        "record_type": picture.record_type if picture else None,
        "record_id": picture.record_id if picture else None,
        "time": picture.time.isoformat() if picture and picture.time else None,
    }


IMAGES: tuple[BabyTrackerImageDescription, ...] = (
    # The baby's photo in the app: its cover photo.
    BabyTrackerImageDescription(
        key="photo",
        translation_key="photo",
        file_fn=lambda baby: baby.photo_file,
    ),
    # The newest picture attached to any of the baby's entries (a milestone, a diaper, ...).
    BabyTrackerImageDescription(
        key="latest_picture",
        translation_key="latest_picture",
        # It can show any entry's picture, a diaper's included, so it is opt-in.
        entity_registry_enabled_default=False,
        file_fn=lambda baby: baby.latest_picture.file if baby.latest_picture else None,
        attributes_fn=_latest_attributes,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BabyTrackerConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the images of every baby, and of babies that appear in later syncs."""
    coordinator = entry.runtime_data
    known: set[str] = set()

    @callback
    def _add_new_babies() -> None:
        # A baby that left (its device is removed with it) is new again if it's added back.
        known.intersection_update(coordinator.data.babies)
        new = [baby_id for baby_id in coordinator.data.babies if baby_id not in known]
        known.update(new)
        async_add_entities(
            BabyTrackerImage(coordinator, description, baby_id)
            for baby_id in new
            for description in IMAGES
        )

    _add_new_babies()
    entry.async_on_unload(coordinator.async_add_listener(_add_new_babies))


class BabyTrackerImage(BabyTrackerBabyEntity, ImageEntity):
    """A picture of a baby from the service, downloaded when the frontend asks for it.

    The picture's time moves when its file name does (and at start-up, so it moves after every
    restart or reload), so the frontend asks for it again only then. Nothing is downloaded at sync
    or setup: ``async_image`` asks the coordinator's picture cache, which downloads a file once.

    The latest picture shows the newest picture on any of the baby's entries, such as a diaper's,
    so it is disabled by default.
    """

    entity_description: BabyTrackerImageDescription
    _attr_content_type = "image/jpeg"

    def __init__(
        self,
        coordinator: BabyTrackerCoordinator,
        description: BabyTrackerImageDescription,
        baby_id: str,
    ) -> None:
        """Set up an image for one baby."""
        BabyTrackerBabyEntity.__init__(self, coordinator, description, baby_id)
        ImageEntity.__init__(self, coordinator.hass)
        self._shown: str | None = None
        self._note_change()

    def _file(self) -> str | None:
        baby = self._baby
        return self.entity_description.file_fn(baby) if baby is not None else None

    def _note_change(self) -> None:
        """Move the image's time when the file it shows changes, and clear it with no file."""
        file = self._file()
        if file != self._shown:
            self._shown = file
            self._attr_image_last_updated = dt_util.utcnow() if file else None

    @callback
    @override
    def _handle_coordinator_update(self) -> None:
        self._note_change()
        super()._handle_coordinator_update()

    @override
    async def async_image(self) -> bytes | None:
        """Return the picture, downloading it if it's the first request, or None when none."""
        return await self.coordinator.pictures.async_get(self._file())

    @property
    @override
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return what the newest picture is attached to (the photo has nothing to add)."""
        baby = self._baby
        attributes_fn = self.entity_description.attributes_fn
        return attributes_fn(baby) if baby is not None and attributes_fn else None
