"""The guard on a forgotten nursing or pump timer: a repair once one runs past its hour."""

from collections.abc import Callable, Mapping
from datetime import datetime, timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_track_point_in_utc_time
from homeassistant.util import dt as dt_util

from .const import (
    DOMAIN,
    NURSING_TIMER_GUARD_AFTER,
    PUMP_TIMER_GUARD_AFTER,
    nursing_guard_issue_id,
    pump_guard_issue_id,
)
from .timer_state import TimerState

# The guard's key for the account's pump timer; a baby's is the baby's ID (a UUID, so no clash).
PUMP_GUARD_KEY = "pump"


class TimerGuard:
    """Raises a repair for each nursing or pump timer past its hour, and clears finished ones.

    These timers live only in Home Assistant, so a forgotten one can't be seen or ended anywhere
    else. The guard keeps the timers whose repair is raised (by guard key, with the repair's ID),
    and the callback waiting for each other timer's hour to end. A key is a baby's ID for a
    nursing timer, or ``PUMP_GUARD_KEY``.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        timers: TimerState,
        republish: Callable[[], None],
    ) -> None:
        """Set up a guard over one entry's timers.

        ``republish`` publishes fresh data without touching ``last_update_success``, so entities
        that are unavailable because a sync is failing stay unavailable.
        """
        self._hass = hass
        self._entry = entry
        self._timers = timers
        self._republish = republish
        self._raised: dict[str, str] = {}
        self._waiting: dict[str, tuple[datetime, CALLBACK_TYPE]] = {}

    def async_setup(self) -> None:
        """Drop the repairs and callbacks on unload or removal, which only a loaded entry owns."""
        self._entry.async_on_unload(self.async_stop)

    def _guarded_timers(
        self, names: Mapping[str, str | None]
    ) -> dict[str, tuple[datetime, timedelta, str, str, dict[str, str]]]:
        """Return each running guarded timer: start, guard time, repair ID, text, and placeholders.

        Keyed by the guard key: a baby's ID for a nursing timer, ``PUMP_GUARD_KEY`` for the pump.
        """
        entry_id = self._entry.entry_id
        timers: dict[str, tuple[datetime, timedelta, str, str, dict[str, str]]] = {
            baby_id: (
                timer.started,
                NURSING_TIMER_GUARD_AFTER,
                nursing_guard_issue_id(entry_id, baby_id),
                "nursing_timer_long",
                {"baby": names[baby_id] or "a baby"},
            )
            for baby_id, timer in self._timers.nursing.items()
        }
        if self._timers.pump is not None:
            timers[PUMP_GUARD_KEY] = (
                self._timers.pump.started,
                PUMP_TIMER_GUARD_AFTER,
                pump_guard_issue_id(entry_id),
                "pump_timer_long",
                {},
            )
        return timers

    def check(self, names: Mapping[str, str | None]) -> None:
        """Raise a repair for each timer past its hour, and clear finished ones.

        A timer not yet an hour old gets a callback for the moment it turns one, so the repair
        doesn't wait for a sync. ``names`` maps each known baby's ID to their name. Nothing here
        touches the network.
        """
        now = dt_util.utcnow()
        timers = self._guarded_timers(names)
        for key, (started, after, issue_id, translation_key, placeholders) in timers.items():
            due = started + after
            if now >= due:
                self._cancel_wait(key)
                if key not in self._raised:
                    self._raised[key] = issue_id
                    ir.async_create_issue(
                        self._hass,
                        DOMAIN,
                        issue_id,
                        is_fixable=False,
                        severity=ir.IssueSeverity.WARNING,
                        translation_key=translation_key,
                        translation_placeholders=placeholders,
                    )
            else:
                self._clear_issue(key)
                waiting = self._waiting.get(key)
                if waiting is None or waiting[0] != due:
                    self._cancel_wait(key)
                    self._waiting[key] = (
                        due,
                        async_track_point_in_utc_time(self._hass, self.async_due, due),
                    )
        for key in [*self._raised, *self._waiting]:
            if key not in timers:
                self._clear_issue(key)
                self._cancel_wait(key)

    def _clear_issue(self, key: str) -> None:
        if (issue_id := self._raised.pop(key, None)) is not None:
            ir.async_delete_issue(self._hass, DOMAIN, issue_id)

    def _cancel_wait(self, key: str) -> None:
        if (waiting := self._waiting.pop(key, None)) is not None:
            waiting[1]()

    @callback
    def async_due(self, now: datetime) -> None:
        """Publish when a timer reaches its hour: raise its repair and ease polling."""
        for key, (due, _) in list(self._waiting.items()):
            if due <= now:
                del self._waiting[key]
        self._republish()

    @callback
    def async_stop(self) -> None:
        """Drop the repairs and callbacks."""
        for key in [*self._raised, *self._waiting]:
            self._clear_issue(key)
            self._cancel_wait(key)
