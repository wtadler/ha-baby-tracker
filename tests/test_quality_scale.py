"""HACS only: quality_scale.yaml lists exactly the current rules, and the manifest claims only
what holds. Core checks this with hassfest."""

import importlib
import json
import pkgutil
import re
from typing import Any

import pytest
import yaml

from custom_components.baby_tracker import pybabytracker

from .helpers import INTEGRATION

# A copy of the tier list, so a change to Home Assistant's rules shows up as a deliberate edit here.
# Source: home-assistant/developers.home-assistant,
# docs/core/integration-quality-scale/_includes/tiers.json, at master commit
# 50b518898989b67bb76b69827f227aa6f0acd32a (read October 26, 2026). Subchecks are dropped: a rule
# is its ID.
PINNED_TIERS: dict[str, list[str]] = {
    "bronze": [
        "action-setup",
        "appropriate-polling",
        "brands",
        "common-modules",
        "config-flow-test-coverage",
        "config-flow",
        "dependency-transparency",
        "docs-actions",
        "docs-triggers",
        "docs-conditions",
        "docs-high-level-description",
        "docs-installation-instructions",
        "docs-removal-instructions",
        "entity-event-setup",
        "entity-unique-id",
        "has-entity-name",
        "runtime-data",
        "test-before-configure",
        "test-before-setup",
        "unique-config-entry",
    ],
    "silver": [
        "action-exceptions",
        "config-entry-unloading",
        "docs-configuration-parameters",
        "docs-installation-parameters",
        "entity-unavailable",
        "integration-owner",
        "log-when-unavailable",
        "parallel-updates",
        "reauthentication-flow",
        "test-coverage",
    ],
    "gold": [
        "devices",
        "diagnostics",
        "discovery-update-info",
        "discovery",
        "docs-data-update",
        "docs-examples",
        "docs-known-limitations",
        "docs-supported-devices",
        "docs-supported-functions",
        "docs-troubleshooting",
        "docs-use-cases",
        "dynamic-devices",
        "entity-category",
        "entity-device-class",
        "entity-disabled-by-default",
        "entity-translations",
        "exception-translations",
        "icon-translations",
        "reconfiguration-flow",
        "repair-issues",
        "stale-devices",
    ],
    "platinum": [
        "async-dependency",
        "inject-websession",
        "strict-typing",
    ],
}

# The keys of "integration_category" in homeassistant/components/homeassistant/strings.json, at
# home-assistant/core dev commit cf39cff80dd063e34c834d40e666708bb85bf116 (October 26, 2026).
CORE_CATEGORIES = {
    "ai_voice",
    "appliances_cleaning",
    "blinds_curtains",
    "buttons_remotes",
    "climate",
    "computers_network",
    "diy_makers",
    "energy",
    "finance",
    "garden_plants",
    "health_wellbeing",
    "helpers",
    "hubs_protocols",
    "lighting",
    "local_services",
    "media_entertainment",
    "messaging_notifications",
    "news_social_media",
    "other_ecosystems",
    "pets",
    "plugs_switches",
    "presence_location",
    "productivity_information",
    "security_safety",
    "sensors",
    "vehicles_transport",
    "water",
    "weather_alerts",
}

STATUSES = {"done", "todo", "exempt"}
TIER_ORDER = ["bronze", "silver", "gold", "platinum"]


def _rules() -> dict[str, Any]:
    rules = yaml.safe_load((INTEGRATION / "quality_scale.yaml").read_text())["rules"]
    assert isinstance(rules, dict)
    return rules


def _status(entry: str | dict[str, Any]) -> str:
    return entry["status"] if isinstance(entry, dict) else entry


def _manifest() -> dict[str, Any]:
    manifest = json.loads((INTEGRATION / "manifest.json").read_text())
    assert isinstance(manifest, dict)
    return manifest


def _highest_tier_met() -> str | None:
    """Return the highest tier whose rules, and every lower tier's, are done or exempt."""
    met = None
    for tier in TIER_ORDER:
        if all(_status(_rules()[rule]) in {"done", "exempt"} for rule in PINNED_TIERS[tier]):
            met = tier
        else:
            break
    return met


def test_the_pinned_tiers_hold_54_distinct_rules() -> None:
    every = [rule for tier in PINNED_TIERS.values() for rule in tier]
    assert len(every) == 54
    assert len(set(every)) == 54


def test_the_file_lists_exactly_the_pinned_rules() -> None:
    expected = {rule for tier in PINNED_TIERS.values() for rule in tier}
    assert set(_rules()) == expected


@pytest.mark.parametrize("tier", TIER_ORDER)
def test_the_file_groups_its_rules_by_tier_in_order(tier: str) -> None:
    listed = list(_rules())
    start = sum(len(PINNED_TIERS[earlier]) for earlier in TIER_ORDER[: TIER_ORDER.index(tier)])
    assert sorted(listed[start : start + len(PINNED_TIERS[tier])]) == sorted(PINNED_TIERS[tier])


def test_every_rule_has_a_known_status() -> None:
    for rule, entry in _rules().items():
        assert _status(entry) in STATUSES, rule


def test_every_todo_and_exempt_rule_says_why() -> None:
    for rule, entry in _rules().items():
        if _status(entry) == "done":
            continue
        assert isinstance(entry, dict), rule
        assert set(entry) == {"status", "comment"}, rule
        assert entry["comment"].strip().endswith("."), rule


