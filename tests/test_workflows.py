"""HACS only: the weekly workflow that runs the suite against the latest Home Assistant.

It reads the workflow files, so it checks the parts that keep the job honest: the triggers, an
upgrade that runs after the pins install (so the pins can't win), the printed versions, the suite
run, a failure step that reports to an issue without spamming one, and that regular CI stays
pinned.
"""

from pathlib import Path
from typing import Any

import yaml

from .helpers import ROOT

WORKFLOWS = ROOT / ".github" / "workflows"
LATEST = WORKFLOWS / "latest-ha.yml"
CI = WORKFLOWS / "ci.yml"


def load(path: Path) -> dict[str, Any]:
    workflow: dict[str, Any] = yaml.safe_load(path.read_text())
    return workflow


def triggers(workflow: dict[str, Any]) -> dict[str, Any]:
    # PyYAML reads the bare key `on` as the boolean True (YAML 1.1), so accept either.
    found: dict[str, Any] = workflow.get("on", workflow.get(True)) or {}
    return found


def steps(workflow: dict[str, Any]) -> list[dict[str, Any]]:
    job: dict[str, Any] = next(iter(workflow["jobs"].values()))
    listed: list[dict[str, Any]] = job["steps"]
    return listed


def index_of(all_steps: list[dict[str, Any]], needle: str) -> int:
    for position, step in enumerate(all_steps):
        if needle in str(step.get("run", "")):
            return position
    raise AssertionError(f"no step runs {needle!r}")


def test_latest_ha_workflow_exists() -> None:
    assert LATEST.is_file()


def test_latest_ha_runs_weekly_and_can_be_started_by_hand() -> None:
    on = triggers(load(LATEST))

    assert "workflow_dispatch" in on
    cron = on["schedule"][0]["cron"]
    assert len(cron.split()) == 5


def test_latest_ha_upgrades_both_packages_after_the_pinned_install() -> None:
    all_steps = steps(load(LATEST))

    pinned = index_of(all_steps, "pip install -r requirements_test.txt")
    upgrade = index_of(all_steps, "pip install --upgrade")
    upgrade_run = all_steps[upgrade]["run"]

    assert upgrade > pinned
    assert "homeassistant" in upgrade_run
    assert "pytest-homeassistant-custom-component" in upgrade_run


def test_latest_ha_prints_both_installed_versions_before_the_suite() -> None:
    all_steps = steps(load(LATEST))

    versions = index_of(all_steps, "pip show")
    suite = next(
        position for position, step in enumerate(all_steps) if step.get("name") == "pytest"
    )

    assert "homeassistant" in all_steps[versions]["run"]
    assert "pytest-homeassistant-custom-component" in all_steps[versions]["run"]
    assert versions < suite


def test_latest_ha_runs_the_same_suite_command_as_ci() -> None:
    all_steps = steps(load(LATEST))
    ci_steps = steps(load(CI))

    ci_suite = next(step["run"] for step in ci_steps if step.get("name") == "pytest")
    latest_suite = next(step["run"] for step in all_steps if step.get("name") == "pytest")

    assert latest_suite == ci_suite


def test_latest_ha_failure_step_opens_or_comments_on_one_issue() -> None:
    workflow = load(LATEST)
    all_steps = steps(workflow)

    failure = next(step for step in all_steps if "gh issue" in str(step.get("run", "")))
    run = failure["run"]

    assert "failure()" in failure["if"]
    assert "github.event_name == 'schedule'" in failure["if"]
    assert "gh issue list" in run
    assert "gh issue comment" in run
    assert "gh issue create" in run
    assert "needs-triage" in run
    assert failure["env"]["GH_TOKEN"] == "${{ secrets.GITHUB_TOKEN }}"


def test_latest_ha_grants_issues_write_only_to_the_job() -> None:
    workflow = load(LATEST)
    job = next(iter(workflow["jobs"].values()))

    assert workflow.get("permissions") == {"contents": "read"}
    assert job["permissions"] == {"contents": "read", "issues": "write"}


def test_regular_ci_stays_pinned() -> None:
    ci_text = CI.read_text()

    assert "pip install -r requirements_test.txt" in ci_text
    assert "--upgrade" not in ci_text
