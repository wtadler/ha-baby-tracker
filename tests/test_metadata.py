"""HACS only: services.yaml, strings, icons, manifest, quality scale, and the library's pin.
Core checks these with hassfest, so this file does not go to core."""

import json
import re
from pathlib import Path
from typing import Any

import pybabytracker
import pytest
import yaml
from homeassistant.core import is_callback

from custom_components.baby_tracker import const as integration_const
from custom_components.baby_tracker.const import (
    CONF_BOTTLE_SIZE,
    CONF_BOTTLE_TYPE,
    CONF_BOTTLE_UNIT,
    DOMAIN,
)

from .helpers import (
    ACTION_IDS,
    ALL_ACTIONS,
    INTEGRATION,
    ROOT,
    load_integration_json,
)


def _yaml() -> dict[str, Any]:
    return yaml.safe_load((INTEGRATION / "services.yaml").read_text())


def _strings() -> dict[str, Any]:
    return json.loads((INTEGRATION / "strings.json").read_text())


def _icons() -> dict[str, Any]:
    return load_integration_json("icons.json")


BRONZE_RULES = [
    "action-setup",
    "appropriate-polling",
    "brands",
    "common-modules",
    "config-flow-test-coverage",
    "config-flow",
    "dependency-transparency",
    "docs-actions",
    "docs-conditions",
    "docs-high-level-description",
    "docs-installation-instructions",
    "docs-removal-instructions",
    "docs-triggers",
    "entity-event-setup",
    "entity-unique-id",
    "has-entity-name",
    "runtime-data",
    "test-before-configure",
    "test-before-setup",
    "unique-config-entry",
]


def _load(path: Path) -> Any:
    return json.loads(path.read_text())


# Failed writes


async def test_the_unknown_outcome_message_asks_the_user_to_try_again_in_a_minute() -> None:
    strings = json.loads((INTEGRATION / "strings.json").read_text())
    message = strings["exceptions"]["write_outcome_unknown"]["message"]
    assert "still being confirmed" in message
    assert "try again in a minute" in message.lower()


def test_the_repair_text_has_numbered_steps_and_names_the_cost() -> None:
    issue = json.loads((INTEGRATION / "strings.json").read_text())["issues"]["own_log_gap"]
    text = issue["description"]
    assert "more than an hour" in text
    for step in ("1. ", "2. ", "3. "):
        assert step in text
    assert "history" in text
    assert "new device" in text
    assert "{account}" in text
    assert "short delay" not in text  # it has already lasted an hour: don't say it may clear itself


# Metadata


def test_services_yaml_describes_the_diaper_and_undo_actions_and_their_fields() -> None:
    import yaml

    services = yaml.safe_load((INTEGRATION / "services.yaml").read_text())
    assert set(services) == {
        "log_diaper",
        "undo",
        "log_bottle",
        "log_nursing",
        "log_sleep",
        "log_pump",
        "start_pump",
        "stop_pump",
        "start_sleep",
        "stop_sleep",
        "start_nursing",
        "switch_nursing_side",
        "stop_nursing",
        "discard_nursing",
    }
    assert set(services["log_diaper"]["fields"]) == {
        "device_id",
        "type",
        "time",
        "note",
        "object_id",
    }
    assert set(services["undo"]["fields"]) == {"config_entry_id", "device_id", "object_id"}
    baby = services["log_diaper"]["fields"]["device_id"]
    assert baby["required"] is True
    assert baby["selector"]["device"]["filter"] == [{"integration": DOMAIN}]
    assert services["log_diaper"]["fields"]["type"]["selector"]["select"]["options"] == [
        "wet",
        "dirty",
        "mixed",
    ]


def test_every_action_field_has_a_translated_name_and_description() -> None:
    import yaml

    services = yaml.safe_load((INTEGRATION / "services.yaml").read_text())
    strings = json.loads((INTEGRATION / "strings.json").read_text())["services"]
    assert set(strings) == set(services)
    for action, spec in services.items():
        assert strings[action]["name"] and strings[action]["description"]
        assert set(strings[action]["fields"]) == set(spec["fields"])
        for field in strings[action]["fields"].values():
            assert field["name"] and field["description"]


