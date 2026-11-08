"""Picture files the image entities show: each baby's photo and newest attached picture."""

from __future__ import annotations

import logging
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
# upload it later.
MISSING_RETRY = timedelta(hours=6)


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
    newest: tuple[float, AttachedPicture] | None = None
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
            key = record.time.timestamp()
            if newest is None or key > newest[0]:
                attached = AttachedPicture(file, record.record_type, record.object_id, record.time)
                newest = (key, attached)
    return newest[1] if newest is not None else None


class PictureCache:
    """The bytes of the picture files the entities show, downloaded once per file.

    Only files still wanted are kept, so a replaced photo is dropped. A failed download is tried
    again at the next sync, and a file the service doesn't have after ``MISSING_RETRY``.
    """

    def __init__(self, client: BabyTrackerClient) -> None:
        """Start with no files."""
        self._client = client
        self._files: dict[str, bytes] = {}
        self._missing: dict[str, datetime] = {}

    def get(self, file: str | None) -> bytes | None:
        """Return a downloaded file's bytes, or None when it isn't downloaded."""
        return self._files.get(file) if file is not None else None

    @property
    def files(self) -> Mapping[str, bytes]:
        """Every downloaded file, by name."""
        return self._files

    async def async_fetch(self, wanted: set[str]) -> None:
        """Download the wanted files not downloaded yet, and forget the ones no longer wanted."""
        for file in set(self._files) - wanted:
            del self._files[file]
        for file in set(self._missing) - wanted:
            del self._missing[file]
        now = dt_util.utcnow()
        for file in sorted(wanted - set(self._files)):
            missed = self._missing.get(file)
            if missed is not None and now - missed < MISSING_RETRY:
                continue
            try:
                content = await self._client.get_picture(file)
            except (BabyTrackerError, ValueError) as err:
                _LOGGER.debug("Couldn't download picture %s: %s", file, err)
                continue
            if content is None:
                self._missing[file] = now
                continue
            self._missing.pop(file, None)
            self._files[file] = content
