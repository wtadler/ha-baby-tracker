"""Picture files the image entities show: each baby's photo and newest attached picture."""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta

from homeassistant.util import dt as dt_util
from pybabytracker import (
    MEDIA_PHOTO,
    MEDIA_VIDEO,
    BabyTrackerClient,
    BabyTrackerError,
    Record,
)

_LOGGER = logging.getLogger(__name__)

# A file the service doesn't have isn't asked for again until this has passed: a phone may
# upload it later. A failed download waits less, as the cause is likelier to pass.
MISSING_RETRY = timedelta(hours=6)
FAILED_RETRY = timedelta(minutes=30)
# How many downloaded files are held at once.
MAX_FILES = 8


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
    thumbnail, since an image entity can't play one.
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
    so a slow picture host can't delay a sync or a write. Only the last few files are kept. Two
    requests for one file share a download. A file the service doesn't have is not asked for again
    for ``MISSING_RETRY``, and a failed download for ``FAILED_RETRY``. A failure is logged at
    warning level once per file, not at every attempt.
    """

    def __init__(self, client: BabyTrackerClient) -> None:
        """Start with no files."""
        self._client = client
        self._files: OrderedDict[str, bytes] = OrderedDict()
        self._retry_after: dict[str, datetime] = {}
        self._warned: set[str] = set()
        self._locks: dict[str, asyncio.Lock] = {}

    @property
    def files(self) -> Mapping[str, bytes]:
        """Every file held, by name."""
        return self._files

    def _cached(self, file: str) -> bytes | None:
        content = self._files.get(file)
        if content is not None:
            self._files.move_to_end(file)
        return content

    async def async_get(self, file: str | None) -> bytes | None:
        """Return a file's bytes, downloading it if it isn't held, or None when there are none."""
        if file is None:
            return None
        if (content := self._cached(file)) is not None:
            return content
        lock = self._locks.setdefault(file, asyncio.Lock())
        async with lock:
            if (content := self._cached(file)) is not None:
                return content
            content = await self._download(file)
        if not lock.locked():
            self._locks.pop(file, None)
        return content

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
        while len(self._files) > MAX_FILES:
            self._files.popitem(last=False)
        return content
