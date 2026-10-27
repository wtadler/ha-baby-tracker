"""Manifest, translations, quality scale, and the vendored library's packaging."""

import json
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from .helpers import INTEGRATION, ROOT, VENDORED

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


def test_the_manifest_names_the_integration() -> None:
    manifest = _load(INTEGRATION / "manifest.json")
    assert manifest["domain"] == "nighp_baby_tracker"
    assert manifest["name"] == "Baby Tracker (Nighp)"
    assert manifest["config_flow"] is True
    assert manifest["iot_class"] == "cloud_polling"


def test_the_manifest_requires_nothing_because_the_library_is_bundled() -> None:
    assert _load(INTEGRATION / "manifest.json")["requirements"] == []


def test_hacs_json_is_present() -> None:
    assert _load(ROOT / "hacs.json")["name"] == "Baby Tracker (Nighp)"


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


def test_the_vendored_copy_records_its_source_commit() -> None:
    commit = (VENDORED / "VENDORED_FROM").read_text().strip()
    assert re.fullmatch(r"[0-9a-f]{40}", commit)


def test_the_vendored_library_only_imports_itself_relatively() -> None:
    offenders = [
        path.name
        for path in VENDORED.glob("*.py")
        if re.search(r"^\s*(from|import)\s+pybabytracker\b", path.read_text(), re.MULTILINE)
    ]
    assert offenders == []


def test_lint_rules_leave_the_vendored_copy_out() -> None:
    pyproject = (ROOT / "pyproject.toml").read_text()
    assert "custom_components/nighp_baby_tracker/pybabytracker" in pyproject


def test_the_readme_covers_install_and_removal() -> None:
    readme = (ROOT / "README.md").read_text().lower()
    for heading in ("installation", "removal", "configuration"):
        assert heading in readme


def test_dependency_transparency_is_a_todo_until_the_library_is_published() -> None:
    scale = yaml.safe_load((INTEGRATION / "quality_scale.yaml").read_text())["rules"]
    entry = scale["dependency-transparency"]
    assert entry["status"] == "todo"
    assert entry["comment"] == "library bundled while private; PyPI at go-public (#12)"


@pytest.mark.parametrize("rule", ["docs-triggers", "docs-conditions"])
def test_trigger_and_condition_docs_are_exempt_with_a_reason(rule: str) -> None:
    scale = yaml.safe_load((INTEGRATION / "quality_scale.yaml").read_text())["rules"]
    assert scale[rule]["status"] == "exempt"
    assert scale[rule]["comment"].strip()


def test_claude_worktrees_are_gitignored() -> None:
    assert ".claude/" in (ROOT / ".gitignore").read_text().splitlines()
