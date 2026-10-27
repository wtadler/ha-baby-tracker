"""Home Assistant's own nursing timer.

The app doesn't sync a nursing timer while it runs: one finished record arrives when it stops
(PROTOCOL.md, "Timers"). So a nursing timer started in Home Assistant lives here, is saved with
``Store`` so it survives a restart, and becomes one ``add_nursing`` write at the stop.

Each side's time is rounded to whole minutes, half up. A session stopped so soon that both sides
round to 0 (under 30 seconds) is written as 1 minute on the side it ended on, rather than
refused: the press was deliberate, and a refusal would leave the timer running. A side longer
than 240 minutes (a forgotten timer) is written as 240, the most the library accepts.
"""

import logging
import math
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any

from homeassistant.util import dt as dt_util

from .pybabytracker.builders import MAX_MINUTES_PER_SIDE

_LOGGER = logging.getLogger(__name__)

LEFT = "left"
RIGHT = "right"
SIDES = (LEFT, RIGHT)


def other_side(side: str) -> str:
    """Return the other side of the body."""
    return RIGHT if side == LEFT else LEFT


def _whole_minutes(seconds: float) -> int:
    return math.floor(seconds / 60 + 0.5)


@dataclass(frozen=True, slots=True)
class NursingTimer:
    """A running nursing session: when it began, the side now, and the time on finished sides.

    ``left_seconds`` and ``right_seconds`` count only the stretches before ``side_started``; the
    current side's running time is ``now - side_started``. ``object_id`` is the record the stop
    writes, chosen at the start so a retried stop can't write twice.
    """

    object_id: str
    started: datetime
    side: str
    side_started: datetime
    left_seconds: float = 0.0
    right_seconds: float = 0.0

    @classmethod
    def start(cls, object_id: str, side: str, now: datetime) -> NursingTimer:
        """Start a timer on ``side`` at ``now``."""
        return cls(object_id, now, side, now)

    def switched(self, side: str, now: datetime) -> NursingTimer:
        """Return the timer on ``side`` from ``now``, with the current stretch added to its side."""
        if side == self.side:
            return self
        left, right = self.totals(now)
        return replace(self, side=side, side_started=now, left_seconds=left, right_seconds=right)

    def totals(self, now: datetime) -> tuple[float, float]:
        """Seconds on each side, counting the current stretch up to ``now``."""
        running = max(0.0, (now - self.side_started).total_seconds())
        if self.side == LEFT:
            return self.left_seconds + running, self.right_seconds
        return self.left_seconds, self.right_seconds + running

    def minutes(self, now: datetime) -> tuple[int, int]:
        """Whole minutes per side to write at ``now`` (see the module notes)."""
        left_s, right_s = self.totals(now)
        left, right = _whole_minutes(left_s), _whole_minutes(right_s)
        if left + right == 0:
            return (1, 0) if self.side == LEFT else (0, 1)
        if left > MAX_MINUTES_PER_SIDE or right > MAX_MINUTES_PER_SIDE:
            _LOGGER.warning(
                "Nursing timer %s ran past %s minutes on a side; writing %s",
                self.object_id,
                MAX_MINUTES_PER_SIDE,
                MAX_MINUTES_PER_SIDE,
            )
        return min(left, MAX_MINUTES_PER_SIDE), min(right, MAX_MINUTES_PER_SIDE)

    def to_dict(self) -> dict[str, Any]:
        """Return the timer as saveable data."""
        return {
            "object_id": self.object_id,
            "started": self.started.isoformat(),
            "side": self.side,
            "side_started": self.side_started.isoformat(),
            "left_seconds": self.left_seconds,
            "right_seconds": self.right_seconds,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> NursingTimer:
        """Rebuild a saved timer.

        Raises ``ValueError`` (or ``KeyError``/``TypeError``) on bad data.
        """
        side = data["side"]
        if side not in SIDES:
            raise ValueError(f"unknown side {side!r}")
        started = dt_util.parse_datetime(str(data["started"]))
        side_started = dt_util.parse_datetime(str(data["side_started"]))
        if started is None or side_started is None:
            raise ValueError("bad time")
        if started.tzinfo is None or side_started.tzinfo is None:
            raise ValueError("naive time")
        left = float(data.get("left_seconds", 0.0))
        right = float(data.get("right_seconds", 0.0))
        if not (math.isfinite(left) and math.isfinite(right)) or left < 0 or right < 0:
            raise ValueError("bad seconds")
        return cls(str(data["object_id"]), started, side, side_started, left, right)


def load_timers(saved: Any) -> dict[str, NursingTimer]:
    """Return the saved timers by baby ID. One that can't be read is dropped with a warning."""
    if not isinstance(saved, Mapping):
        return {}
    timers: dict[str, NursingTimer] = {}
    for baby_id, data in saved.items():
        try:
            timers[str(baby_id)] = NursingTimer.from_dict(data)
        except (KeyError, TypeError, ValueError, AttributeError) as err:
            _LOGGER.warning("Dropping a saved nursing timer that can't be read (%r)", err)
    return timers