def test_every_error_and_issue_the_integration_raises_has_a_translation() -> None:
    strings = json.loads((INTEGRATION / "strings.json").read_text())
    assert set(strings["exceptions"]) >= {
        "baby_not_found",
        "nothing_to_undo",
        "record_not_for_baby",
        "not_own_record",
        "record_not_found",
        "write_outcome_unknown",
        "own_log_gap",
        "auth_failed",
        "sync_failed",
        "write_failed",
        "no_nursing_timer",
    }
    assert all(item["message"] for item in strings["exceptions"].values())
    assert strings["issues"]["own_log_gap"]["title"]
    assert "{account}" in strings["issues"]["own_log_gap"]["description"]
    assert {"wet_diaper", "dirty_diaper", "mixed_diaper"} <= set(strings["entity"]["button"])
    long_timer = strings["issues"]["nursing_timer_long"]
    assert long_timer["title"]
    assert "{baby}" in long_timer["title"] or "{baby}" in long_timer["description"]
    assert "discard" in long_timer["description"].lower()


def test_the_action_setup_rule_is_done() -> None:
    import yaml

    scale = yaml.safe_load((INTEGRATION / "quality_scale.yaml").read_text())["rules"]
    assert scale["action-setup"] == "done"
    assert scale["docs-actions"] == "done"


# --- metadata ----------------------------------------------------------------------------------


def test_services_yaml_describes_the_new_actions_and_their_fields() -> None:
    import yaml

    services = yaml.safe_load((INTEGRATION / "services.yaml").read_text())
    assert set(services) == {
        "log_diaper",
        "undo",
        "log_bottle",
        "log_nursing",
        "log_sleep",
        "log_pump",
        "start_pump",
        "stop_pump",
        "start_sleep",
        "stop_sleep",
        "start_nursing",
        "switch_nursing_side",
        "stop_nursing",
        "discard_nursing",
    }
    fields = {name: set(spec["fields"]) for name, spec in services.items()}
    assert fields["log_bottle"] == {
        "device_id",
        "kind",
        "amount",
        "unit",
        "time",
        "note",
        "object_id",
    }
    assert fields["log_nursing"] == {
        "device_id",
        "left_minutes",
        "right_minutes",
        "finish_side",
        "end_time",
        "note",
        "object_id",
    }
    assert fields["log_sleep"] == {"device_id", "start", "end", "note", "object_id"}
    assert fields["log_pump"] == {
        "config_entry_id",
        "amount",
        "minutes",
        "left_amount",
        "right_amount",
        "unit",
        "left_minutes",
        "right_minutes",
        "finish_side",
        "time",
        "note",
        "object_id",
    }
    bottle = services["log_bottle"]["fields"]
    assert bottle["kind"]["required"] is True
    assert bottle["amount"]["required"] is True
    assert bottle["unit"]["selector"]["select"]["options"] == ["fl. oz.", "mL"]
    for action in ("log_bottle", "log_nursing", "log_sleep"):
        assert services[action]["fields"]["device_id"]["selector"]["device"]["filter"] == [
            {"integration": DOMAIN}
        ]


def test_the_new_actions_errors_options_and_button_are_translated() -> None:
    strings = json.loads((INTEGRATION / "strings.json").read_text())
    assert {
        "minutes_too_long",
        "sleep_too_long",
        "amount_too_small",
        "invalid_amount",
        "end_before_start",
        "no_duration",
        "no_amount",
    } <= set(strings["exceptions"])
    assert "metric_not_supported" not in strings["exceptions"]
    assert "bottle" in strings["entity"]["button"]
    options = strings["options"]["step"]["init"]
    for key in (CONF_BOTTLE_SIZE, CONF_BOTTLE_UNIT, CONF_BOTTLE_TYPE):
        assert options["data"][key] and options["data_description"][key]
    assert "isn't supported" not in options["data_description"][CONF_BOTTLE_UNIT]
    assert "120 mL" in options["data_description"][CONF_BOTTLE_SIZE]
    for text in json.dumps(strings["services"]).split('"description": '):
        assert "refused for now" not in text
    assert {"formula", "pumped"} <= set(strings["selector"]["bottle_type"]["options"])
    assert strings == json.loads((INTEGRATION / "translations" / "en.json").read_text())


# Setup


def test_the_actions_are_set_up_by_a_callback_named_for_core() -> None:
    from custom_components.baby_tracker import services

    assert is_callback(services.async_setup_services)
    assert not hasattr(services, "async_register_services")


# services.yaml and strings


@pytest.mark.parametrize("action", ACTION_IDS)
def test_every_baby_action_has_a_required_device_id_field(action: str) -> None:
    fields = _yaml()[action]["fields"]

    assert "baby" not in fields
    assert fields["device_id"]["required"] is True
    assert fields["device_id"]["selector"] == {"device": {"filter": [{"integration": DOMAIN}]}}


