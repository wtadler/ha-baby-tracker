"""HACS only: scripts/core_cut.py, which writes a planned core PR's slice into a core checkout."""

import ast
import importlib.util
import json
import shutil
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "core_cut.py"
INTEGRATION = ROOT / "custom_components" / "nighp_baby_tracker"
CORE_PREP = ROOT / "core-prep"
COMPONENT = Path("homeassistant/components/nighp_baby_tracker")
TESTS = Path("tests/components/nighp_baby_tracker")

PR1_INTEGRATION = {
    "__init__.py",
    "config_flow.py",
    "const.py",
    "coordinator.py",
    "entity.py",
    "icons.json",
    "manifest.json",
    "quality_scale.yaml",
    "sensor.py",
    "strings.json",
}
PR1_TESTS = {
    "__init__.py",
    "conftest.py",
    "factories.py",
    "fake_server.py",
    "fixtures/baby.json",
    "fixtures/record.json",
    "fixtures/volume.json",
    "fixtures/wire.json",
    "snapshots/test_sensor.ambr",
    "test_config_flow.py",
    "test_init.py",
    "test_sensor.py",
    "test_sensor_last_activity.py",
}


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("core_cut", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["core_cut"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def core_cut() -> ModuleType:
    return _load()


def _tree(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): path.read_text()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _files(root: Path) -> set[str]:
    return {str(path.relative_to(root)) for path in root.rglob("*") if path.is_file()}


# Imports


def test_imports_of_the_integration_point_at_core(core_cut: ModuleType) -> None:
    source = (
        "from custom_components.nighp_baby_tracker.const import DOMAIN\n"
        'patch("custom_components.nighp_baby_tracker.coordinator.SCAN_INTERVAL")\n'
    )
    assert core_cut.rewrite_imports(source) == (
        "from homeassistant.components.nighp_baby_tracker.const import DOMAIN\n"
        'patch("homeassistant.components.nighp_baby_tracker.coordinator.SCAN_INTERVAL")\n'
    )


@pytest.mark.parametrize(
    "source",
    [
        "from .pybabytracker import Record\n",
        "from custom_components.nighp_baby_tracker.pybabytracker import Record\n",
    ],
    ids=["relative", "absolute"],
)
def test_imports_of_the_vendored_library_point_at_the_package(
    core_cut: ModuleType, source: str
) -> None:
    assert core_cut.rewrite_imports(source) == "from pybabytracker import Record\n"


def test_test_helpers_come_from_cores_test_package(core_cut: ModuleType) -> None:
    source = (
        "from pytest_homeassistant_custom_component.common import MockConfigEntry\n"
        "from pytest_homeassistant_custom_component.typing import ClientSessionGenerator\n"
    )
    assert core_cut.rewrite_imports(source) == (
        "from pytest_homeassistant.common import MockConfigEntry\n"
        "from pytest_homeassistant.typing import ClientSessionGenerator\n"
    )


# Markers


def test_a_marked_statement_waits_for_its_pr(core_cut: ModuleType) -> None:
    source = 'A = 1\n\n# Core PR 5\ndef later() -> None:\n    """Later."""\n\n\nB = 2\n'
    assert "def later" not in core_cut.cut_python(source, 4)
    assert (
        core_cut.cut_python(source, 5)
        == 'A = 1\n\ndef later() -> None:\n    """Later."""\n\n\nB = 2\n'
    )


def test_the_comments_above_a_dropped_statement_go_with_it(core_cut: ModuleType) -> None:
    source = "A = 1\n# Why later is needed.\n# Core PR 5\nLATER = 2\nB = 3\n"
    assert core_cut.cut_python(source, 1) == "A = 1\nB = 3\n"
    assert core_cut.cut_python(source, 5) == "A = 1\n# Why later is needed.\nLATER = 2\nB = 3\n"


def test_a_marked_line_inside_a_statement_waits_for_its_pr(core_cut: ModuleType) -> None:
    source = "VALUES = (\n    1,\n    2,  # Core PR 3\n)\n"
    assert core_cut.cut_python(source, 2) == "VALUES = (\n    1,\n)\n"
    assert core_cut.cut_python(source, 3) == "VALUES = (\n    1,\n    2,\n)\n"


def test_a_marker_on_the_first_line_of_a_statement_drops_all_of_it(
    core_cut: ModuleType,
) -> None:
    source = "def f() -> None:\n    call(  # Core PR 5\n        1,\n    )\n    other()\n"
    assert core_cut.cut_python(source, 1) == "def f() -> None:\n    other()\n"


def test_a_decorated_statement_drops_with_its_decorators(core_cut: ModuleType) -> None:
    source = (
        "class A:\n    # Core PR 7\n    @staticmethod\n    def f() -> None:\n        pass\n\n"
        "    x = 1\n"
    )
    assert core_cut.cut_python(source, 1) == "class A:\n\n    x = 1\n"


@pytest.mark.parametrize(
    "source",
    [
        "# HACS-only: the old cleanup\nOLD = 1\nNEW = 2\n",
        "# HACS only\nOLD = 1\nNEW = 2\n",
        "OLD = 1  # HACS-only: the old cleanup\nNEW = 2\n",
        'def old() -> None:\n    """HACS-only: clean up.\n\n    More.\n    """\n\n\nNEW = 2\n',
        "@pytest.mark.engine\nasync def test_old() -> None:\n    pass\n\n\nNEW = 2\n",
    ],
    ids=["comment", "comment-with-space", "trailing", "docstring", "engine-test"],
)
def test_hacs_only_code_never_reaches_core(core_cut: ModuleType, source: str) -> None:
    out = core_cut.cut_python(source, 22)
    assert "OLD" not in out
    assert "old" not in out
    assert out.strip().endswith("NEW = 2")


def test_hacs_only_further_down_a_docstring_marks_nothing(core_cut: ModuleType) -> None:
    source = 'def kept() -> None:\n    """Do it.\n\n    HACS-only: one detail of it.\n    """\n'
    assert core_cut.cut_python(source, 1) == source


CLAUSES = {
    "elif": "if a:\n    x = 1\nelif b:{m}\n    x = 2\nelse:\n    x = 3\n",
    "else": "if a:\n    x = 1\nelse:{m}\n    x = 3\n",
    "except": "try:\n    x = 1\nexcept ValueError:{m}\n    x = 2\n",
    "finally": "try:\n    x = 1\nfinally:{m}\n    x = 2\n",
    "case": "match a:\n    case 1:{m}\n        x = 1\n    case _:\n        x = 2\n",
}


@pytest.mark.parametrize("clause", list(CLAUSES))
def test_a_trailing_marker_on_a_clause_is_an_error(core_cut: ModuleType, clause: str) -> None:
    source = CLAUSES[clause].format(m="  # Core PR 7")
    with pytest.raises(ValueError, match=f"line .*: a marker can't mark the {clause} clause"):
        core_cut.cut_python(source, 1)


@pytest.mark.parametrize("clause", ["elif", "case"])
def test_a_marker_line_above_a_clause_is_an_error(core_cut: ModuleType, clause: str) -> None:
    source = CLAUSES[clause].format(m="")
    lines = source.splitlines(keepends=True)
    index = next(i for i, line in enumerate(lines) if line.lstrip().startswith(clause))
    indent = lines[index][: len(lines[index]) - len(lines[index].lstrip())]
    lines.insert(index, f"{indent}# Core PR 7\n")
    with pytest.raises(ValueError, match=f"a marker can't mark the {clause} clause"):
        core_cut.cut_python("".join(lines), 1)


def test_a_marker_with_nothing_under_it_is_an_error(core_cut: ModuleType) -> None:
    with pytest.raises(ValueError, match="line 2"):
        core_cut.cut_python("A = 1\n# Core PR 4\n\nB = 2\n", 1)


def test_a_cut_that_does_not_parse_is_an_error(core_cut: ModuleType) -> None:
    with pytest.raises(ValueError, match="parse"):
        core_cut.cut_python("if True:\n    a = 1  # Core PR 4\n", 1)


def test_a_core_line_replaces_a_hacs_line(core_cut: ModuleType) -> None:
    source = (
        "async def setup(\n"
        "    hass: object,\n"
        ") -> bool:  # Core: ) -> None:\n"
        "    await go(hass)\n"
        "    return True  # HACS-only: core returns None\n"
    )
    assert core_cut.cut_python(source, 1) == (
        "async def setup(\n    hass: object,\n) -> None:\n    await go(hass)\n"
    )


def test_a_core_comment_line_becomes_code(core_cut: ModuleType) -> None:
    source = (
        "async def unload() -> bool:  # Core: async def unload() -> None:\n"
        '    """Unload."""\n'
        "    return await go()  # HACS-only: core raises instead\n"
        "    # Core: await go_or_raise()\n"
    )
    assert core_cut.cut_python(source, 1) == (
        'async def unload() -> None:\n    """Unload."""\n    await go_or_raise()\n'
    )


def test_a_core_line_stays_a_comment_in_the_repository(core_cut: ModuleType) -> None:
    source = "def f() -> None:\n    # Core: core_only()\n    shared()  # Core PR 3\n"
    assert core_cut.cut_python(source, 2) == "def f() -> None:\n    core_only()\n"


def test_a_test_module_docstring_drops_its_pr_label(core_cut: ModuleType) -> None:
    source = '"""Core PR 1: the config flow\'s user step."""\n\nA = 1\n'
    assert (
        core_cut.cut_python(source, 1) == '"""Tests for the config flow\'s user step."""\n\nA = 1\n'
    )


# Generated files


def test_the_manifest_is_reshaped_for_core(core_cut: ModuleType) -> None:
    manifest = core_cut.core_manifest(1)
    assert list(manifest)[:2] == ["domain", "name"]
    assert list(manifest)[2:] == sorted(list(manifest)[2:])
    assert "version" not in manifest
    assert "issue_tracker" not in manifest
    assert manifest["documentation"] == (
        "https://www.home-assistant.io/integrations/nighp_baby_tracker"
    )
    assert manifest["loggers"] == ["pybabytracker"]
    assert manifest["quality_scale"] == "bronze"
    assert manifest["categories"] == ["health_wellbeing"]
    vendored = (INTEGRATION / "pybabytracker" / "_version.py").read_text()
    version = vendored.split('"')[1]
    assert manifest["requirements"] == [f"pybabytracker=={version}"]
    hacs = json.loads((INTEGRATION / "manifest.json").read_text())
    for key in ("domain", "name", "codeowners", "config_flow", "integration_type", "iot_class"):
        assert manifest[key] == hacs[key]


def test_pr1_strings_are_its_keys_only(core_cut: ModuleType) -> None:
    strings = core_cut.core_strings(1)
    assert set(strings) == {"config", "device", "entity", "exceptions"}
    assert set(strings["config"]["step"]) == {"user"}
    assert set(strings["config"]["abort"]) == {"already_configured"}
    assert set(strings["device"]) == {"baby"}
    assert set(strings["entity"]) == {"sensor"}
    assert set(strings["entity"]["sensor"]) == {"last_diaper", "last_feed", "last_sleep"}
    assert set(strings["exceptions"]) == {"auth_failed", "sync_failed_update"}


def test_pr1_strings_use_core_references(core_cut: ModuleType) -> None:
    config = core_cut.core_strings(1)["config"]
    assert config["error"]["invalid_auth"] == "[%key:common::config_flow::error::invalid_auth%]"
    assert config["step"]["user"]["data"]["email"] == "[%key:common::config_flow::data::email%]"
    assert config["abort"]["already_configured"] == (
        "[%key:common::config_flow::abort::already_configured_account%]"
    )


@pytest.mark.parametrize("table", ["references", "overrides"])
def test_a_planned_string_that_is_missing_is_an_error(
    core_cut: ModuleType, monkeypatch: pytest.MonkeyPatch, table: str
) -> None:
    value = {"until": 2, "text": "x"} if table == "overrides" else "[%key:x%]"
    monkeypatch.setitem(core_cut.PLAN, table, {"config.error.renamed_away": value})
    with pytest.raises(KeyError, match="config.error.renamed_away"):
        core_cut.core_strings(1)


def test_auth_failed_does_not_promise_reauthentication_before_pr2(core_cut: ModuleType) -> None:
    hacs = json.loads((INTEGRATION / "strings.json").read_text())
    before = core_cut.core_strings(1)["exceptions"]["auth_failed"]["message"]
    after = core_cut.core_strings(2)["exceptions"]["auth_failed"]["message"]
    assert "settings" not in before
    assert before != hacs["exceptions"]["auth_failed"]["message"]
    assert after == hacs["exceptions"]["auth_failed"]["message"]


def test_pr1_icons_are_the_last_activity_sensors(core_cut: ModuleType) -> None:
    icons = core_cut.core_icons(1)
    assert icons == {
        "entity": {
            "sensor": {
                key: json.loads((INTEGRATION / "icons.json").read_text())["entity"]["sensor"][key]
                for key in ("last_diaper", "last_feed", "last_sleep")
            }
        }
    }


def test_pr1_has_no_services_yaml_and_pr9_has_only_log_diaper(core_cut: ModuleType) -> None:
    assert core_cut.core_services(1) is None
    services = core_cut.core_services(9)
    assert list(yaml.safe_load(services)) == ["log_diaper"]
    first = (INTEGRATION / "services.yaml").read_text().split("\nlog_bottle:")[0]
    assert services == first.rstrip("\n") + "\n"


def _sorted_throughout(data: Any) -> bool:
    if not isinstance(data, dict):
        return True
    return list(data) == sorted(data) and all(_sorted_throughout(v) for v in data.values())


@pytest.mark.parametrize("number", [1, 9, 22])
def test_strings_and_icons_are_sorted_as_cores_formatter_sorts_them(
    core_cut: ModuleType, number: int
) -> None:
    assert _sorted_throughout(core_cut.core_strings(number))
    assert _sorted_throughout(core_cut.core_icons(number))


# The plan covers everything


def _leaves(data: Any, prefix: str = "") -> list[str]:
    if not isinstance(data, dict):
        return [prefix]
    return [leaf for key, value in data.items() for leaf in _leaves(value, f"{prefix}{key}.")]


@pytest.mark.parametrize("name", ["strings.json", "icons.json"])
def test_every_key_belongs_to_a_planned_pr(core_cut: ModuleType, name: str) -> None:
    data = json.loads((INTEGRATION / name).read_text())
    for leaf in _leaves(data):
        owner = core_cut.key_owner(leaf.rstrip("."))
        assert owner == "hacs" or 1 <= owner <= 22, leaf


def test_every_action_belongs_to_a_planned_pr(core_cut: ModuleType) -> None:
    services = yaml.safe_load((INTEGRATION / "services.yaml").read_text())
    for action in services:
        owner = core_cut.key_owner(f"services.{action}")
        assert owner == "hacs" or 1 <= owner <= 22, action


def test_every_entry_in_the_plan_names_a_real_key(core_cut: ModuleType) -> None:
    plan = yaml.safe_load((CORE_PREP / "plan.yaml").read_text())
    strings = json.loads((INTEGRATION / "strings.json").read_text())
    services = yaml.safe_load((INTEGRATION / "services.yaml").read_text())
    known = {leaf.rstrip(".") for leaf in _leaves(strings)}
    known |= {f"services.{action}" for action in services}
    for entry in plan["keys"]:
        assert any(key == entry or key.startswith(f"{entry}.") for key in known), entry


def test_an_unplanned_key_is_an_error(core_cut: ModuleType) -> None:
    with pytest.raises(KeyError, match="entity.sensor.unplanned"):
        core_cut.key_owner("entity.sensor.unplanned.name")


def test_every_file_belongs_to_a_planned_pr(core_cut: ModuleType) -> None:
    for path in INTEGRATION.iterdir():
        if path.name == "__pycache__" or path.name in core_cut.GENERATED:
            continue
        owner = core_cut.integration_file_owner(path.name)
        assert owner == "hacs" or 1 <= owner <= 22, path.name
    for path in (ROOT / "tests").rglob("*"):
        if path.is_dir() or "__pycache__" in path.parts:
            continue
        owner = core_cut.test_file_owner(path.relative_to(ROOT / "tests"))
        assert owner == "hacs" or 1 <= owner <= 22, path


# The quality scale


def test_each_pr_has_an_up_to_date_quality_scale(core_cut: ModuleType) -> None:
    written = sorted(path.name for path in (CORE_PREP / "quality_scale").iterdir())
    assert written == [f"pr{number:02d}.yaml" for number in range(1, 23)]
    for number in range(1, 23):
        expected = core_cut.quality_scale_yaml(number)
        assert (CORE_PREP / "quality_scale" / f"pr{number:02d}.yaml").read_text() == expected


def test_each_quality_scale_lists_the_same_rules_as_the_repository(core_cut: ModuleType) -> None:
    hacs = yaml.safe_load((INTEGRATION / "quality_scale.yaml").read_text())["rules"]
    for number in range(1, 23):
        rules = yaml.safe_load(core_cut.quality_scale_yaml(number))["rules"]
        assert set(rules) == set(hacs), number


def test_pr1_meets_bronze_and_defers_what_the_first_pr_may_not_have(
    core_cut: ModuleType,
) -> None:
    rules = yaml.safe_load(core_cut.quality_scale_yaml(1))["rules"]

    def status(rule: str) -> str:
        value = rules[rule]
        return value if isinstance(value, str) else value["status"]

    plan = yaml.safe_load((CORE_PREP / "plan.yaml").read_text())
    assert all(status(rule) in ("done", "exempt") for rule in plan["quality_scale"]["bronze"])
    for rule in ("reauthentication-flow", "diagnostics", "dynamic-devices", "stale-devices"):
        assert status(rule) == "todo"
    assert status("strict-typing") == "done"


def test_the_last_pr_reaches_the_repositorys_statuses(core_cut: ModuleType) -> None:
    hacs = yaml.safe_load((INTEGRATION / "quality_scale.yaml").read_text())["rules"]
    rules = yaml.safe_load(core_cut.quality_scale_yaml(22))["rules"]

    def status(value: Any) -> str:
        return value if isinstance(value, str) else value["status"]

    differ = {rule for rule in hacs if status(hacs[rule]) != status(rules[rule])}
    # Both wait on the library being a typed PyPI package (#12), which core requires.
    assert differ == {"dependency-transparency", "strict-typing"}


# The cut


@pytest.fixture
def core(tmp_path: Path) -> Path:
    root = tmp_path / "core"
    (root / "homeassistant" / "components").mkdir(parents=True)
    (root / "tests" / "components").mkdir(parents=True)
    (root / ".strict-typing").write_text(
        "homeassistant.components.a_first.*\nhomeassistant.components.zz_last.*\n"
    )
    return root


def test_pr1_writes_exactly_its_files(core_cut: ModuleType, core: Path) -> None:
    core_cut.cut(1, core, ruff=None)
    assert _files(core / COMPONENT) == PR1_INTEGRATION
    assert _files(core / TESTS) == PR1_TESTS


def test_the_cut_is_idempotent(core_cut: ModuleType, core: Path) -> None:
    core_cut.cut(1, core, ruff=None)
    first = _tree(core)
    core_cut.cut(1, core, ruff=None)
    assert _tree(core) == first


def test_a_new_cut_replaces_the_old_one(core_cut: ModuleType, core: Path) -> None:
    core_cut.cut(3, core, ruff=None)
    assert (core / COMPONENT / "diagnostics.py").exists()
    (core / TESTS / "stray.py").write_text("")
    core_cut.cut(1, core, ruff=None)
    assert _files(core / COMPONENT) == PR1_INTEGRATION
    assert _files(core / TESTS) == PR1_TESTS


def test_strict_typing_adds_the_integration_once_in_order(core_cut: ModuleType, core: Path) -> None:
    core_cut.cut(1, core, ruff=None)
    core_cut.cut(1, core, ruff=None)
    assert (core / ".strict-typing").read_text() == (
        "homeassistant.components.a_first.*\n"
        "homeassistant.components.nighp_baby_tracker.*\n"
        "homeassistant.components.zz_last.*\n"
    )


def test_the_pr1_cut_has_no_trace_of_the_hacs_repository(core_cut: ModuleType, core: Path) -> None:
    core_cut.cut(1, core, ruff=None)
    for root in (core / COMPONENT, core / TESTS):
        for name, text in _tree(root).items():
            for trace in ("custom_components", "HACS", "Core PR", "# Core:", "_custom_component"):
                assert trace not in text, (name, trace)


def test_the_pr1_cut_leaves_out_later_features(core_cut: ModuleType, core: Path) -> None:
    core_cut.cut(1, core, ruff=None)
    source = "\n".join((core / COMPONENT / name).read_text() for name in PR1_INTEGRATION)
    for later in (
        "async_migrate_entry",
        "REAUTH_SCHEMA",
        "OptionsFlow",
        "async_setup_services",
        "BabyTrackerAccountEntity",
        "TodaySensor",
        "TimerState",
        "TimerGuard",
        "_remove_deleted_devices",
        "async_remove_config_entry_device",
        "MINOR_VERSION",
        "extra_state_attributes",
    ):
        assert later not in source, later


@pytest.mark.parametrize("number", range(1, 23))
def test_every_planned_cut_is_valid_python(core_cut: ModuleType, core: Path, number: int) -> None:
    core_cut.cut(number, core, ruff=None)
    for path in [*(core / COMPONENT).rglob("*.py"), *(core / TESTS).rglob("*.py")]:
        ast.parse(path.read_text(), filename=str(path))


def test_a_cut_formatted_with_ruff_is_still_idempotent(core_cut: ModuleType, core: Path) -> None:
    ruff = shutil.which("ruff") or str(Path(sys.executable).parent / "ruff")
    core_cut.cut(1, core, ruff=ruff)
    first = _tree(core)
    core_cut.cut(1, core, ruff=ruff)
    assert _tree(core) == first


def test_a_cut_takes_the_latest_core_only_overlay(core_cut: ModuleType, core: Path) -> None:
    overlay = CORE_PREP / "overlay" / "pr01" / "tests" / "snapshots" / "test_sensor.ambr"
    for number in (1, 3):
        core_cut.cut(number, core, ruff=None)
        written = core / TESTS / "snapshots" / "test_sensor.ambr"
        assert written.read_text() == overlay.read_text()
    assert "last_pump" not in overlay.read_text()


def test_json_is_written_the_way_cores_formatter_leaves_it(
    core_cut: ModuleType, core: Path
) -> None:
    core_cut.cut(1, core, ruff=None)
    manifest = (core / COMPONENT / "manifest.json").read_text()
    assert '  "codeowners": ["@wtadler"],\n' in manifest
    assert '  "requirements": ["pybabytracker==' in manifest
    assert json.loads(manifest) == core_cut.core_manifest(1)
    strings = (core / COMPONENT / "strings.json").read_text()
    assert json.loads(strings) == core_cut.core_strings(1)
    assert strings.startswith('{\n  "config": {\n')


def test_the_pr1_snapshot_was_made_from_the_current_sources(core_cut: ModuleType) -> None:
    """Fails when the PR 1 sensors change: refresh the snapshot as core-prep/README.md says."""
    recorded = (CORE_PREP / "overlay" / "pr01" / "snapshot-sources.sha256").read_text().strip()
    assert core_cut.snapshot_sources_hash(1) == recorded


def test_the_snapshot_hash_follows_the_sensor_source(
    core_cut: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = core_cut.snapshot_sources_hash(1)
    real = core_cut.planned_files

    def renamed(pr: int) -> dict[Path, str]:
        files = real(pr)
        sensor = core_cut.COMPONENT / "sensor.py"
        files[sensor] = files[sensor].replace("last_diaper", "latest_diaper")
        return files

    monkeypatch.setattr(core_cut, "planned_files", renamed)
    assert core_cut.snapshot_sources_hash(1) != before


def test_the_overlay_record_is_not_copied_into_core(core_cut: ModuleType, core: Path) -> None:
    core_cut.cut(1, core, ruff=None)
    assert not list(core.rglob("snapshot-sources.sha256"))


def test_the_cut_names_a_stale_snapshot(
    core_cut: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = CORE_PREP / "overlay" / "pr01" / "snapshot-sources.sha256"
    assert core_cut.stale_snapshot(3) is None
    monkeypatch.setattr(core_cut, "snapshot_sources_hash", lambda pr: "changed")
    assert core_cut.stale_snapshot(1) == record
    assert core_cut.stale_snapshot(3) == record
