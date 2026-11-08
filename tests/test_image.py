"""Core PR 23: each baby's photo and newest attached picture, as image entities."""

from datetime import timedelta
from typing import Any

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.image import async_get_image
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util
from pybabytracker import BabyTrackerConnectionError
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from . import BABY_ONE, NOW, setup_integration, tick
from .factories import DEVICE_A, at
from .fake_server import FakeServer

PHOTO = "image.baby_one_photo"
LATEST = "image.baby_one_latest_picture"
JPEG = b"\xff\xd8\xffinvented photo"
OTHER_JPEG = b"\xff\xd8\xffinvented other photo"


@pytest.fixture(autouse=True)
def _start_at_now(freezer: FrozenDateTimeFactory) -> None:
    """Freeze the clock at NOW before anything is set up."""
    freezer.move_to(NOW)


def _with_photo(server: FakeServer, picture_name: str) -> None:
    """Baby one's own record, now with a photo."""
    server.add_payload(DEVICE_A, {**BABY_ONE, "pictureName": picture_name})


def _picture(file_name: str, media_type: Any = "0", **overrides: Any) -> dict[str, Any]:
    """A pictureNote entry shaped like one an Android phone synced, with invented IDs."""
    return {
        "activityID": "ACTIVITY-1",
        "attached": True,
        "deleted": False,
        "fileName": file_name,
        "mediaType": media_type,
        "newFlage": True,
        "objectID": f"ID-{file_name}",
        "timestamp": "2026-10-26 14:00:00 +0000",
        **overrides,
    }


def _milestone(server: FakeServer, number: int, minute: float, *pictures: dict[str, Any]) -> None:
    server.add_record(
        DEVICE_A,
        "Milestone",
        f"M0000000-0000-4000-8000-{number:012d}",
        when=at(minute),
        pictureNote=list(pictures),
    )


async def _image(hass: HomeAssistant, entity_id: str) -> bytes:
    return (await async_get_image(hass, entity_id)).content


def _state(hass: HomeAssistant, entity_id: str) -> str:
    state = hass.states.get(entity_id)
    assert state is not None, f"{entity_id} does not exist"
    return state.state


async def test_a_babys_photo_is_downloaded_at_sync_and_shown(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, server: FakeServer
) -> None:
    """Test the cover photo is the baby's pictureName plus .jpg, downloaded once."""
    _with_photo(server, "PHOTO-1")
    server.pictures["PHOTO-1.jpg"] = JPEG
    await setup_integration(hass, mock_config_entry)

    assert _state(hass, PHOTO) == NOW.isoformat()
    assert await _image(hass, PHOTO) == JPEG
    await tick(hass)
    assert server.picture_requests == ["PHOTO-1.jpg"]


async def test_a_baby_without_a_photo_has_an_unknown_image_and_nothing_is_asked_for(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    for entity_id in (PHOTO, LATEST, "image.baby_two_photo"):
        assert _state(hass, entity_id) == "unknown"
    assert server.picture_requests == []


async def test_a_new_photo_replaces_the_old_one(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    server: FakeServer,
    freezer: FrozenDateTimeFactory,
) -> None:
    _with_photo(server, "PHOTO-1")
    server.pictures |= {"PHOTO-1.jpg": JPEG, "PHOTO-2.jpg": OTHER_JPEG}
    await setup_integration(hass, mock_config_entry)

    freezer.tick(timedelta(minutes=5))
    _with_photo(server, "PHOTO-2")
    await tick(hass)

    assert _state(hass, PHOTO) == dt_util.utcnow().isoformat()
    assert await _image(hass, PHOTO) == OTHER_JPEG
    assert set(mock_config_entry.runtime_data.pictures.files) == {"PHOTO-2.jpg"}


async def test_the_latest_picture_is_the_newest_one_on_any_record(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, server: FakeServer
) -> None:
    _milestone(server, 1, 10, _picture("PICTURE-1"))
    _milestone(server, 2, 30, _picture("PICTURE-2"))
    server.pictures |= {"PICTURE-1.jpg": JPEG, "PICTURE-2.jpg": OTHER_JPEG}
    await setup_integration(hass, mock_config_entry)

    assert await _image(hass, LATEST) == OTHER_JPEG
    state = hass.states.get(LATEST)
    assert state is not None
    assert state.attributes["record_type"] == "Milestone"
    assert state.attributes["record_id"] == "M0000000-0000-4000-8000-000000000002"
    assert state.attributes["time"] == at(30).isoformat()
    assert server.picture_requests == ["PICTURE-2.jpg"]


async def test_a_video_shows_its_thumbnail(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, server: FakeServer
) -> None:
    _milestone(server, 1, 10, _picture("VIDEO-1", media_type=1))
    server.pictures["VIDEO-1__thumbnail.jpg"] = JPEG
    await setup_integration(hass, mock_config_entry)

    assert await _image(hass, LATEST) == JPEG


async def test_a_deleted_picture_is_not_shown(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, server: FakeServer
) -> None:
    _milestone(server, 1, 10, _picture("PICTURE-1"))
    _milestone(server, 2, 30, _picture("PICTURE-2", deleted=True))
    server.pictures |= {"PICTURE-1.jpg": JPEG, "PICTURE-2.jpg": OTHER_JPEG}
    await setup_integration(hass, mock_config_entry)

    assert await _image(hass, LATEST) == JPEG


async def test_a_picture_the_service_does_not_have_is_asked_for_again_hours_later(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    server: FakeServer,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Test a missing file leaves the image unknown, and isn't asked for at every sync."""
    _with_photo(server, "PHOTO-1")
    await setup_integration(hass, mock_config_entry)
    assert _state(hass, PHOTO) == "unknown"
    with pytest.raises(HomeAssistantError):
        await async_get_image(hass, PHOTO)

    await tick(hass)
    assert server.picture_requests == ["PHOTO-1.jpg"]

    server.pictures["PHOTO-1.jpg"] = JPEG
    freezer.tick(timedelta(hours=6, minutes=1))
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()
    assert server.picture_requests == ["PHOTO-1.jpg", "PHOTO-1.jpg"]
    assert await _image(hass, PHOTO) == JPEG


async def test_a_failed_download_does_not_fail_the_sync_and_is_tried_again(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, server: FakeServer
) -> None:
    _with_photo(server, "PHOTO-1")
    server.pictures["PHOTO-1.jpg"] = JPEG
    server.picture_errors["PHOTO-1.jpg"] = BabyTrackerConnectionError("down")
    await setup_integration(hass, mock_config_entry)

    assert mock_config_entry.runtime_data.last_update_success
    assert _state(hass, PHOTO) == "unknown"
    assert _state(hass, "sensor.baby_one_last_diaper") != "unavailable"

    del server.picture_errors["PHOTO-1.jpg"]
    await tick(hass)
    assert await _image(hass, PHOTO) == JPEG
    assert _state(hass, PHOTO) != "unknown"