def test_log_pump_has_a_required_config_entry_id_and_no_device_field() -> None:
    fields = _yaml()["log_pump"]["fields"]

    assert {"device", "baby", "device_id"}.isdisjoint(fields)
    assert fields["config_entry_id"]["required"] is True
    assert fields["config_entry_id"]["selector"] == {"config_entry": {"integration": DOMAIN}}


def test_undo_offers_a_device_for_a_baby_and_an_account_for_all() -> None:
    fields = _yaml()["undo"]["fields"]

    assert set(fields) == {"config_entry_id", "device_id", "object_id"}
    assert fields["device_id"]["selector"] == {"device": {"filter": [{"integration": DOMAIN}]}}
    assert fields["config_entry_id"]["selector"] == {"config_entry": {"integration": DOMAIN}}


@pytest.mark.parametrize("action", ALL_ACTIONS)
def test_every_yaml_field_has_a_name_and_description_and_no_alias_is_left(action: str) -> None:
    strings = _strings()["services"][action]["fields"]

    assert set(strings) == set(_yaml()[action]["fields"])
    assert all(field["name"] and field["description"] for field in strings.values())


def test_the_deprecation_repair_is_translated() -> None:
    issue = _strings()["issues"]["deprecated_action_field"]

    assert issue["title"]
    for placeholder in ("{action}", "{field}", "{replacement}"):
        assert placeholder in issue["description"]


# Errors carry translation keys


def test_the_new_exception_message_is_in_strings_and_the_english_translation() -> None:
    for name in ("strings.json", "translations/en.json"):
        assert (
            "{error}" in load_integration_json(name)["exceptions"]["sync_failed_update"]["message"]
        )


# Icons


def test_icons_json_covers_every_action() -> None:
    actions = set(yaml.safe_load((INTEGRATION / "services.yaml").read_text()))
    icons = _icons()["services"]
    assert actions
    assert set(icons) == actions
    for name, icon in icons.items():
        assert icon["service"].startswith("mdi:"), name


def test_icons_json_covers_every_entity_with_a_translation_key() -> None:
    entity_icons = _icons()["entity"]
    keys = load_integration_json("strings.json")["entity"]
    for platform, entities in keys.items():
        for key in entities:
            assert key in entity_icons[platform], f"{platform}.{key}"


def test_the_asleep_sensor_has_an_icon_for_each_state() -> None:
    asleep = _icons()["entity"]["binary_sensor"]["asleep"]
    assert set(asleep["state"]) == {"on", "off"}
    assert asleep["default"].startswith("mdi:")


@pytest.mark.parametrize("key", ["sleep", "nursing_left", "nursing_right"])
def test_each_timer_switch_has_an_icon_for_on_and_off(key: str) -> None:
    icons = _icons()["entity"]["switch"][key]
    assert set(icons["state"]) == {"on", "off"}
    assert icons["state"]["on"] != icons["state"]["off"]
    assert all(icon.startswith("mdi:") for icon in (icons["default"], *icons["state"].values()))


def test_the_timer_switches_and_discard_button_are_translated_and_the_old_buttons_are_gone() -> (
    None
):
    entity = load_integration_json("strings.json")["entity"]
    assert {key: value["name"] for key, value in entity["switch"].items()} == {
        "sleep": "Sleep",
        "nursing_left": "Nursing left",
        "nursing_right": "Nursing right",
        "pump": "Pump timer",
    }
    assert entity["button"]["discard_nursing"]["name"] == "Discard nursing"
    for key in ("sleep", "nurse_left", "nurse_right"):
        assert key not in entity["button"]
        assert key not in _icons()["entity"]["button"]


def test_the_repair_text_names_the_switches_and_the_discard_button_not_the_old_buttons() -> None:
    text = load_integration_json("strings.json")["issues"]["nursing_timer_long"]["description"]
    assert "Nurse button" not in text
    assert "Nursing switch" in text
    assert "Discard nursing button" in text


# Constants and registry cleanup helpers


def test_email_and_password_constants_come_from_home_assistant() -> None:
    assert "CONF_EMAIL" not in vars(integration_const)
    assert "CONF_PASSWORD" not in vars(integration_const)


def test_the_integration_is_a_hub() -> None:
    assert load_integration_json("manifest.json")["integration_type"] == "hub"


@pytest.mark.parametrize("rule", ["diagnostics", "icon-translations", "stale-devices"])
def test_the_quality_scale_marks_the_gold_rules_built_in_this_ticket(rule: str) -> None:
    scale = yaml.safe_load((INTEGRATION / "quality_scale.yaml").read_text())["rules"]
    assert scale[rule] == "done"


