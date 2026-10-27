"""Small helpers shared by the test modules."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.nighp_baby_tracker.const import CONF_DEVICE_UUID, DOMAIN
from custom_components.nighp_baby_tracker.pybabytracker import (
    OP_ADD,
    Nursing,
    decode_transaction,
)

from . import BABY_ONE, EMAIL, PASSWORD
from .factories import (
    BABY_ONE_ID,
    DEVICE_A,
    OP_UPDATE,
    record_payload,
)
from .fake_server import FakeServer

ROOT = Path(__file__).resolve().parent.parent
INTEGRATION = ROOT / "custom_components" / "nighp_baby_tracker"
VENDORED = INTEGRATION / "pybabytracker"

NOW = datetime(2026, 10, 26, 15, 0, 0, tzinfo=UTC)
OTHER_DEVICES_RECORD = "D0000000-0000-4000-8000-000000000001"  # baby one's diaper from device A


async def tick(hass: HomeAssistant, minutes: float = 5) -> None:
    """Let the clock reach the next scheduled sync (and a second past it), then settle."""
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=minutes, seconds=1))
    await hass.async_block_till_done()


async def sync_interval(hass: HomeAssistant, server: FakeServer) -> timedelta:
    """How often the integration syncs, found by moving the clock and counting the reads.

    A sync already scheduled at the old interval runs first, so the first minute is let go by.
    """
    await tick(hass, minutes=1)
    calls = server.device_list_calls
    await tick(hass, minutes=1)
    if server.device_list_calls > calls:
        return timedelta(minutes=1)
    await tick(hass, minutes=5)
    if server.device_list_calls > calls:
        return timedelta(minutes=5)
    raise AssertionError("no sync within five minutes")


def get_baby_device(registry: dr.DeviceRegistry, baby_id: str) -> dr.DeviceEntry | None:
    """Return the device registered for a baby, or None."""
    devices = registry.async_get_devices(identifiers={(DOMAIN, baby_id)})
    return devices[0] if devices else None


def button(baby: str, kind: str) -> str:
    return f"button.{baby}_{kind}_diaper"


async def press(hass: HomeAssistant, entity_id: str) -> None:
    await hass.services.async_call("button", "press", {"entity_id": entity_id}, blocking=True)


async def turn_on(hass: HomeAssistant, entity_id: str) -> None:
    await hass.services.async_call("switch", "turn_on", {"entity_id": entity_id}, blocking=True)


async def turn_off(hass: HomeAssistant, entity_id: str) -> None:
    await hass.services.async_call("switch", "turn_off", {"entity_id": entity_id}, blocking=True)


async def toggle(hass: HomeAssistant, entity_id: str) -> None:
    """Tap a switch the way a dashboard does: off if it reads on, else on."""
    found = hass.states.get(entity_id)
    assert found is not None, f"{entity_id} doesn't exist"
    await (turn_off if found.state == "on" else turn_on)(hass, entity_id)


def device_id(hass: HomeAssistant, baby_id: str) -> str:
    device = get_baby_device(dr.async_get(hass), baby_id)
    assert device is not None
    return device.id


async def log_diaper(hass: HomeAssistant, baby_id: str, kind: str = "wet", **extra: Any) -> None:
    data = {"device_id": device_id(hass, baby_id), "type": kind, **extra}
    await hass.services.async_call(DOMAIN, "log_diaper", data, blocking=True)


async def undo(hass: HomeAssistant, **data: Any) -> None:
    await hass.services.async_call(DOMAIN, "undo", data, blocking=True)


def posted(server: FakeServer) -> list[tuple[int, int, Any]]:
    """Everything the fake service received: (number, opcode, decoded record)."""
    return [(n, op, decode_transaction(t)) for n, op, t in server.posted]  # type: ignore[attr-defined]


def sensor_states(hass: HomeAssistant) -> dict[str, tuple[str, dict[str, Any]]]:
    return {
        state.entity_id: (state.state, dict(state.attributes))
        for state in hass.states.async_all("sensor")
    }


def reauth_flows(hass: HomeAssistant) -> list[str]:
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    return [flow["context"]["source"] for flow in flows]


def pump_entity(hass: HomeAssistant, entry: MockConfigEntry) -> str:
    """The entity ID of an entry's account-level last-pump sensor."""
    entity_id = er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, f"{entry.entry_id}_last_pump"
    )
    assert entity_id is not None, "the account's last-pump sensor doesn't exist"
    return entity_id


