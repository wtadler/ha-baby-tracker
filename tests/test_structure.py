"""How the integration is put together: the shared entity base, coordinator data, icons, and
translations. Behavior stays the same, so most of these pin what must not change."""

import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import EVENT_STATE_REPORTED, STATE_UNAVAILABLE
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import DATA_DOMAIN_ENTITIES
from pytest_homeassistant_custom_component.common import MockConfigEntry
from syrupy.assertion import SnapshotAssertion

from custom_components.nighp_baby_tracker import const as integration_const
from custom_components.nighp_baby_tracker import entity as entity_module
from custom_components.nighp_baby_tracker.binary_sensor import BabyTrackerAsleepSensor
from custom_components.nighp_baby_tracker.button import BabyTrackerButton
from custom_components.nighp_baby_tracker.const import DOMAIN
from custom_components.nighp_baby_tracker.pybabytracker import BabyTrackerConnectionError
from custom_components.nighp_baby_tracker.sensor import (
    BabyTrackerLastPumpSensor,
    BabyTrackerSensor,
    BabyTrackerTimerSensor,
    BabyTrackerTodaySensor,
)

from .factories import BABY_ONE_ID, OP_UPDATE, at, baby_payload
from .fake_server import FakeServer
from .helpers import tick

INTEGRATION = Path(__file__).resolve().parent.parent / "custom_components" / "nighp_baby_tracker"
DIAPER = "sensor.baby_one_last_diaper"
LAST_PUMP = "sensor.baby_tracker_account_last_pump"


def _load(name: str) -> Any:
    return json.loads((INTEGRATION / name).read_text())


def _defined_in_entity_module(cls: type) -> bool:
    """Whether some class in ``cls``'s ancestry is defined in ``entity.py``."""
    return any(base.__module__ == entity_module.__name__ for base in cls.__mro__)


# The shared base entity


@pytest.mark.parametrize(
    "cls",
    [
        BabyTrackerButton,
        BabyTrackerSensor,
        BabyTrackerTodaySensor,
        BabyTrackerTimerSensor,
        BabyTrackerAsleepSensor,
        BabyTrackerLastPumpSensor,
    ],
)
def test_every_entity_class_builds_on_the_base_in_entity_py(cls: type) -> None:
    assert _defined_in_entity_module(cls)


def test_the_baby_and_account_bases_share_one_root() -> None:
    root = entity_module.BabyTrackerEntity
    assert issubclass(entity_module.BabyTrackerBabyEntity, root)
    assert issubclass(entity_module.BabyTrackerAccountEntity, root)
    assert BabyTrackerButton.__mro__.index(entity_module.BabyTrackerBabyEntity) > 0
    assert BabyTrackerLastPumpSensor.__mro__.index(entity_module.BabyTrackerAccountEntity) > 0


def test_the_old_today_entity_module_is_gone() -> None:
    assert not (INTEGRATION / "today_entity.py").exists()


async def test_entity_ids_unique_ids_and_devices_are_unchanged(
    hass: HomeAssistant, init_integration: MockConfigEntry, snapshot: SnapshotAssertion
) -> None:
    """A live install has these registered, so none of it may move."""
    entities = er.async_get(hass)
    devices = dr.async_get(hass)
    registered = {}
    for entry in sorted(
        er.async_entries_for_config_entry(entities, init_integration.entry_id),
        key=lambda e: e.unique_id,
    ):
        device = devices.async_get(entry.device_id) if entry.device_id else None
        registered[entry.entity_id] = {
            "unique_id": entry.unique_id.replace(init_integration.entry_id, "<entry>"),
            "translation_key": entry.translation_key,
            "device_name": device.name if device else None,
            "device_identifiers": sorted(
                (domain, ident.replace(init_integration.entry_id, "<entry>"))
                for domain, ident in (device.identifiers if device else ())
            ),
        }
    assert registered == snapshot