def test_the_manifest_names_the_integration() -> None:
    manifest = _load(INTEGRATION / "manifest.json")
    assert manifest["domain"] == "baby_tracker"
    assert manifest["name"] == "Baby Tracker (Nighp)"
    assert manifest["config_flow"] is True
    assert manifest["iot_class"] == "cloud_polling"


def test_the_manifest_requires_the_library_version_the_environment_imports() -> None:
    requirements = _load(INTEGRATION / "manifest.json")["requirements"]
    assert requirements == [f"pybabytracker=={pybabytracker.__version__}"]


def test_the_manifest_names_the_librarys_logger() -> None:
    assert _load(INTEGRATION / "manifest.json")["loggers"] == ["pybabytracker"]


def test_the_test_requirements_pin_the_same_library_version() -> None:
    lines = (ROOT / "requirements_test.txt").read_text().splitlines()
    assert f"pybabytracker=={pybabytracker.__version__}" in lines


def test_hacs_json_is_present() -> None:
    assert _load(ROOT / "hacs.json")["name"] == "Baby Tracker (Nighp)"


# Every key the Marketplace and HACS document for hacs.json (marketplace/reference/hacs-json).
HACS_JSON_KEYS = {
    "name",
    "content_in_root",
    "filename",
    "zip_release",
    "persistent_directory",
    "homeassistant",
    "hide_default_branch",
    "hacs",
    "country",
}


def test_hacs_json_uses_only_documented_keys() -> None:
    assert set(_load(ROOT / "hacs.json")) <= HACS_JSON_KEYS


def test_hacs_json_names_the_oldest_home_assistant_the_integration_loads_on() -> None:
    # services.py imports probatio, which Home Assistant ships from 2026.9.0 (2026.8.0 does not).
    # Later changes were only tested on 2026.10, so hacs.json names that release.
    assert _load(ROOT / "hacs.json")["homeassistant"] == "2026.10.0"
    assert "import probatio" in (INTEGRATION / "services.py").read_text()


def test_the_readme_names_the_same_minimum_home_assistant_as_hacs_json() -> None:
    minimum = _load(ROOT / "hacs.json")["homeassistant"]
    assert f"Home Assistant {minimum} or later" in (ROOT / "README.md").read_text()


def test_the_readme_offers_the_install_and_setup_links() -> None:
    readme = (ROOT / "README.md").read_text()
    assert (
        "https://my.home-assistant.io/redirect/hacs_repository/"
        "?owner=wtadler&repository=ha-baby-tracker&category=integration"
    ) in readme
    assert "marketplace_repository" in readme
    assert (
        "https://my.home-assistant.io/redirect/config_flow_start/?domain=baby_tracker"
    ) in readme
    assert "custom_components/baby_tracker" in readme


def test_english_translations_match_strings() -> None:
    assert _load(INTEGRATION / "translations" / "en.json") == _load(INTEGRATION / "strings.json")


def test_every_config_flow_field_has_a_description() -> None:
    strings = _load(INTEGRATION / "strings.json")
    steps = list(strings["config"]["step"].values()) + list(strings["options"]["step"].values())
    assert steps
    for step in steps:
        assert step["data"], step
        assert set(step["data_description"]) == set(step["data"])


def test_config_flow_errors_and_aborts_have_messages() -> None:
    config = _load(INTEGRATION / "strings.json")["config"]
    assert {"invalid_auth", "cannot_connect", "unknown"} <= set(config["error"])
    assert {"already_configured", "reauth_successful", "unique_id_mismatch"} <= set(config["abort"])


def test_every_sensor_has_a_translated_name() -> None:
    sensors = _load(INTEGRATION / "strings.json")["entity"]["sensor"]
    assert {key: value["name"] for key, value in sensors.items()} == {
        "diapers_today": "Diapers today",
        "feeds_today": "Feeds today",
        "bottle_volume_today": "Bottle volume today",
        "last_diaper": "Last diaper",
        "last_feed": "Last feed",
        "last_pump": "Last pump",
        "last_sleep": "Last sleep",
        "sleep_timer": "Sleep timer",
        "nursing_timer": "Nursing timer",
        "nursing_left_time": "Nursing left time",
        "nursing_right_time": "Nursing right time",
        "pump_timer": "Pump timer",
    }