def account_device(hass: HomeAssistant, entry: MockConfigEntry) -> dr.DeviceEntry:
    entity = er.async_get(hass).async_get(pump_entity(hass, entry))
    assert entity is not None and entity.device_id is not None
    device = dr.async_get(hass).async_get(entity.device_id)
    assert device is not None
    return device


# The actions that name the account (a pump belongs to it), not a baby.
ACCOUNT_ACTIONS = ("log_pump", "start_pump", "stop_pump")


async def call(hass: HomeAssistant, action: str, baby_id: str = BABY_ONE_ID, **data: Any) -> None:
    """Call one of the integration's actions for a baby (a pump names its account instead)."""
    if action in ACCOUNT_ACTIONS:
        # A pump belongs to the account: it names the account, not a baby.
        [entry, *_] = hass.config_entries.async_entries(DOMAIN)
        data = {"config_entry_id": entry.entry_id, **data}
        await hass.services.async_call(DOMAIN, action, data, blocking=True)
        return
    await hass.services.async_call(
        DOMAIN, action, {"device_id": device_id(hass, baby_id), **data}, blocking=True
    )


NEW_YORK = ZoneInfo("America/New_York")


def local(year: int, month: int, day: int, hour: int = 0, minute: int = 0, second: int = 0):
    return datetime(year, month, day, hour, minute, second, tzinfo=NEW_YORK)


# The default "now" is Monday, October 26, 2026 at 4:00 p.m. Eastern (daylight time).
LOCAL_NOW = local(2026, 10, 26, 16)

MIDNIGHT = local(2026, 10, 27)

TODAY = local(2026, 10, 26, 9)


def baby_state(hass: HomeAssistant, entity: str, baby: str = "baby_one"):
    domain = "binary_sensor" if entity == "asleep" else "sensor"
    state = hass.states.get(f"{domain}.{baby}_{entity}")
    assert state is not None, f"{domain}.{baby}_{entity} does not exist"
    return state


def baby_value(hass: HomeAssistant, entity: str, baby: str = "baby_one") -> str:
    return baby_state(hass, entity, baby).state


