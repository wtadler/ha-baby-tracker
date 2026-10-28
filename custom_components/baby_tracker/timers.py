"""Loading Home Assistant's saved nursing and pump timers.

The timers themselves (sides, switching, rounding to whole minutes, the 240-minute cap) are the
library's: ``pybabytracker.NursingTimer`` and ``pybabytracker.PumpTimer``. A nursing timer started
in Home Assistant is saved with ``Store`` so it survives a restart, and becomes one
``add_nursing`` write at the stop; the pump timer works the same way for the account. This module
reads the saved copies back, dropping one that can't be read with a warning.
"""

import logging
from collections.abc import Mapping
from typing import Any

from pybabytracker import NursingTimer, PumpTimer
from pybabytracker.timers import LEFT, RIGHT, SIDES, other_side

_LOGGER = logging.getLogger(__name__)

__all__ = [
    "LEFT",
    "RIGHT",
    "SIDES",
    "NursingTimer",
    "PumpTimer",
    "load_pump_timer",
    "load_timers",
    "other_side",
]


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


def load_pump_timer(saved: Any) -> PumpTimer | None:
    """Return the saved pump timer, or None when there is none.

    Nothing saved, or an empty save (the timer was stopped), is no timer and says nothing. One
    that can't be read is dropped with a warning.
    """
    if saved is None or (isinstance(saved, Mapping) and not saved):
        return None
    try:
        if not isinstance(saved, Mapping):
            raise TypeError("not a mapping")
        return PumpTimer.from_dict(saved)
    except (KeyError, TypeError, ValueError, AttributeError) as err:
        _LOGGER.warning("Dropping a saved pump timer that can't be read (%r)", err)
        return None