def test_the_quality_scale_marks_every_bronze_rule() -> None:
    scale = yaml.safe_load((INTEGRATION / "quality_scale.yaml").read_text())["rules"]
    assert set(BRONZE_RULES) <= set(scale)
    for rule in BRONZE_RULES:
        entry = scale[rule]
        if entry == "done":
            continue
        assert isinstance(entry, dict), rule
        assert entry["status"] in ("done", "exempt", "todo"), rule
        if entry["status"] != "done":
            assert entry["comment"].strip(), rule


@pytest.mark.parametrize("rule", ["reauthentication-flow", "config-entry-unloading"])
def test_the_quality_scale_marks_the_silver_rules_built_in_this_ticket(rule: str) -> None:
    scale = yaml.safe_load((INTEGRATION / "quality_scale.yaml").read_text())["rules"]
    assert scale[rule] == "done"


def test_no_bundled_copy_of_the_library_is_left() -> None:
    assert not (INTEGRATION / "pybabytracker").exists()
    assert not (ROOT / "scripts" / "vendor_library.sh").exists()


def test_nothing_in_the_integration_or_tests_imports_the_library_from_inside_the_integration() -> (
    None
):
    pattern = re.compile(r"from \.pybabytracker|baby_tracker\.pybabytracker")
    offenders = [
        str(path.relative_to(ROOT))
        for folder in (INTEGRATION, ROOT / "tests")
        for path in folder.rglob("*.py")
        if pattern.search(path.read_text())
        # This file writes the pattern down.
        and path.name != "test_metadata.py"
    ]
    assert offenders == []


def test_lint_and_type_rules_no_longer_leave_a_bundled_copy_out() -> None:
    pyproject = (ROOT / "pyproject.toml").read_text()
    assert "pybabytracker" not in pyproject.replace("tests/", "")
    assert "vendored" not in pyproject.lower()


def test_the_readme_does_not_describe_a_bundled_library() -> None:
    readme = (ROOT / "README.md").read_text().lower()
    assert "vendor" not in readme
    assert "bundled" not in readme


def test_the_readme_covers_install_and_removal() -> None:
    readme = (ROOT / "README.md").read_text().lower()
    for heading in ("installation", "removal", "configuration"):
        assert heading in readme


@pytest.mark.parametrize("rule", ["docs-triggers", "docs-conditions"])
def test_trigger_and_condition_docs_are_exempt_with_a_reason(rule: str) -> None:
    scale = yaml.safe_load((INTEGRATION / "quality_scale.yaml").read_text())["rules"]
    assert scale[rule]["status"] == "exempt"
    assert scale[rule]["comment"].strip()


def test_claude_worktrees_are_gitignored() -> None:
    assert ".claude/" in (ROOT / ".gitignore").read_text().splitlines()


# --- the pump timer ------------------------------------------------------------------------------


def test_the_pump_actions_take_the_account_and_stop_pump_takes_an_amount_unit_and_note() -> None:
    services = _yaml()

    assert set(services["start_pump"]["fields"]) == {"config_entry_id"}
    assert set(services["stop_pump"]["fields"]) == {"config_entry_id", "amount", "unit", "note"}
    for action in ("start_pump", "stop_pump"):
        field = services[action]["fields"]["config_entry_id"]
        assert field["required"] is True
        assert field["selector"] == {"config_entry": {"integration": DOMAIN}}
    assert services["stop_pump"]["fields"]["amount"]["required"] is False
    assert services["stop_pump"]["fields"]["unit"]["selector"]["select"]["options"] == [
        "fl. oz.",
        "mL",
    ]
    assert services["log_pump"]["fields"]["amount"]["required"] is False
    assert services["log_pump"]["fields"]["minutes"]["required"] is False


def test_the_pump_timer_errors_repair_entities_and_icons_are_translated() -> None:
    strings = _strings()
    assert {
        "pump_already_running",
        "no_pump_timer",
        "pump_not_saved",
        "amount_with_sides",
        "minutes_with_sides",
    } <= set(strings["exceptions"])
    repair = strings["issues"]["pump_timer_long"]
    assert repair["title"]
    assert "Pump timer switch" in repair["description"]
    assert "Stop pump" in repair["description"]
    assert "Undo" in repair["description"]  # a forgotten timer's record can be deleted again
    assert strings["entity"]["switch"]["pump"]["name"] == "Pump timer"
    assert strings["entity"]["sensor"]["pump_timer"]["name"] == "Pump timer"
    icons = _icons()["entity"]
    assert icons["switch"]["pump"]["state"]["on"] != icons["switch"]["pump"]["state"]["off"]
    assert icons["sensor"]["pump_timer"]["default"].startswith("mdi:")