async def setup_loaded(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED


async def pass_midnight(
    hass: HomeAssistant, clock: FrozenDateTimeFactory, moment: datetime = MIDNIGHT
) -> None:
    """Move the clock to just after a moment and let Home Assistant's timers run."""
    clock.move_to(moment + timedelta(seconds=1))
    async_fire_time_changed(hass, moment + timedelta(seconds=1))
    await hass.async_block_till_done()


async def log_pump(hass: HomeAssistant, **data: Any) -> None:
    await hass.services.async_call(DOMAIN, "log_pump", data, blocking=True)


def state_of(hass: HomeAssistant, entry: MockConfigEntry):
    state = hass.states.get(pump_entity(hass, entry))
    assert state is not None
    return state


PHONE_SLEEP = "5EE90000-0000-4000-8000-000000000001"

SLEEP_TIMER = "sensor.baby_one_sleep_timer"

NURSING_TIMER = "sensor.baby_one_nursing_timer"

TWO_SLEEP_TIMER = "sensor.baby_two_sleep_timer"

TWO_ASLEEP = "binary_sensor.baby_two_asleep"


def minutes(n: float) -> datetime:
    return NOW + timedelta(minutes=n)


def phone_sleep(
    server: FakeServer,
    object_id: str = PHONE_SLEEP,
    *,
    start: datetime,
    duration: int = 0,
    baby: dict[str, Any] | None = None,
) -> None:
    kwargs: dict[str, Any] = {"baby": baby} if baby is not None else {}
    server.add_record(DEVICE_A, "Sleep", object_id, when=start, duration=duration, **kwargs)


def phone_ends_sleep(
    server: FakeServer, object_id: str, *, start: datetime, duration: int, changed: datetime
) -> None:
    payload = record_payload(
        "Sleep", object_id, when=start, baby=BABY_ONE, changed=changed, duration=duration
    )
    payload["newFlage"] = "false"
    server.add_payload(DEVICE_A, payload, op=OP_UPDATE)


async def sync(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Let the clock reach the next scheduled sync."""
    await tick(hass)


def state(hass: HomeAssistant, entity_id: str) -> Any:
    found = hass.states.get(entity_id)
    assert found is not None, f"{entity_id} doesn't exist"
    return found


def nursing_posts(server: FakeServer) -> list[Nursing]:
    return [r for _, op, r in posted(server) if op == OP_ADD and isinstance(r, Nursing)]


async def nurse(hass: HomeAssistant, freezer: Any, *steps: tuple[float, str]) -> None:
    """Tap the nursing switches at the given minutes after NOW: (minute, "left" | "right")."""
    for minute, side in steps:
        freezer.move_to(minutes(minute))
        await toggle(hass, f"switch.baby_one_nursing_{side}")


LAST_DIAPER = "sensor.baby_one_last_diaper"

GAP_ISSUE = "own_log_gap_{}"


async def fail_to_write(hass: HomeAssistant) -> None:
    """Press a button while the gap is on, and check the error it raises."""
    with pytest.raises(HomeAssistantError) as raised:
        await press(hass, button("baby_one", "wet"))
    assert raised.value.translation_key == "own_log_gap"


def gap_issue(hass: HomeAssistant, entry: MockConfigEntry) -> ir.IssueEntry | None:
    return ir.async_get(hass).async_get_issue(DOMAIN, GAP_ISSUE.format(entry.entry_id))


LAST_FEED = "sensor.baby_one_last_feed"

LAST_PUMP = "sensor.baby_tracker_account_last_pump"  # the account's

PUMP_SWITCH = "switch.baby_tracker_account_pump_timer"

PUMP_TIMER = "sensor.baby_tracker_account_pump_timer"

LAST_SLEEP = "sensor.baby_one_last_sleep"

FEEDS_TODAY = "sensor.baby_one_feeds_today"

BOTTLE_TODAY = "sensor.baby_one_bottle_volume_today"

ASLEEP = "binary_sensor.baby_one_asleep"


async def set_options(hass: HomeAssistant, entry: MockConfigEntry, **options: Any) -> None:
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], options)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()


def only_record(server: FakeServer) -> Any:
    [(_, _, record)] = posted(server)
    return record


# Every baby action, the data it needs besides the baby, and whether a timer must already run.
BABY_ACTIONS: dict[str, tuple[dict[str, Any], str | None]] = {
    "log_diaper": ({"type": "wet"}, None),
    "log_bottle": ({"kind": "formula", "amount": 4}, None),
    "log_nursing": ({"left_minutes": 5}, None),
    "log_sleep": ({"start": "2026-10-26T13:00:00+00:00", "end": "2026-10-26T14:00:00+00:00"}, None),
    "start_sleep": ({}, None),
    "stop_sleep": ({}, "switch.baby_one_sleep"),
    "start_nursing": ({"side": "left"}, None),
    "switch_nursing_side": ({}, "switch.baby_one_nursing_left"),
    "stop_nursing": ({}, "switch.baby_one_nursing_left"),
    "discard_nursing": ({}, "switch.baby_one_nursing_left"),
}

ACTION_IDS = list(BABY_ACTIONS)

ALL_ACTIONS = [*BABY_ACTIONS, "log_pump", "start_pump", "stop_pump", "undo"]

DIAPER = "sensor.baby_one_last_diaper"

# The option an earlier version saved. It is no longer a setting, so these tests spell it out.
LEGACY_INTERVAL = "scan_interval"

SLEEP_SWITCH = "switch.baby_one_sleep"

NURSE_RIGHT = "switch.baby_one_nursing_right"

TWO_SLEEP_SWITCH = "switch.baby_two_sleep"

TWO_NURSE_LEFT = "switch.baby_two_nursing_left"

TWO_NURSE_RIGHT = "switch.baby_two_nursing_right"

NURSING_LEFT_TIME = "sensor.baby_one_nursing_left_time"

NURSING_RIGHT_TIME = "sensor.baby_one_nursing_right_time"

DISCARD_NURSING = "button.baby_one_discard_nursing"

NURSE_LEFT = "switch.baby_one_nursing_left"


async def entry_with_options(hass: HomeAssistant, options: dict[str, Any]) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=EMAIL,
        unique_id=EMAIL,
        data={
            CONF_EMAIL: EMAIL,
            CONF_PASSWORD: PASSWORD,
            CONF_DEVICE_UUID: "EEEEEEEE-0000-4000-8000-0000000000EE",
        },
        options=options,
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    return entry


def load_integration_json(name: str) -> Any:
    return json.loads((INTEGRATION / name).read_text())