def test_a_done_rule_carries_no_stray_keys() -> None:
    for rule, entry in _rules().items():
        if isinstance(entry, dict):
            assert set(entry) == {"status", "comment"}, rule


@pytest.mark.parametrize("rule", ["brands", "dependency-transparency"])
def test_the_rules_the_ticket_names_are_never_exempt(rule: str) -> None:
    assert _status(_rules()[rule]) != "exempt"


def test_dependency_transparency_stays_todo_while_the_library_is_bundled() -> None:
    assert _manifest()["requirements"] == []
    assert _status(_rules()["dependency-transparency"]) == "todo"


def test_the_nursing_time_sensors_timer_makes_entity_event_setup_done_not_exempt() -> None:
    assert _status(_rules()["entity-event-setup"]) == "done"


def test_exempt_rules_are_ones_the_rule_text_allows() -> None:
    exempt = {rule for rule, entry in _rules().items() if _status(entry) == "exempt"}
    # Each of these has an exception in its rule text that this integration meets: no triggers or
    # conditions, a cloud account with nothing to discover and no
    # physical device, only primary entities, and credentials-only configuration.
    assert exempt == {
        "docs-conditions",
        "docs-triggers",
        "discovery",
        "discovery-update-info",
        "docs-supported-devices",
        "entity-category",
        "reconfiguration-flow",
    }


# --- the manifest --------------------------------------------------------------------------------


def test_the_manifest_leaves_out_categories_until_the_published_hassfest_accepts_them() -> None:
    # The hassfest image the CI action runs (ghcr.io/home-assistant/hassfest) still rejects the
    # key: "not a valid option at 'categories'", seen on pull request 55. Core's dev branch accepts
    # it, and requires it for a core integration, so CONTRIBUTING.md lists it for core submission.
    # Add it when the 2026.11 hassfest image ships (the first release cut after core's change).
    assert "categories" not in _manifest()


def test_the_category_contributing_names_for_core_submission_is_one_core_lists() -> None:
    from .helpers import ROOT

    section = (ROOT / "CONTRIBUTING.md").read_text().split("## Core submission", 1)[1]
    section = section.split("\n## ", 1)[0]
    named = re.search(r"`categories` is added as `\[\"(\w+)\"\]`", section)
    assert named is not None
    assert named.group(1) in CORE_CATEGORIES


def test_the_manifest_names_the_bundled_librarys_loggers() -> None:
    loggers = _manifest()["loggers"]
    assert loggers == ["custom_components.baby_tracker.pybabytracker"]
    for module in pkgutil.iter_modules(pybabytracker.__path__):
        name = f"{pybabytracker.__name__}.{module.name}"
        if module.name in {"__main__", "cli"}:
            continue
        logger = getattr(importlib.import_module(name), "_LOGGER", None)
        if logger is not None:
            assert any(logger.name.startswith(prefix) for prefix in loggers), name


def test_the_manifest_declares_no_tier_a_rule_denies() -> None:
    declared = _manifest().get("quality_scale")
    if declared is None:
        return
    assert declared in TIER_ORDER
    met = _highest_tier_met()
    assert met is not None
    assert TIER_ORDER.index(declared) <= TIER_ORDER.index(met)


def test_bronze_does_not_hold_while_the_library_is_bundled_so_no_tier_is_declared() -> None:
    assert _highest_tier_met() is None
    assert "quality_scale" not in _manifest()


def test_the_custom_only_keys_stay_for_the_repository() -> None:
    manifest = _manifest()
    assert manifest["documentation"].startswith("https://github.com/wtadler/ha-baby-tracker")
    assert manifest["issue_tracker"] == "https://github.com/wtadler/ha-baby-tracker/issues"
    assert manifest["version"]


def test_contributing_lists_what_changes_in_the_manifest_at_core_submission() -> None:
    from .helpers import ROOT

    readme = (ROOT / "CONTRIBUTING.md").read_text()
    section = readme.split("## Core submission", 1)[1].split("\n## ", 1)[0]
    for key in (
        "documentation",
        "issue_tracker",
        "version",
        "categories",
        "quality_scale",
        "loggers",
    ):
        assert f"`{key}`" in section, key


# --- the gold documentation rules and the entity default ----------------------------------------

DOCS_RULES = {
    "docs-known-limitations": "## Known limitations",
    "docs-troubleshooting": "## Troubleshooting",
    "docs-use-cases": "## Use cases",
}


@pytest.mark.parametrize(("rule", "heading"), DOCS_RULES.items())
def test_a_docs_rule_is_done_only_with_its_section_in_the_readme(rule: str, heading: str) -> None:
    from .helpers import ROOT

    headings = (ROOT / "README.md").read_text().splitlines()
    assert heading in headings
    assert _status(_rules()[rule]) == "done"


def test_no_entity_starts_disabled_so_the_default_rule_is_done_with_its_reason() -> None:
    entry = _rules()["entity-disabled-by-default"]
    assert isinstance(entry, dict)
    assert entry["status"] == "done"
    assert entry["comment"].strip().endswith(".")
    for path in (INTEGRATION).glob("*.py"):
        assert "entity_registry_enabled_default" not in path.read_text(), path.name
