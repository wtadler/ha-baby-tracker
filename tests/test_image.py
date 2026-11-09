"""Core PR 23: each baby's photo and newest attached picture, as image entities."""

import asyncio
import logging
from datetime import timedelta
from typing import Any

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.image import async_get_image
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pybabytracker import BabyTrackerConnectionError, ProtocolError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.baby_tracker.const import DOMAIN
from custom_components.baby_tracker.pictures import FAILED_RETRY, MISSING_RETRY, PictureCache

from . import BABY_ONE, BABY_THREE, BABY_TWO, NOW, setup_integration, tick
from .factories import BABY_ONE_ID, DEVICE_A, at, baby_payload
from .fake_server import FakeServer
from .helpers import call, posted

PHOTO = "image.baby_one_photo"
LATEST = "image.baby_one_latest_picture"
JPEG = b"\xff\xd8\xffinvented photo"
OTHER_JPEG = b"\xff\xd8\xffinvented other photo"


@pytest.fixture(autouse=True)
def _start_at_now(freezer: FrozenDateTimeFactory) -> None:
    """Freeze the clock at NOW before anything is set up."""
    freezer.move_to(NOW)


def _with_photo(server: FakeServer, picture_name: Any, baby: dict[str, Any] = BABY_ONE) -> None:
    """A baby's own record, now with a photo."""
    server.add_payload(DEVICE_A, {**baby, "pictureName": picture_name})


def _picture(file_name: Any, media_type: Any = "0", **overrides: Any) -> dict[str, Any]:
    """A pictureNote entry as a phone is expected to sync one, with invented IDs."""
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


def _record(
    server: FakeServer,
    number: int,
    minute: float,
    *pictures: Any,
    type_name: str = "Milestone",
    **fields: Any,
) -> str:
    object_id = f"M0000000-0000-4000-8000-{number:012d}"
    server.add_record(
        DEVICE_A, type_name, object_id, when=at(minute), pictureNote=list(pictures), **fields
    )
    return object_id


async def _setup(
    hass: HomeAssistant, entry: MockConfigEntry, *, latest: bool = False
) -> MockConfigEntry:
    """Set the entry up, with Latest picture enabled when a test needs it."""
    if latest:
        entry.add_to_hass(hass)
        er.async_get(hass).async_get_or_create(
            "image",
            DOMAIN,
            f"{BABY_ONE_ID}_latest_picture",
            config_entry=entry,
            suggested_object_id="baby_one_latest_picture",
            disabled_by=None,
        )
    await setup_integration(hass, entry)
    return entry


async def _image(hass: HomeAssistant, entity_id: str) -> bytes:
    return (await async_get_image(hass, entity_id)).content


async def _no_image(hass: HomeAssistant, entity_id: str) -> None:
    with pytest.raises(HomeAssistantError):
        await async_get_image(hass, entity_id)


def _state(hass: HomeAssistant, entity_id: str) -> str:
    state = hass.states.get(entity_id)
    assert state is not None, f"{entity_id} does not exist"
    return state.state


async def test_a_babys_photo_is_downloaded_when_asked_for_and_then_kept(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, server: FakeServer
) -> None:
    """Test setup downloads nothing: the first request for the image does, once."""
    _with_photo(server, "PHOTO-1")
    server.pictures["PHOTO-1.jpg"] = JPEG
    await _setup(hass, mock_config_entry)

    assert _state(hass, PHOTO) == NOW.isoformat()
    assert server.picture_requests == []

    assert await _image(hass, PHOTO) == JPEG
    assert await _image(hass, PHOTO) == JPEG
    await tick(hass)
    assert await _image(hass, PHOTO) == JPEG
    assert server.picture_requests == ["PHOTO-1.jpg"]


