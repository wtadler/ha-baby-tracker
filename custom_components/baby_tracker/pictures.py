"""Picture files the image entities show: each baby's photo and newest attached picture."""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from collections.abc import Callable, Coroutine, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from homeassistant.util import dt as dt_util
from pybabytracker import (
    MEDIA_PHOTO,
    MEDIA_VIDEO,
    BabyTrackerClient,
    BabyTrackerError,
    Record,
)

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

# A file the service doesn't have isn't asked for again until this has passed: a phone may
# upload it later. A failed download waits less, as the cause is likelier to pass.
MISSING_RETRY = timedelta(hours=6)
FAILED_RETRY = timedelta(minutes=30)
# How many downloaded files are held at once: two for each baby (the photo and the latest picture),
# and at least this many.
MIN_FILES = 8
FILES_PER_BABY = 2


@dataclass(frozen=True, slots=True)
class AttachedPicture:
    """The newest picture attached to one of a baby's records, and what it's attached to."""

    file: str
    record_type: str
    record_id: str
    time: datetime | None


def newest_attached_picture(records: Iterable[Record], baby_id: str) -> AttachedPicture | None:
    """Return the newest picture on any of the baby's records.

    A record's time decides which is newest. A photo is shown as itself, and a video by its
    thumbnail, since an image entity can't play one. The video's thumbnail file (and a media type
    sent as a digit string) is the expected shape, not yet seen in synced data.
    """
    newest: tuple[tuple[float, str], AttachedPicture] | None = None
    for record in records:
        if record.baby_id != baby_id or record.time is None:
            continue
        for picture in record.pictures:
            if picture.media_type == MEDIA_PHOTO and picture.file is not None:
                file = picture.file
            elif picture.media_type == MEDIA_VIDEO:
                file = picture.thumbnail_file
            else:
                continue
            # Records at one time go to the larger ID, so a reload shows the same picture.
            key = (record.time.timestamp(), record.object_id)
            if newest is None or key > newest[0]:
                attached = AttachedPicture(file, record.record_type, record.object_id, record.time)
                newest = (key, attached)
    return newest[1] if newest is not None else None


class PictureCache:
    """The bytes of picture files, downloaded when an image entity is asked for one.

    A file is downloaded once, on its first request, and not while the coordinator's lock is held,
    so a slow picture host can't delay a sync or a write. Only the last few files are kept (see
    ``set_babies``). A download runs as a task of its own, shared by every request for the file,
    so a request that is cancelled (the frontend gives up after ten seconds, the library allows
    thirty) still lets it finish and fill the cache for the next one. A file the service doesn't
    have is not asked for again for ``MISSING_RETRY``, and a failed download for
    ``FAILED_RETRY``. A failure is logged at warning level once per file, not at every attempt.
    """

    def __init__(
        self,
        client: BabyTrackerClient,
        start: Callable[[Coroutine[Any, Any, bytes | None], str], asyncio.Task[bytes | None]],
    ) -> None:
        """Start with no files. ``start`` runs a download as a task the config entry owns."""
        self._client = client
        self._start = start
        self._files: OrderedDict[str, bytes] = OrderedDict()
        self._retry_after: dict[str, datetime] = {}
        self._warned: set[str] = set()
        self._downloads: dict[str, asyncio.Task[bytes | None]] = {}
        self._max_files = MIN_FILES

    @property
    def downloads(self) -> dict[str, asyncio.Task[bytes | None]]:
        """The downloads running, by file name."""
        return self._downloads

    @property
    def files(self) -> Mapping[str, bytes]:
        """Every file held, by name."""
        return self._files

    def _cached(self, file: str) -> bytes | None:
        content = self._files.get(file)
        if content is not None:
            self._files.move_to_end(file)
        return content

    def set_babies(self, count: int) -> None:
        """Size the cache for ``count`` babies."""
        self._max_files = max(MIN_FILES, FILES_PER_BABY * count)
        self._trim()

    def _trim(self) -> None:
        while len(self._files) > self._max_files:
            self._files.popitem(last=False)

    def _forget(self, file: str, task: asyncio.Task[bytes | None]) -> None:
        """Drop a finished download, unless a newer one for the file has taken its place."""
        if self._downloads.get(file) is task:
            del self._downloads[file]

    async def async_get(self, file: str | None) -> bytes | None:
        """Return a file's bytes, downloading it if it isn't held, or None when there are none."""
        if file is None:
            return None
        if (content := self._cached(file)) is not None:
            return content
        task = self._downloads.get(file)
        # A task that is done but not yet forgotten (its callback runs a loop turn later) is stale.
        if task is None or task.done():
            task = self._start(self._download(file), f"{DOMAIN} download picture {file}")
            self._downloads[file] = task
            task.add_done_callback(lambda done: self._forget(file, done))
        # Shielded, so cancelling this request leaves the download running.
        return await asyncio.shield(task)

    async def _download(self, file: str) -> bytes | None:
        now = dt_util.utcnow()
        self._retry_after = {f: t for f, t in self._retry_after.items() if t > now}
        if file in self._retry_after:
            return None
        try:
            content = await self._client.get_picture(file)
        except (BabyTrackerError, ValueError) as err:
            self._retry_after[file] = now + FAILED_RETRY
            if file not in self._warned:
                self._warned.add(file)
                _LOGGER.warning(
                    "Couldn't download picture %s (%s); trying again in %d minutes",
                    file,
                    err,
                    FAILED_RETRY.total_seconds() // 60,
                )
            return None
        if not content:
            self._retry_after[file] = now + MISSING_RETRY
            return None
        self._warned.discard(file)
        self._files[file] = content
        self._trim()
        return content