async def test_every_platform_has_its_states_after_the_refactor(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    domains = {
        entry.domain
        for entry in er.async_entries_for_config_entry(
            er.async_get(hass), init_integration.entry_id
        )
    }
    assert domains == {"button", "sensor", "binary_sensor"}
    assert hass.states.get(DIAPER).state == at(0).isoformat()
    assert hass.states.get("binary_sensor.baby_one_asleep").state == "off"
    assert hass.states.get(LAST_PUMP).state == "unknown"


# Device names are translated


def _device_info(hass: HomeAssistant, entity_id: str) -> dict[str, Any]:
    """The device info the live entity object offers the registry."""
    domain = entity_id.split(".")[0]
    return dict(hass.data[DATA_DOMAIN_ENTITIES][domain][entity_id].device_info)


async def test_the_account_device_name_comes_from_a_translation(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    info = _device_info(hass, LAST_PUMP)
    assert info.get("translation_key") == "account"
    assert "name" not in info
    [account] = dr.async_get(hass).async_get_devices(
        identifiers={(DOMAIN, f"{init_integration.entry_id}_account")}
    )
    assert account.name == "Baby Tracker account"


async def test_a_baby_with_no_name_gets_a_translated_fallback_name(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, server: FakeServer
) -> None:
    nameless_id = "44444444-DDDD-4DDD-8DDD-000000000004"
    server.add_record(
        "AAAAAAAA-0000-4000-8000-00000000000A",
        "Diaper",
        "D0000000-0000-4000-8000-000000000009",
        when=at(20),
        baby=baby_payload("", nameless_id),
        status=0,
    )
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    [nameless] = dr.async_get(hass).async_get_devices(identifiers={(DOMAIN, nameless_id)})
    assert nameless.name == "Baby"
    info = _device_info(
        hass,
        er.async_get(hass).async_get_entity_id("binary_sensor", DOMAIN, f"{nameless_id}_asleep"),
    )
    assert info.get("translation_key") == "baby"
    assert "name" not in info


async def test_a_named_baby_device_keeps_the_babys_name(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    [baby] = dr.async_get(hass).async_get_devices(identifiers={(DOMAIN, BABY_ONE_ID)})
    assert baby.name == "Baby One"
    info = _device_info(hass, "sensor.baby_one_last_diaper")
    assert info["name"] == "Baby One"
    assert "translation_key" not in info


def test_the_device_names_are_in_strings_and_the_english_translation() -> None:
    for name in ("strings.json", "translations/en.json"):
        devices = _load(name)["device"]
        assert devices["account"]["name"] == "Baby Tracker account"
        assert devices["baby"]["name"] == "Baby"


# Coordinator data


async def test_a_refresh_with_nothing_new_writes_no_states(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    reported: list[Event] = []

    @callback
    def record(event: Event) -> None:
        reported.append(event)

    @callback
    def is_ours(data: dict[str, Any]) -> bool:
        return data["entity_id"].split(".")[1].startswith(("baby_", "baby_tracker"))

    hass.bus.async_listen(EVENT_STATE_REPORTED, record, event_filter=is_ours)
    await tick(hass)
    assert reported == []


async def test_a_refresh_with_a_new_record_still_updates_the_sensor(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    server.add_record(
        "BBBBBBBB-0000-4000-8000-00000000000B",
        "Diaper",
        "D2000000-0000-4000-8000-000000000001",
        when=at(60),
    )
    await tick(hass)
    assert hass.states.get(DIAPER).state == at(60).isoformat()


async def test_a_record_changed_by_another_phone_still_updates_the_sensor(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    """The old record and its replacement must compare unequal, or always_update=False hides it."""
    before = dict(hass.states.get(DIAPER).attributes)
    server.add_record(
        "BBBBBBBB-0000-4000-8000-00000000000B",
        "Diaper",
        "D0000000-0000-4000-8000-000000000001",
        when=at(0),
        changed=at(30),
        op=OP_UPDATE,
        status=2,
    )
    await tick(hass)
    assert dict(hass.states.get(DIAPER).attributes) != before
    assert hass.states.get(DIAPER).attributes["type"] == "mixed"


async def test_a_pump_alone_reaches_the_account_sensor_on_a_refresh(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    """The last pump lives in the sync data, so a pump-only change isn't dropped."""
    assert hass.states.get(LAST_PUMP).state == "unknown"
    server.add_pump(
        "BBBBBBBB-0000-4000-8000-00000000000B", "F0000000-0000-4000-8000-000000000001", when=at(45)
    )
    await tick(hass)
    assert hass.states.get(LAST_PUMP).state == at(45).isoformat()


# Errors carry translation keys


async def test_a_failed_first_sync_is_reported_with_a_translation_key(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, server: FakeServer
) -> None:
    server.request_error = BabyTrackerConnectionError("offline")
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.SETUP_RETRY
    assert mock_config_entry.error_reason_translation_key == "sync_failed_update"
    assert "offline" in mock_config_entry.error_reason_translation_placeholders["error"]


async def test_a_failed_later_sync_raises_a_translated_update_failure(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    server: FakeServer,
    caplog: pytest.LogCaptureFixture,
) -> None:
    server.request_error = BabyTrackerConnectionError("offline")
    await tick(hass)

    assert hass.states.get(DIAPER).state == STATE_UNAVAILABLE
    # The message is the translated one, not the key.
    assert "Couldn't sync with the service: offline" in caplog.text


async def test_a_rejected_login_is_reported_with_a_translation_key(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, server: FakeServer
) -> None:
    server.password = "other-password"
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.SETUP_ERROR
    assert mock_config_entry.error_reason_translation_key == "auth_failed"


def test_the_new_exception_message_is_in_strings_and_the_english_translation() -> None:
    for name in ("strings.json", "translations/en.json"):
        assert "{error}" in _load(name)["exceptions"]["sync_failed_update"]["message"]


# Icons


def _icons() -> dict[str, Any]:
    return _load("icons.json")


def test_icons_json_covers_every_action() -> None:
    actions = set(yaml.safe_load((INTEGRATION / "services.yaml").read_text()))
    icons = _icons()["services"]
    assert actions
    assert set(icons) == actions
    for name, icon in icons.items():
        assert icon["service"].startswith("mdi:"), name


def test_icons_json_covers_every_entity_with_a_translation_key() -> None:
    entity_icons = _icons()["entity"]
    keys = _load("strings.json")["entity"]
    for platform, entities in keys.items():
        for key in entities:
            assert key in entity_icons[platform], f"{platform}.{key}"


def test_the_asleep_sensor_has_an_icon_for_each_state() -> None:
    asleep = _icons()["entity"]["binary_sensor"]["asleep"]
    assert set(asleep["state"]) == {"on", "off"}
    assert asleep["default"].startswith("mdi:")


# Constants and registry cleanup helpers


def test_email_and_password_constants_come_from_home_assistant() -> None:
    assert "CONF_EMAIL" not in vars(integration_const)
    assert "CONF_PASSWORD" not in vars(integration_const)


def test_the_integration_is_a_hub() -> None:
    assert _load("manifest.json")["integration_type"] == "hub"


async def test_a_failed_setup_closes_the_client(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, server: FakeServer
) -> None:
    """Regression guard for the move to ``entry.async_on_unload(client.close)``."""
    server.request_error = BabyTrackerConnectionError("offline")
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert server.clients
    assert server.open_clients == []


async def test_unloading_closes_the_client(
    hass: HomeAssistant, init_integration: MockConfigEntry, server: FakeServer
) -> None:
    assert len(server.open_clients) == 1
    await hass.config_entries.async_unload(init_integration.entry_id)
    await hass.async_block_till_done()
    assert server.open_clients == []


@pytest.mark.parametrize("rule", ["diagnostics", "icon-translations", "stale-devices"])
def test_the_quality_scale_marks_the_gold_rules_built_in_this_ticket(rule: str) -> None:
    scale = yaml.safe_load((INTEGRATION / "quality_scale.yaml").read_text())["rules"]
    assert scale[rule] == "done"