async def test_a_baby_without_a_photo_has_an_unknown_image_and_nothing_is_asked_for(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    for entity_id in (PHOTO, "image.baby_two_photo"):
        assert _state(hass, entity_id) == "unknown"
        await _no_image(hass, entity_id)
    assert server.picture_requests == []


async def test_a_new_photo_replaces_the_old_one(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    server: FakeServer,
    freezer: FrozenDateTimeFactory,
) -> None:
    _with_photo(server, "PHOTO-1")
    server.pictures |= {"PHOTO-1.jpg": JPEG, "PHOTO-2.jpg": OTHER_JPEG}
    await _setup(hass, mock_config_entry)
    assert await _image(hass, PHOTO) == JPEG

    freezer.tick(timedelta(minutes=5))
    _with_photo(server, "PHOTO-2")
    await tick(hass)

    assert _state(hass, PHOTO) == dt_util.utcnow().isoformat()
    assert await _image(hass, PHOTO) == OTHER_JPEG
    assert server.picture_requests == ["PHOTO-1.jpg", "PHOTO-2.jpg"]


async def test_the_photo_is_not_asked_for_again_while_its_name_is_the_same(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    server: FakeServer,
    freezer: FrozenDateTimeFactory,
) -> None:
    _with_photo(server, "PHOTO-1")
    server.pictures["PHOTO-1.jpg"] = JPEG
    await _setup(hass, mock_config_entry)
    assert await _image(hass, PHOTO) == JPEG
    shown = _state(hass, PHOTO)

    freezer.tick(timedelta(hours=1))
    server.add_record(DEVICE_A, "Diaper", "D0000000-0000-4000-8000-000000000001", when=at(5))
    await tick(hass)

    assert _state(hass, PHOTO) == shown
    assert await _image(hass, PHOTO) == JPEG
    assert server.picture_requests == ["PHOTO-1.jpg"]


async def test_a_photo_removed_in_the_app_leaves_the_image_unknown(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, server: FakeServer
) -> None:
    _with_photo(server, "PHOTO-1")
    server.pictures["PHOTO-1.jpg"] = JPEG
    await _setup(hass, mock_config_entry)
    assert await _image(hass, PHOTO) == JPEG

    _with_photo(server, None)
    await tick(hass)

    assert _state(hass, PHOTO) == "unknown"
    await _no_image(hass, PHOTO)


@pytest.mark.parametrize(
    "name", ["", "   ", "a/b", "..", "a\\b", 5, True, ["x"], {"a": 1}, ".jpg"], ids=repr
)
async def test_a_photo_name_that_is_not_a_plain_file_name_is_never_requested(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, server: FakeServer, name: Any
) -> None:
    _with_photo(server, name)
    await _setup(hass, mock_config_entry)

    assert _state(hass, PHOTO) == "unknown"
    await _no_image(hass, PHOTO)
    assert server.picture_requests == []


async def test_a_picture_the_service_does_not_have_is_asked_for_again_hours_later(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    server: FakeServer,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Test a missing file shows no image, and isn't asked for at every request."""
    _with_photo(server, "PHOTO-1")
    await _setup(hass, mock_config_entry)
    await _no_image(hass, PHOTO)
    await _no_image(hass, PHOTO)
    assert server.picture_requests == ["PHOTO-1.jpg"]

    server.pictures["PHOTO-1.jpg"] = JPEG
    freezer.tick(MISSING_RETRY - timedelta(minutes=1))
    await _no_image(hass, PHOTO)
    assert server.picture_requests == ["PHOTO-1.jpg"]

    freezer.tick(timedelta(minutes=2))
    assert await _image(hass, PHOTO) == JPEG
    assert server.picture_requests == ["PHOTO-1.jpg", "PHOTO-1.jpg"]


async def test_a_failed_download_does_not_fail_the_sync_and_is_tried_again_after_a_delay(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    server: FakeServer,
    freezer: FrozenDateTimeFactory,
) -> None:
    _with_photo(server, "PHOTO-1")
    server.pictures["PHOTO-1.jpg"] = JPEG
    server.picture_errors["PHOTO-1.jpg"] = BabyTrackerConnectionError("down")
    await _setup(hass, mock_config_entry)

    await _no_image(hass, PHOTO)
    assert mock_config_entry.runtime_data.last_update_success
    assert _state(hass, "sensor.baby_one_last_diaper") != "unavailable"

    del server.picture_errors["PHOTO-1.jpg"]
    freezer.tick(FAILED_RETRY - timedelta(minutes=1))
    await _no_image(hass, PHOTO)
    assert server.picture_requests == ["PHOTO-1.jpg"]

    freezer.tick(timedelta(minutes=2))
    assert await _image(hass, PHOTO) == JPEG
    assert server.picture_requests == ["PHOTO-1.jpg", "PHOTO-1.jpg"]


@pytest.mark.parametrize(
    "error",
    [BabyTrackerConnectionError("down"), ProtocolError("redirected"), ValueError("bad name")],
    ids=["connection", "protocol", "value"],
)
async def test_a_failed_download_is_logged_once_at_warning(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    server: FakeServer,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
    error: Exception,
) -> None:
    _with_photo(server, "PHOTO-1")
    server.picture_errors["PHOTO-1.jpg"] = error
    await _setup(hass, mock_config_entry)
    caplog.set_level(logging.DEBUG)

    for _ in range(3):
        await _no_image(hass, PHOTO)
        freezer.tick(FAILED_RETRY + timedelta(minutes=1))

    assert len(server.picture_requests) == 3
    warnings = [
        record
        for record in caplog.records
        if record.levelno == logging.WARNING and "PHOTO-1" in record.getMessage()
    ]
    assert len(warnings) == 1
    assert warnings[0].name.startswith("custom_components.baby_tracker")


async def test_a_failure_after_a_success_is_logged_again_for_the_new_file(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    server: FakeServer,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _with_photo(server, "PHOTO-1")
    server.pictures["PHOTO-1.jpg"] = JPEG
    server.picture_errors["PHOTO-1.jpg"] = BabyTrackerConnectionError("down")
    await _setup(hass, mock_config_entry)
    await _no_image(hass, PHOTO)

    freezer.tick(FAILED_RETRY + timedelta(minutes=1))
    del server.picture_errors["PHOTO-1.jpg"]
    assert await _image(hass, PHOTO) == JPEG

    _with_photo(server, "PHOTO-2")
    server.picture_errors["PHOTO-2.jpg"] = BabyTrackerConnectionError("down again")
    await tick(hass)
    await _no_image(hass, PHOTO)

    warnings = [
        record.getMessage()
        for record in caplog.records
        if record.levelno == logging.WARNING and "PHOTO-" in record.getMessage()
    ]
    assert [("PHOTO-1" in message, "PHOTO-2" in message) for message in warnings] == [
        (True, False),
        (False, True),
    ]


async def test_a_download_that_hangs_blocks_neither_setup_a_sync_nor_a_write(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, server: FakeServer
) -> None:
    _with_photo(server, "PHOTO-1")
    server.pictures["PHOTO-1.jpg"] = JPEG
    server.picture_gate = asyncio.Event()
    await _setup(hass, mock_config_entry)

    # The long timeout keeps the clock moves below from timing the request out. The task isn't
    # one Home Assistant waits for, so settling the sync doesn't wait for the download.
    download = asyncio.get_running_loop().create_task(async_get_image(hass, PHOTO, timeout=3600))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert server.picture_requests == ["PHOTO-1.jpg"]
    assert not download.done()

    reads = server.device_list_calls
    await tick(hass)
    assert server.device_list_calls > reads
    await call(hass, "log_diaper", type="wet")
    assert len(posted(server)) == 1
    assert not download.done()

    server.picture_gate.set()
    assert (await download).content == JPEG


async def test_two_requests_for_one_file_download_it_once(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, server: FakeServer
) -> None:
    _with_photo(server, "PHOTO-1")
    server.pictures["PHOTO-1.jpg"] = JPEG
    server.picture_gate = asyncio.Event()
    await _setup(hass, mock_config_entry)

    first = hass.async_create_task(async_get_image(hass, PHOTO))
    second = hass.async_create_task(async_get_image(hass, PHOTO))
    await asyncio.sleep(0)
    server.picture_gate.set()

    assert (await first).content == JPEG
    assert (await second).content == JPEG
    assert server.picture_requests == ["PHOTO-1.jpg"]


async def test_the_image_is_served_through_its_access_token(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    server: FakeServer,
    hass_client: Any,
) -> None:
    _with_photo(server, "PHOTO-1")
    server.pictures["PHOTO-1.jpg"] = JPEG
    await _setup(hass, mock_config_entry)
    state = hass.states.get(PHOTO)
    assert state is not None
    url = state.attributes["entity_picture"]
    assert url.startswith("/api/image_proxy/image.baby_one_photo?token=")

    client = await hass_client()
    response = await client.get(url)

    assert response.status == 200
    assert await response.read() == JPEG


async def test_a_baby_added_later_gets_a_photo_image(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    assert hass.states.get("image.baby_three_photo") is None

    server.add_payload(DEVICE_A, {**BABY_THREE, "pictureName": "PHOTO-3"})
    server.pictures["PHOTO-3.jpg"] = JPEG
    await tick(hass)

    assert await _image(hass, "image.baby_three_photo") == JPEG


async def test_each_baby_shows_its_own_photo(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, server: FakeServer
) -> None:
    _with_photo(server, "PHOTO-1")
    _with_photo(server, "PHOTO-2", BABY_TWO)
    server.pictures |= {"PHOTO-1.jpg": JPEG, "PHOTO-2.jpg": OTHER_JPEG}
    await _setup(hass, mock_config_entry)

    assert await _image(hass, PHOTO) == JPEG
    assert await _image(hass, "image.baby_two_photo") == OTHER_JPEG


async def test_the_latest_picture_is_disabled_by_default(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """Test it can show any entry's picture (a diaper's, say), so it is opt-in."""
    assert hass.states.get(LATEST) is None
    registry = er.async_get(hass)
    entry = registry.async_get(LATEST)
    assert entry is not None
    assert entry.disabled_by is er.RegistryEntryDisabler.INTEGRATION
    photo = registry.async_get(PHOTO)
    assert photo is not None
    assert photo.disabled_by is None


async def test_the_latest_picture_is_the_newest_one_on_any_record(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, server: FakeServer
) -> None:
    older = _record(server, 1, 10, _picture("PICTURE-1"))
    newer = _record(server, 2, 30, _picture("PICTURE-2"))
    server.pictures |= {"PICTURE-1.jpg": JPEG, "PICTURE-2.jpg": OTHER_JPEG}
    await _setup(hass, mock_config_entry, latest=True)

    assert await _image(hass, LATEST) == OTHER_JPEG
    state = hass.states.get(LATEST)
    assert state is not None
    assert state.attributes["record_type"] == "Milestone"
    assert state.attributes["record_id"] == newer
    assert state.attributes["record_id"] != older
    assert state.attributes["time"] == at(30).isoformat()
    assert server.picture_requests == ["PICTURE-2.jpg"]


async def test_a_picture_on_a_diaper_is_the_latest_picture(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, server: FakeServer
) -> None:
    """Test why it is off by default: a diaper's photo shows here."""
    _record(server, 1, 30, _picture("DIAPER-1"), type_name="Diaper", status="0")
    server.pictures["DIAPER-1.jpg"] = JPEG
    await _setup(hass, mock_config_entry, latest=True)

    assert await _image(hass, LATEST) == JPEG
    state = hass.states.get(LATEST)
    assert state is not None
    assert state.attributes["record_type"] == "Diaper"


async def test_a_video_shows_its_thumbnail(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, server: FakeServer
) -> None:
    _record(server, 1, 10, _picture("VIDEO-1", media_type=1))
    server.pictures["VIDEO-1__thumbnail.jpg"] = JPEG
    await _setup(hass, mock_config_entry, latest=True)

    assert await _image(hass, LATEST) == JPEG
    assert server.picture_requests == ["VIDEO-1__thumbnail.jpg"]


async def test_a_deleted_picture_is_not_shown(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, server: FakeServer
) -> None:
    _record(server, 1, 10, _picture("PICTURE-1"))
    _record(server, 2, 30, _picture("PICTURE-2", deleted=True))
    server.pictures |= {"PICTURE-1.jpg": JPEG, "PICTURE-2.jpg": OTHER_JPEG}
    await _setup(hass, mock_config_entry, latest=True)

    assert await _image(hass, LATEST) == JPEG


async def test_ties_in_the_latest_picture_go_to_the_larger_record_id(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, server: FakeServer
) -> None:
    """Test two records at one time give the same answer on every reload."""
    _record(server, 2, 10, _picture("PICTURE-2"))
    _record(server, 1, 10, _picture("PICTURE-1"))
    _record(server, 3, 10, _picture("PICTURE-3"))
    server.pictures |= {f"PICTURE-{n}.jpg": JPEG for n in (1, 2, 3)}
    await _setup(hass, mock_config_entry, latest=True)

    state = hass.states.get(LATEST)
    assert state is not None
    assert state.attributes["record_id"] == "M0000000-0000-4000-8000-000000000003"


@pytest.mark.parametrize("media_type", [True, None, "x", 2, "2", [1], {"a": 1}, "", -1], ids=repr)
async def test_a_picture_that_is_neither_a_photo_nor_a_video_is_not_shown(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, server: FakeServer, media_type: Any
) -> None:
    _record(server, 1, 30, _picture("PICTURE-1", media_type=media_type))
    await _setup(hass, mock_config_entry, latest=True)

    assert _state(hass, LATEST) == "unknown"
    assert server.picture_requests == []


async def test_a_picture_with_no_media_type_key_is_not_shown(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, server: FakeServer
) -> None:
    entry = _picture("PICTURE-1")
    del entry["mediaType"]
    _record(server, 1, 30, entry)
    await _setup(hass, mock_config_entry, latest=True)

    assert _state(hass, LATEST) == "unknown"


@pytest.mark.parametrize("file_name", ["", "   ", "a/b", "..", 5, None, True, ["x"]], ids=repr)
async def test_a_picture_with_a_bad_file_name_is_not_shown_or_requested(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, server: FakeServer, file_name: Any
) -> None:
    _record(server, 1, 30, _picture(file_name))
    await _setup(hass, mock_config_entry, latest=True)

    assert _state(hass, LATEST) == "unknown"
    assert server.picture_requests == []


async def test_a_pictures_entry_that_is_not_a_picture_is_skipped(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, server: FakeServer
) -> None:
    _record(server, 1, 30, "x", 5, None, ["y"], _picture("PICTURE-1"))
    server.pictures["PICTURE-1.jpg"] = JPEG
    await _setup(hass, mock_config_entry, latest=True)

    assert await _image(hass, LATEST) == JPEG


async def test_another_babys_picture_is_not_shown(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, server: FakeServer
) -> None:
    _record(server, 1, 30, _picture("PICTURE-1"), baby=BABY_TWO)
    await _setup(hass, mock_config_entry, latest=True)

    assert _state(hass, LATEST) == "unknown"
    assert server.picture_requests == []


async def test_a_record_without_a_time_is_skipped(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, server: FakeServer
) -> None:
    _record(server, 1, 30, _picture("PICTURE-1"), time=None)
    await _setup(hass, mock_config_entry, latest=True)

    assert _state(hass, LATEST) == "unknown"


async def test_the_latest_picture_has_empty_attributes_without_a_picture(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    await _setup(hass, mock_config_entry, latest=True)

    state = hass.states.get(LATEST)
    assert state is not None
    assert state.state == "unknown"
    assert state.attributes["record_type"] is None
    assert state.attributes["record_id"] is None
    assert state.attributes["time"] is None


async def test_the_same_file_shown_twice_is_downloaded_once(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, server: FakeServer
) -> None:
    """Test the photo and the latest picture share one download of one file."""
    _with_photo(server, "SHARED")
    _record(server, 1, 30, _picture("SHARED"))
    server.pictures["SHARED.jpg"] = JPEG
    await _setup(hass, mock_config_entry, latest=True)

    assert await _image(hass, PHOTO) == JPEG
    assert await _image(hass, LATEST) == JPEG
    assert server.picture_requests == ["SHARED.jpg"]


async def test_a_new_latest_picture_replaces_the_old_one(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    server: FakeServer,
    freezer: FrozenDateTimeFactory,
) -> None:
    _record(server, 1, 10, _picture("PICTURE-1"))
    server.pictures |= {"PICTURE-1.jpg": JPEG, "PICTURE-2.jpg": OTHER_JPEG}
    await _setup(hass, mock_config_entry, latest=True)
    assert await _image(hass, LATEST) == JPEG

    freezer.tick(timedelta(minutes=5))
    _record(server, 2, 30, _picture("PICTURE-2"))
    await tick(hass)

    assert _state(hass, LATEST) == dt_util.utcnow().isoformat()
    assert await _image(hass, LATEST) == OTHER_JPEG


async def test_downloaded_pictures_do_not_pile_up_in_memory(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, server: FakeServer
) -> None:
    await _setup(hass, mock_config_entry)
    cache = mock_config_entry.runtime_data.pictures
    for number in range(30):
        server.pictures[f"P-{number}.jpg"] = JPEG
        assert await cache.async_get(f"P-{number}.jpg") == JPEG

    assert len(cache.files) <= 8


async def test_a_request_cancelled_mid_download_still_fills_the_cache(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, server: FakeServer
) -> None:
    """Test a slow download isn't lost when the frontend gives up on the request."""
    _with_photo(server, "PHOTO-1")
    server.pictures["PHOTO-1.jpg"] = JPEG
    server.picture_gate = asyncio.Event()
    await _setup(hass, mock_config_entry)

    request = asyncio.get_running_loop().create_task(async_get_image(hass, PHOTO, timeout=3600))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert server.picture_requests == ["PHOTO-1.jpg"]
    request.cancel()
    # Home Assistant reports a cancelled request as an image error.
    with pytest.raises(HomeAssistantError):
        await request

    server.picture_gate.set()
    for _ in range(5):
        await asyncio.sleep(0)

    assert list(mock_config_entry.runtime_data.pictures.files) == ["PHOTO-1.jpg"]
    assert await _image(hass, PHOTO) == JPEG
    assert server.picture_requests == ["PHOTO-1.jpg"]


async def test_the_downloads_still_running_stop_when_the_entry_unloads(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, server: FakeServer
) -> None:
    _with_photo(server, "PHOTO-1")
    server.pictures["PHOTO-1.jpg"] = JPEG
    server.picture_gate = asyncio.Event()
    await _setup(hass, mock_config_entry)
    request = asyncio.get_running_loop().create_task(async_get_image(hass, PHOTO, timeout=3600))
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    cache = mock_config_entry.runtime_data.pictures
    await hass.config_entries.async_unload(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    # Home Assistant reports a cancelled request as an image error.
    async with asyncio.timeout(5):
        with pytest.raises(HomeAssistantError):
            await request
    assert cache.files == {}
    assert cache.downloads == {}


async def test_the_cache_holds_two_files_for_each_baby(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, server: FakeServer
) -> None:
    for number in range(4, 9):
        server.add_payload(
            DEVICE_A,
            baby_payload(f"Baby {number}", f"{number}0000000-AAAA-4AAA-8AAA-00000000000{number}"),
        )
    await _setup(hass, mock_config_entry)
    cache = mock_config_entry.runtime_data.pictures
    for number in range(30):
        server.pictures[f"P-{number}.jpg"] = JPEG
        await cache.async_get(f"P-{number}.jpg")

    # Two babies from the fixtures and five more: seven babies, fourteen files.
    assert len(cache.files) == 14


async def test_a_finished_download_does_not_forget_a_newer_one_for_the_same_file() -> None:
    started: list[asyncio.Future[bytes | None]] = []

    def start(coro: Any, name: str) -> Any:
        coro.close()
        future: asyncio.Future[bytes | None] = asyncio.get_running_loop().create_future()
        started.append(future)
        return future

    cache = PictureCache(None, start)  # type: ignore[arg-type]
    first = asyncio.get_running_loop().create_task(cache.async_get("A.jpg"))
    await asyncio.sleep(0)
    older = cache.downloads["A.jpg"]
    # A newer download for the file takes the older one's place.
    newer = asyncio.get_running_loop().create_future()
    cache.downloads["A.jpg"] = newer

    older.set_result(None)
    await asyncio.sleep(0)
    await first

    assert cache.downloads["A.jpg"] is newer


async def test_the_cache_drops_the_oldest_files_when_a_baby_leaves(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, server: FakeServer
) -> None:
    await _setup(hass, mock_config_entry)
    cache = mock_config_entry.runtime_data.pictures
    cache.set_babies(7)
    for number in range(14):
        server.pictures[f"P-{number}.jpg"] = JPEG
        await cache.async_get(f"P-{number}.jpg")
    assert len(cache.files) == 14

    cache.set_babies(2)

    assert list(cache.files) == [f"P-{number}.jpg" for number in range(6, 14)]
