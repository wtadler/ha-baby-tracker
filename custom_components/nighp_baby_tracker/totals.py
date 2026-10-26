"""Today's totals for one baby, counted from the sync engine's records."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from homeassistant.const import UnitOfVolume
from homeassistant.util.unit_conversion import VolumeConverter

from .helpers import volume_parts
from .pybabytracker import (
    DIAPER_DIRTY,
    DIAPER_MIXED,
    DIAPER_WET,
    Diaper,
    Formula,
    Nursing,
    Pumped,
    Record,
)


@dataclass(frozen=True, slots=True)
class DayTotals:
    """What one baby had logged inside one local calendar day."""

    diapers: int = 0
    wet: int = 0
    dirty: int = 0
    mixed: int = 0
    feeds: int = 0
    # Formula and pumped bottles together, in milliliters.
    bottle_milliliters: float = 0.0


def day_totals(
    records: Iterable[Record], baby_id: str, start: datetime, end: datetime
) -> DayTotals:
    """Count a baby's records that happened from ``start`` up to, not including, ``end``.

    Only records the library models are counted: a raw record (a type the library lacks, or a
    known type it couldn't decode) is an ``UnknownRecord`` and matches none of the classes below.
    """
    diapers = wet = dirty = mixed = feeds = 0
    milliliters = 0.0
    for record in records:
        if record.time is None or record.baby_id != baby_id or not start <= record.time < end:
            continue
        if isinstance(record, Diaper):
            diapers += 1
            wet += record.status == DIAPER_WET
            dirty += record.status == DIAPER_DIRTY
            mixed += record.status == DIAPER_MIXED
        elif isinstance(record, Nursing | Formula | Pumped):
            feeds += 1
            if not isinstance(record, Nursing):
                amount, unit = volume_parts(record.volume)
                if amount is not None and unit is not None:
                    milliliters += VolumeConverter.convert(amount, unit, UnitOfVolume.MILLILITERS)
    return DayTotals(diapers, wet, dirty, mixed, feeds, milliliters)
