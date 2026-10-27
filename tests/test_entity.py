"""HACS only: the shared entity base and the device names, checked across every platform at
once. Core covers each entity with its own platform snapshot."""

from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import DATA_DOMAIN_ENTITIES
from pytest_homeassistant_custom_component.common import MockConfigEntry
from syrupy.assertion import SnapshotAssertion

from custom_components.nighp_baby_tracker import entity as entity_module
from custom_components.nighp_baby_tracker.binary_sensor import BabyTrackerAsleepSensor
from custom_components.nighp_baby_tracker.button import BabyTrackerButton
from custom_components.nighp_baby_tracker.const import DOMAIN
from custom_components.nighp_baby_tracker.sensor import (
    BabyTrackerLastPumpSensor,
    BabyTrackerNursingTimeSensor,
    BabyTrackerSensor,
    BabyTrackerTimerSensor,
    BabyTrackerTodaySensor,
)
from custom_components.nighp_baby_tracker.switch import BabyTrackerSwitch

from .factories import BABY_ONE_ID, at, baby_payload
from .fake_server import FakeServer
from .helpers import DIAPER, INTEGRATION, LAST_PUMP, load_integration_json


def _defined_in_entity_module(cls: type) -> bool:
    """Whether some class in ``cls``'s ancestry is defined in ``entity.py``."""
    return any(base.__module__ == entity_module.__name__ for base in cls.__mro__)


def _device_info(hass: HomeAssistant, entity_id: str) -> dict[str, Any]:
    """The device info the live entity object offers the registry."""
    domain = entity_id.split(".")[0]
    return dict(hass.data[DATA_DOMAIN_ENTITIES][domain][entity_id].device_info)


# The shared base entity


@pytest.mark.parametrize(
    "cls",
    [
        BabyTrackerButton,
        BabyTrackerSensor,
        BabyTrackerTodaySensor,
        BabyTrackerTimerSensor,
        BabyTrackerNursingTimeSensor,
        BabyTrackerSwitch,
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
    assert domains == {"button", "sensor", "binary_sensor", "switch"}
    assert hass.states.get(DIAPER).state == at(0).isoformat()
    assert hass.states.get("binary_sensor.baby_one_asleep").state == "off"
    assert hass.states.get(LAST_PUMP).state == "unknown"


# Device names are translated


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
        devices = load_integration_json(name)["device"]
        assert devices["account"]["name"] == "Baby Tracker account"
        assert devices["baby"]["name"] == "Baby"
