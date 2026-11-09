"""HACS only: scripts/core_cut.py, which writes a planned core PR's slice into a core checkout."""

import ast
import importlib.util
import json
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any

import pybabytracker
import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "core_cut.py"
INTEGRATION = ROOT / "custom_components" / "baby_tracker"
CORE_PREP = ROOT / "core-prep"
COMPONENT = Path("homeassistant/components/baby_tracker")
TESTS = Path("tests/components/baby_tracker")

# The last planned core PR, read from the plan so a new PR needs no edit here.
LAST_PR: int = yaml.safe_load((CORE_PREP / "plan.yaml").read_text())["prs"]

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


@pytest.fixture(scope="module")
def planned(core_cut: ModuleType) -> Callable[[int], dict[Path, str]]:
    """Each planned PR's files, cut once per module: the per-PR checks share them."""
    cache: dict[int, dict[Path, str]] = {}

    def files(number: int) -> dict[Path, str]:
        if number not in cache:
            cache[number] = core_cut.planned_files(number)
        return cache[number]

    return files


@pytest.fixture(scope="module")
def cut_tree(
    planned: Callable[[int], dict[Path, str]], tmp_path_factory: pytest.TempPathFactory
) -> Callable[[int], Path]:
    """A folder holding each planned PR's files as a cut writes them, made once per module."""
    cache: dict[int, Path] = {}

    def tree(number: int) -> Path:
        if number not in cache:
            root = tmp_path_factory.mktemp(f"cut{number:02d}")
            for relative, text in planned(number).items():
                (root / relative).parent.mkdir(parents=True, exist_ok=True)
                (root / relative).write_text(text)
            cache[number] = root
        return cache[number]

    return tree


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
        "from custom_components.baby_tracker.const import DOMAIN\n"
        'patch("custom_components.baby_tracker.coordinator.SCAN_INTERVAL")\n'
    )
    assert core_cut.rewrite_imports(source) == (
        "from homeassistant.components.baby_tracker.const import DOMAIN\n"
        'patch("homeassistant.components.baby_tracker.coordinator.SCAN_INTERVAL")\n'
    )


def test_imports_of_the_library_are_already_the_packages_and_stay_untouched(
    core_cut: ModuleType,
) -> None:
    source = "from pybabytracker import Record\nfrom pybabytracker.times import utc_now\n"
    assert core_cut.rewrite_imports(source) == source


def test_the_library_version_core_pins_comes_from_the_manifest_pin(
    core_cut: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    folder = tmp_path / "component"
    folder.mkdir()
    (folder / "manifest.json").write_text('{"requirements": ["pybabytracker==9.8.7"]}')
    monkeypatch.setattr(core_cut, "INTEGRATION", folder)
    assert core_cut.library_version() == "9.8.7"


def test_the_library_version_fails_loudly_when_the_manifest_has_no_pin(
    core_cut: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    folder = tmp_path / "component"
    folder.mkdir()
    (folder / "manifest.json").write_text('{"requirements": []}')
    monkeypatch.setattr(core_cut, "INTEGRATION", folder)
    with pytest.raises(ValueError, match="pybabytracker"):
        core_cut.library_version()


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
    out = core_cut.cut_python(source, LAST_PR)
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


def test_a_core_line_before_a_pr_stops_replacing_from_that_pr(core_cut: ModuleType) -> None:
    source = "def f() -> None:\n    raise Strong()  # Core before PR 3: raise Weak()\n"
    assert core_cut.cut_python(source, 2) == "def f() -> None:\n    raise Weak()\n"
    assert core_cut.cut_python(source, 3) == "def f() -> None:\n    raise Strong()\n"


def test_a_core_comment_line_before_a_pr_is_dropped_from_that_pr(core_cut: ModuleType) -> None:
    source = (
        "def f() -> None:\n    # Core before PR 3: weak()\n    strong()  # HACS-only\n    rest()\n"
    )
    assert core_cut.cut_python(source, 2) == "def f() -> None:\n    weak()\n    rest()\n"
    assert core_cut.cut_python(source, 3) == "def f() -> None:\n    rest()\n"


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
    assert manifest["documentation"] == ("https://www.home-assistant.io/integrations/baby_tracker")
    assert manifest["loggers"] == ["pybabytracker"]
    assert manifest["quality_scale"] == "bronze"
    assert manifest["categories"] == ["health_wellbeing"]
    assert manifest["requirements"] == [f"pybabytracker=={pybabytracker.__version__}"]
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


@pytest.mark.parametrize("number", [1, 9, LAST_PR])
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
        assert owner == "hacs" or 1 <= owner <= core_cut.PLAN["prs"], leaf


def test_every_action_belongs_to_a_planned_pr(core_cut: ModuleType) -> None:
    services = yaml.safe_load((INTEGRATION / "services.yaml").read_text())
    for action in services:
        owner = core_cut.key_owner(f"services.{action}")
        assert owner == "hacs" or 1 <= owner <= core_cut.PLAN["prs"], action


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
        assert owner == "hacs" or 1 <= owner <= core_cut.PLAN["prs"], path.name
    for path in (ROOT / "tests").rglob("*"):
        if path.is_dir() or "__pycache__" in path.parts:
            continue
        owner = core_cut.test_file_owner(path.relative_to(ROOT / "tests"))
        assert owner == "hacs" or 1 <= owner <= core_cut.PLAN["prs"], path


# The quality scale


def test_each_pr_has_an_up_to_date_quality_scale(core_cut: ModuleType) -> None:
    written = sorted(path.name for path in (CORE_PREP / "quality_scale").iterdir())
    assert written == [f"pr{number:02d}.yaml" for number in range(1, core_cut.PLAN["prs"] + 1)]
    for number in range(1, core_cut.PLAN["prs"] + 1):
        expected = core_cut.quality_scale_yaml(number)
        assert (CORE_PREP / "quality_scale" / f"pr{number:02d}.yaml").read_text() == expected


def test_each_quality_scale_lists_the_same_rules_as_the_repository(core_cut: ModuleType) -> None:
    hacs = yaml.safe_load((INTEGRATION / "quality_scale.yaml").read_text())["rules"]
    for number in range(1, core_cut.PLAN["prs"] + 1):
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
    rules = yaml.safe_load(core_cut.quality_scale_yaml(core_cut.PLAN["prs"]))["rules"]

    def status(value: Any) -> str:
        return value if isinstance(value, str) else value["status"]

    differ = {rule for rule in hacs if status(hacs[rule]) != status(rules[rule])}
    assert differ == set()


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
        "homeassistant.components.baby_tracker.*\n"
        "homeassistant.components.zz_last.*\n"
    )


@pytest.mark.parametrize("number", range(1, LAST_PR + 1))
def test_every_planned_cut_has_no_trace_of_the_hacs_repository(
    cut_tree: Callable[[int], Path], number: int
) -> None:
    core = cut_tree(number)
    for root in (core / COMPONENT, core / TESTS):
        for name, text in _tree(root).items():
            for trace in ("custom_components", "HACS", "Core PR", "# Core:", "_custom_component"):
                assert trace not in text, (name, trace)


@pytest.mark.parametrize("number", range(1, LAST_PR + 1))
def test_every_cut_raises_config_entry_auth_failed_on_a_rejected_password(
    planned: Callable[[int], dict[Path, str]], number: int
) -> None:
    # Core starts a reauth flow only when the integration has one, so PR 1, which has none,
    # still gets the authentication path: setup fails, and a loaded entry stops polling
    # instead of sending the rejected password at every interval.
    source = planned(number)[COMPONENT / "coordinator.py"]
    assert "raise ConfigEntryAuthFailed(" in source
    assert "ConfigEntryError" not in source
    assert 'translation_key="auth_failed"' in source


def test_reconfiguration_is_exempt_only_once_reauthentication_exists() -> None:
    # The rule exempts credentials-only flows when the reauthentication flow handles the
    # credentials, and that flow arrives in PR 2.
    def rule(number: int) -> Any:
        scale = yaml.safe_load((CORE_PREP / "quality_scale" / f"pr{number:02}.yaml").read_text())
        return scale["rules"]["reconfiguration-flow"]

    assert rule(1) == "todo"
    assert rule(2)["status"] == "exempt"
    assert "reauthentication" in rule(2)["comment"]


def test_pr2_brings_the_reauth_tests_with_it(core_cut: ModuleType, core: Path) -> None:
    core_cut.cut(1, core, ruff=None)
    assert not (core / TESTS / "test_init_reauth.py").exists()
    core_cut.cut(2, core, ruff=None)
    assert "starts_reauth" in (core / TESTS / "test_init_reauth.py").read_text()


def test_the_repository_still_raises_config_entry_auth_failed() -> None:
    tree = ast.parse((INTEGRATION / "coordinator.py").read_text())
    raised = {
        node.exc.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Raise)
        and isinstance(node.exc, ast.Call)
        and isinstance(node.exc.func, ast.Name)
    }
    assert "ConfigEntryAuthFailed" in raised
    assert "ConfigEntryError" not in raised
    assert "ConfigEntryError" not in {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}


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


@pytest.mark.parametrize("number", range(1, LAST_PR + 1))
def test_every_planned_cut_is_valid_python(cut_tree: Callable[[int], Path], number: int) -> None:
    core = cut_tree(number)
    for path in [*(core / COMPONENT).rglob("*.py"), *(core / TESTS).rglob("*.py")]:
        ast.parse(path.read_text(), filename=str(path))


@pytest.mark.parametrize("number", range(1, LAST_PR + 1))
def test_every_planned_cut_leaves_no_unused_imports(
    cut_tree: Callable[[int], Path], number: int
) -> None:
    # Core's ruff fails on an import whose only users wait for a later PR, so each import
    # needs the marker of its earliest user.
    core = cut_tree(number)
    ruff = shutil.which("ruff") or str(Path(sys.executable).parent / "ruff")
    result = subprocess.run(
        [
            ruff,
            "check",
            "--isolated",
            "--no-cache",
            "--select",
            "F401",
            "--output-format",
            "concise",
            str(core / COMPONENT),
            str(core / TESTS),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout


def _bound(target: ast.expr) -> set[str]:
    """The names an assignment target binds: ``A``, or each name of ``A, (B, *C)``."""
    return {node.id for node in ast.walk(target) if isinstance(node, ast.Name)}


def _class_members(tree: ast.Module) -> dict[str, set[str]]:
    """The names each top-level class assigns in its body, by class name."""
    members: dict[str, set[str]] = {}
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            names = members.setdefault(node.name, set())
            for item in node.body:
                if isinstance(item, ast.Assign):
                    for target in item.targets:
                        names |= _bound(target)
                elif isinstance(item, ast.AnnAssign):
                    names |= _bound(item.target)
                elif isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef):
                    names.add(item.name)
    return members


def _python(files: dict[Path, str]) -> dict[Path, ast.Module]:
    return {path: ast.parse(text) for path, text in files.items() if path.suffix == ".py"}


def _missing_members(files: dict[Path, str]) -> list[str]:
    """``Class.member`` reads, in the component and its tests, of a member the class lacks."""
    members = _cut_members(files)
    return sorted(
        f"{path}:{node.lineno}: {node.value.id}.{node.attr}"
        for path, tree in _python(files).items()
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id in members
        and node.attr not in members[node.value.id]
        and not node.attr.startswith("__")
    )


def _cut_members(files: dict[Path, str]) -> dict[str, set[str]]:
    members: dict[str, set[str]] = {}
    for path, tree in _python(files).items():
        if path.is_relative_to(COMPONENT):
            for cls, names in _class_members(tree).items():
                members.setdefault(cls, set()).update(names)
    return members


@pytest.mark.parametrize("number", range(1, LAST_PR + 1))
def test_every_planned_cut_uses_only_class_members_it_keeps(
    planned: Callable[[int], dict[Path, str]], number: int
) -> None:
    # A HACS-only member (``BABY = "baby"  # HACS-only``) leaves the cut, so a line still kept in
    # core that reads ``BabyTrackerServiceArgument.BABY`` fails at import. Ruff can't see that.
    files = planned(number)
    if number >= 9:
        # The check is only as good as the members it finds: from PR 9 on, the actions' fields.
        assert "DEVICE_ID" in _cut_members(files).get("BabyTrackerServiceArgument", set())
    assert _missing_members(files) == []


def test_the_member_check_reports_a_member_the_cut_removed() -> None:
    files = {
        COMPONENT / "const.py": "class Field:\n    KEPT = 1\n",
        TESTS / "test_x.py": "from homeassistant.components.baby_tracker.const import Field\n"
        "Field.KEPT\nField.GONE\n",
    }
    assert _missing_members(files) == [f"{TESTS / 'test_x.py'}:3: Field.GONE"]


def test_the_member_check_knows_methods_annotations_and_tuple_targets() -> None:
    files = {
        COMPONENT / "const.py": (
            "class Field:\n"
            "    ONE, (TWO, THREE) = 1, (2, 3)\n"
            "    FOUR: int = 4\n"
            "    def five(self) -> None: ...\n"
            "    async def six(self) -> None: ...\n"
            "Field.ONE, Field.TWO, Field.THREE, Field.FOUR, Field.five, Field.six\n"
            "Field.__name__\n"
            "Field.SEVEN\n"
        ),
    }
    assert _missing_members(files) == [f"{COMPONENT / 'const.py'}:8: Field.SEVEN"]


@pytest.mark.parametrize("number", range(1, LAST_PR + 1))
def test_every_planned_cut_uses_only_names_it_defines(
    cut_tree: Callable[[int], Path], number: int
) -> None:
    # A line kept in core that uses a name only HACS defines (a HACS-only helper or enum member)
    # parses fine but fails at runtime, so each such line needs the HACS-only marker too.
    core = cut_tree(number)
    ruff = shutil.which("ruff") or str(Path(sys.executable).parent / "ruff")
    result = subprocess.run(
        [
            ruff,
            "check",
            "--isolated",
            "--no-cache",
            "--target-version",
            "py314",
            "--select",
            "F821,F822,F823",
            "--output-format",
            "concise",
            str(core / COMPONENT),
            str(core / TESTS),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout


def _module_name(path: Path) -> str:
    parts = path.with_suffix("").parts
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


def _top_level_names(tree: ast.Module) -> set[str]:
    """The names a module binds at its top level, where an import of it can find them."""
    names: set[str] = set()

    def visit(body: list[ast.stmt]) -> None:
        for node in body:
            match node:
                case ast.FunctionDef() | ast.AsyncFunctionDef() | ast.ClassDef():
                    names.add(node.name)
                case ast.TypeAlias():
                    names.update(_bound(node.name))
                case ast.Assign():
                    for target in node.targets:
                        names.update(_bound(target))
                case ast.AnnAssign() | ast.AugAssign():
                    names.update(_bound(node.target))
                case ast.Import():
                    names.update((a.asname or a.name).split(".")[0] for a in node.names)
                case ast.ImportFrom():
                    names.update(a.asname or a.name for a in node.names)
                case ast.For() | ast.AsyncFor():
                    names.update(_bound(node.target))
                    visit(node.body)
                    visit(node.orelse)
                case ast.With() | ast.AsyncWith():
                    for item in node.items:
                        if item.optional_vars is not None:
                            names.update(_bound(item.optional_vars))
                    visit(node.body)
                case ast.If() | ast.While():
                    visit(node.body)
                    visit(node.orelse)
                case ast.Try() | ast.TryStar():
                    visit(node.body)
                    for handler in node.handlers:
                        visit(handler.body)
                    visit(node.orelse)
                    visit(node.finalbody)

    visit(tree.body)
    return names


def _unresolved_imports(files: dict[Path, str]) -> list[str]:
    """Imports, in the component and its tests, of a module or name the cut doesn't have."""
    trees = _python(files)
    modules = {_module_name(path): tree for path, tree in trees.items()}
    packages = {_module_name(path) for path in trees if path.name == "__init__.py"}
    names = {module: _top_level_names(tree) for module, tree in modules.items()}
    ours = tuple(_module_name(root) for root in (COMPONENT, TESTS))

    def is_ours(module: str) -> bool:
        return any(module == root or module.startswith(f"{root}.") for root in ours)

    found = []
    for path, tree in trees.items():
        here = _module_name(path)
        package = here if path.name == "__init__.py" else here.rpartition(".")[0]
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                found.extend(
                    f"{path}:{node.lineno}: no module {alias.name}"
                    for alias in node.names
                    if is_ours(alias.name) and alias.name not in modules
                )
                continue
            if not isinstance(node, ast.ImportFrom):
                continue
            if node.level:
                base = package.rsplit(".", node.level - 1)[0] if node.level > 1 else package
                target = f"{base}.{node.module}" if node.module else base
                shown = "." * node.level + (node.module or "")
            elif node.module is not None and is_ours(node.module):
                target = shown = node.module
            elif node.module is not None and f"{node.module}.{node.names[0].name}" in modules:
                # ``from homeassistant.components import baby_tracker``
                continue
            else:
                continue
            if target not in modules:
                found.append(f"{path}:{node.lineno}: no module {shown}")
                continue
            found.extend(
                f"{path}:{node.lineno}: {shown} has no {alias.name}"
                for alias in node.names
                if alias.name != "*"
                and alias.name not in names[target]
                and not (target in packages and f"{target}.{alias.name}" in modules)
            )
    return sorted(found)


@pytest.mark.parametrize("number", range(1, LAST_PR + 1))
def test_every_planned_cut_imports_only_what_it_has(
    planned: Callable[[int], dict[Path, str]], number: int
) -> None:
    # Ruff can't see that ``from .bottle import OUNCES`` names a module or a name the cut leaves
    # for a later PR (or for HACS), and core fails at import.
    assert _unresolved_imports(planned(number)) == []


def test_the_import_check_reports_a_missing_module_and_a_missing_name() -> None:
    files = {
        COMPONENT / "__init__.py": "from .const import DOMAIN, LATER\nfrom .bottle import OUNCES\n",
        COMPONENT / "const.py": "DOMAIN = 'baby_tracker'\n",
        TESTS / "__init__.py": "",
        TESTS / "test_x.py": "from homeassistant.components.baby_tracker.const import GONE\n"
        "import homeassistant.components.baby_tracker.gone\n",
    }
    assert _unresolved_imports(files) == [
        f"{COMPONENT / '__init__.py'}:1: .const has no LATER",
        f"{COMPONENT / '__init__.py'}:2: no module .bottle",
        f"{TESTS / 'test_x.py'}:1: homeassistant.components.baby_tracker.const has no GONE",
        f"{TESTS / 'test_x.py'}:2: no module homeassistant.components.baby_tracker.gone",
    ]


def test_the_import_check_finds_every_kind_of_top_level_name() -> None:
    files = {
        COMPONENT / "__init__.py": "from .const import *\n",
        COMPONENT / "const.py": (
            "import json\n"
            "import os.path\n"
            "from typing import Any as Anything\n"
            "type Alias = int\n"
            "ONE, (TWO, *REST) = 1, (2, 3)\n"
            "THREE: int = 3\n"
            "FOUR: int\n"
            "FIVE = 0\n"
            "FIVE += 1\n"
            "def function() -> None: ...\n"
            "async def coroutine() -> None: ...\n"
            "class Class: ...\n"
            "if FIVE:\n"
            "    SIX = 6\n"
            "else:\n"
            "    SEVEN = 7\n"
            "try:\n"
            "    import missing_library as EIGHT\n"
            "except ImportError:\n"
            "    EIGHT = None\n"
            "with open(json.__file__) as NINE:\n"
            "    TEN = 10\n"
            "for ELEVEN in ():\n"
            "    pass\n"
        ),
        COMPONENT / "user.py": (
            "from .const import (\n"
            "    json, os, Anything, Alias, ONE, TWO, REST, THREE, FOUR, FIVE, function,\n"
            "    coroutine, Class, SIX, SEVEN, EIGHT, NINE, TEN, ELEVEN,\n"
            ")\n"
            "from .const import TWELVE\n"
        ),
    }
    assert _unresolved_imports(files) == [f"{COMPONENT / 'user.py'}:5: .const has no TWELVE"]


def test_the_import_check_follows_packages_and_their_submodules() -> None:
    files = {
        COMPONENT / "__init__.py": "from . import const\nfrom .const import DOMAIN\nNAME = 1\n",
        COMPONENT / "const.py": "DOMAIN = 'baby_tracker'\n",
        TESTS / "__init__.py": "NOW = 1\n",
        TESTS / "helpers.py": "from . import NOW\n",
        TESTS / "test_x.py": (
            "from . import NOW, helpers\n"
            "from .helpers import NOW as THEN\n"
            "from homeassistant.components.baby_tracker import NAME, const\n"
            "from homeassistant.components import baby_tracker\n"
            "from homeassistant.const import Platform\n"
            "import homeassistant.components.baby_tracker.const\n"
            "def test() -> None:\n"
            "    from homeassistant.components.baby_tracker.const import DOMAIN\n"
            "from . import later\n"
        ),
    }
    assert _unresolved_imports(files) == [f"{TESTS / 'test_x.py'}:9: . has no later"]


def test_the_import_check_looks_inside_functions() -> None:
    files = {
        COMPONENT / "__init__.py": "def setup() -> None:\n    from .const import GONE\n",
        COMPONENT / "const.py": "",
    }
    assert _unresolved_imports(files) == [f"{COMPONENT / '__init__.py'}:2: .const has no GONE"]


def _removed_fields(files: dict[Path, str]) -> set[str]:
    """The field names the repository's actions take that the cut's actions don't."""

    def fields(text: str) -> set[str]:
        return {
            item.value.value
            for node in ast.parse(text).body
            if isinstance(node, ast.ClassDef) and node.name == "BabyTrackerServiceArgument"
            for item in node.body
            if isinstance(item, ast.Assign)
            and isinstance(item.value, ast.Constant)
            and isinstance(item.value.value, str)
        }

    repository = fields((INTEGRATION / "services.py").read_text())
    return repository - fields(files.get(COMPONENT / "services.py", ""))


# The methods of ``hass.services`` whose arguments are a domain and an action.
_NAMES_AN_ACTION = {
    "call",
    "has_service",
    "async_remove",
    "remove",
    "supports_response",
    "async_register",
    "register",
}


def _action_call(node: ast.AST) -> tuple[ast.expr | None, list[ast.expr]] | None:
    """The action and data of a call that names one of the integration's actions, else None.

    That's ``async_call`` (or ``call``) for the integration's domain, or another method of
    ``hass.services`` that takes a domain and an action (``has_service``, ``async_remove``, and
    so on).
    """
    if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
        return None
    registry = isinstance(node.func.value, ast.Attribute) and node.func.value.attr == "services"
    if node.func.attr != "async_call" and not (registry and node.func.attr in _NAMES_AN_ACTION):
        return None
    keywords = {k.arg: k.value for k in node.keywords if k.arg}
    domain = node.args[0] if node.args else keywords.get("domain")
    ours = (isinstance(domain, ast.Name) and domain.id == "DOMAIN") or (
        isinstance(domain, ast.Constant) and domain.value == "baby_tracker"
    )
    if not ours:
        return None
    action = node.args[1] if len(node.args) > 1 else keywords.get("service")
    data = node.args[2] if len(node.args) > 2 else keywords.get("service_data")
    return action, [] if data is None else [data]


def _test_functions(
    files: dict[Path, str],
) -> dict[Path, list[ast.FunctionDef | ast.AsyncFunctionDef]]:
    return {
        path: [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)]
        for path, tree in _python(files).items()
        if path.is_relative_to(TESTS)
    }


def _action_helpers(
    functions: dict[Path, list[ast.FunctionDef | ast.AsyncFunctionDef]],
) -> dict[str, list[str]]:
    """The tests' helpers that call one of the actions, with their parameters in order."""
    return {
        function.name: [
            arg.arg
            for arg in (*function.args.posonlyargs, *function.args.args, *function.args.kwonlyargs)
        ]
        for found in functions.values()
        for function in found
        if any(_action_call(node) is not None for node in ast.walk(function))
    }


def _module_bindings(files: dict[Path, str]) -> dict[str, list[tuple[Path, ast.AST]]]:
    """What each top-level name of the tests is built from: its assignments, and the
    ``NAME[key] = ...`` and ``NAME.append(...)`` statements that fill it in."""
    bindings: dict[str, list[tuple[Path, ast.AST]]] = {}
    for path, tree in _python(files).items():
        if not path.is_relative_to(TESTS):
            continue
        for node in tree.body:
            targets: list[ast.expr] = []
            if isinstance(node, ast.Assign):
                targets = node.targets
            elif isinstance(node, ast.AnnAssign | ast.AugAssign):
                targets = [node.target]
            elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
                func = node.value.func
                if isinstance(func, ast.Attribute):
                    targets = [func.value]
            for target in targets:
                while isinstance(target, ast.Subscript | ast.Attribute):
                    target = target.value
                for name in _bound(target):
                    bindings.setdefault(name, []).append((path, node))
    return bindings


def _parametrized_names(decorator: ast.expr) -> set[str]:
    """The argument names of a ``pytest.mark.parametrize`` decorator, else none."""
    if not (
        isinstance(decorator, ast.Call)
        and ast.unparse(decorator.func) == "pytest.mark.parametrize"
        and decorator.args
    ):
        return set()
    names = decorator.args[0]
    if isinstance(names, ast.Constant) and isinstance(names.value, str):
        return {name.strip() for name in names.value.split(",")}
    if isinstance(names, ast.Tuple | ast.List):
        return {n.value for n in names.elts if isinstance(n, ast.Constant)}
    return set()


def _actions_called(files: dict[Path, str]) -> list[str]:
    """Where a test of the cut calls an action the cut's services.yaml doesn't have.

    An action is called by name in ``async_call`` for the integration's domain, or as the
    ``action`` argument of a test helper that makes such a call. Only the repository's own
    actions count, so a test that an action doesn't exist isn't one. A name the call reads is
    followed to what it's built from: the function's own assignments, the values of its
    ``parametrize`` argument, and the tests' top-level tables. There, any string that names
    one of the repository's actions counts, unless it's only compared against.
    """
    services = yaml.safe_load(files.get(COMPONENT / "services.yaml", "")) or {}
    known = set(yaml.safe_load((INTEGRATION / "services.yaml").read_text()))
    functions = _test_functions(files)
    helpers = _action_helpers(functions)
    bindings = _module_bindings(files)
    callers = set(helpers)
    while True:
        more = {
            function.name
            for found_in in functions.values()
            for function in found_in
            if any(
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in callers
                for node in ast.walk(function)
            )
        } - callers
        if not more:
            break
        callers |= more
    found: set[tuple[str, int, str]] = set()

    def constants(node: ast.AST, path: Path, seen: set[str]) -> None:
        compared = {
            id(inner)
            for compare in ast.walk(node)
            if isinstance(compare, ast.Compare)
            for inner in ast.walk(compare)
        }
        for inner in ast.walk(node):
            if id(inner) in compared:
                continue
            if (
                isinstance(inner, ast.Constant)
                and inner.value in known
                and inner.value not in services
            ):
                found.add((str(path), inner.lineno, inner.value))
            elif isinstance(inner, ast.Name) and inner.id in bindings and inner.id not in seen:
                seen.add(inner.id)
                for where, binding in bindings[inner.id]:
                    constants(binding, where, seen)

    for path, found_in in functions.items():
        for function in found_in:
            sent: list[ast.expr] = []
            for node in ast.walk(function):
                if (called := _action_call(node)) is not None:
                    sent.extend(e for e in (called[0], *called[1]) if e is not None)
                elif (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id in callers
                ):
                    sent.extend(node.args)
                    sent.extend(k.value for k in node.keywords)
            read = {n.id for e in sent for n in ast.walk(e) if isinstance(n, ast.Name)}
            while True:
                more = {
                    n.id
                    for node in ast.walk(function)
                    if isinstance(node, ast.Assign) and any(_bound(t) & read for t in node.targets)
                    for n in ast.walk(node.value)
                    if isinstance(n, ast.Name)
                } - read
                if not more:
                    break
                read |= more
            for decorator in function.decorator_list:
                if _parametrized_names(decorator) & read:
                    constants(decorator.args[1], path, set())
            for name in read & set(bindings):
                for where, binding in bindings[name]:
                    constants(binding, where, {name})
            for node in ast.walk(function):
                action: ast.expr | None = None
                if (called := _action_call(node)) is not None:
                    action = called[0]
                elif (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and "action" in helpers.get(node.func.id, [])
                ):
                    index = helpers[node.func.id].index("action")
                    keywords = {k.arg: k.value for k in node.keywords}
                    action = node.args[index] if len(node.args) > index else keywords.get("action")
                if (
                    isinstance(action, ast.Constant)
                    and action.value in known
                    and action.value not in services
                ):
                    found.add((str(path), action.lineno, action.value))
    return [f"{path}:{line}: {action}" for path, line, action in sorted(found)]


def _fields_sent(files: dict[Path, str], removed: set[str]) -> list[str]:
    """Where a test of the cut sends one of the ``removed`` fields to one of the actions.

    A field is sent when it's a key of a dict passed to ``async_call`` for the integration's
    domain, or to a test helper that makes such a call, or a keyword argument of such a helper
    (other than one of its own parameters). A dict assigned to a name the call's data reads, and
    the values of a ``parametrize`` argument it reads, count too.
    """
    functions = _test_functions(files)
    helpers = _action_helpers(functions)
    found: set[tuple[str, int, str]] = set()

    def keys(node: ast.AST, path: Path) -> None:
        for inner in ast.walk(node):
            if isinstance(inner, ast.Dict):
                for key in inner.keys:
                    if isinstance(key, ast.Constant) and key.value in removed:
                        found.add((str(path), key.lineno, key.value))

    for path, found_in in functions.items():
        for function in found_in:
            sent: list[ast.expr] = []
            for node in ast.walk(function):
                if (called := _action_call(node)) is not None:
                    sent.extend(called[1])
                elif (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id in helpers
                ):
                    sent.extend(node.args)
                    for keyword in node.keywords:
                        if keyword.arg is None:
                            sent.append(keyword.value)
                        elif keyword.arg in removed and keyword.arg not in helpers[node.func.id]:
                            found.add((str(path), keyword.lineno, keyword.arg))
            read = {n.id for data in sent for n in ast.walk(data) if isinstance(n, ast.Name)}
            for data in sent:
                keys(data, path)
            for node in ast.walk(function):
                if isinstance(node, ast.Assign) and any(_bound(t) & read for t in node.targets):
                    keys(node.value, path)
            for decorator in function.decorator_list:
                if (
                    isinstance(decorator, ast.Call)
                    and ast.unparse(decorator.func) == "pytest.mark.parametrize"
                    and isinstance(decorator.args[0], ast.Constant)
                    and {n.strip() for n in str(decorator.args[0].value).split(",")} & read
                ):
                    for value in ast.walk(decorator.args[1]):
                        if isinstance(value, ast.Constant) and value.value in removed:
                            found.add((str(path), value.lineno, value.value))
    return [f"{path}:{line}: {field}" for path, line, field in sorted(found)]


@pytest.mark.parametrize("number", range(1, LAST_PR + 1))
def test_every_planned_cut_sends_only_fields_its_actions_take(
    planned: Callable[[int], dict[Path, str]], number: int
) -> None:
    # A test kept in core that still sends a HACS-only field (the deprecated ``baby`` or
    # ``device``) fails in core with "extra keys not allowed".
    files = planned(number)
    removed = _removed_fields(files)
    assert {"baby", "device"} <= removed
    assert _fields_sent(files, removed) == []


def test_the_removed_fields_are_the_hacs_only_ones_and_those_of_later_prs(
    core_cut: ModuleType,
) -> None:
    assert _removed_fields(core_cut.planned_files(LAST_PR)) == {"baby", "device"}
    assert _removed_fields(core_cut.planned_files(9)) >= {"baby", "device", "medicine"}
    assert "device_id" not in _removed_fields(core_cut.planned_files(9))


def test_the_field_check_finds_each_way_a_test_sends_a_field() -> None:
    helpers = (
        "from homeassistant.components.baby_tracker.const import DOMAIN\n"
        "async def log_pump(hass, **data):\n"
        "    await hass.services.async_call(DOMAIN, 'log_pump', data, blocking=True)\n"
        "async def call(hass, action, baby='one', **data):\n"
        "    await hass.services.async_call(DOMAIN, action, data, blocking=True)\n"
    )
    test = (
        "import pytest\n"
        "from .helpers import call, log_pump\n"
        "async def test_a(hass):\n"
        "    await log_pump(hass, device='x')\n"
        "    await hass.services.async_call(DOMAIN, 'undo', {'baby': 'x'})\n"
        "    await hass.services.async_call(domain=DOMAIN, service='u', service_data={'baby': 1})\n"
        "    await hass.services.async_call('baby_tracker', 'undo', {'device': 'x'})\n"
        "async def _call(hass, action, data):\n"
        "    await hass.services.async_call(DOMAIN, action, data, blocking=True)\n"
        "async def test_b(hass):\n"
        "    values = {'config_entry_id': 'e', 'device': 'd'}\n"
        "    await _call(hass, 'log_pump', {**{k: values[k] for k in values}, 'left_amount': 3})\n"
        "@pytest.mark.parametrize('field', ['config_entry_id', 'baby'])\n"
        "async def test_c(hass, field):\n"
        "    await _call(hass, 'log_pump', {field: 'x'})\n"
    )
    files = {TESTS / "helpers.py": helpers, TESTS / "test_x.py": test}
    found = _fields_sent(files, {"baby", "device"})
    assert found == [
        f"{TESTS / 'test_x.py'}:4: device",
        f"{TESTS / 'test_x.py'}:5: baby",
        f"{TESTS / 'test_x.py'}:6: baby",
        f"{TESTS / 'test_x.py'}:7: device",
        f"{TESTS / 'test_x.py'}:11: device",
        f"{TESTS / 'test_x.py'}:13: baby",
    ]


@pytest.mark.parametrize("number", range(1, LAST_PR + 1))
def test_every_planned_cut_calls_only_actions_it_has(
    planned: Callable[[int], dict[Path, str]], number: int
) -> None:
    # A test that calls an action planned for a later PR fails in core with "action not found".
    assert _actions_called(planned(number)) == []


def test_the_action_check_finds_each_way_a_test_calls_an_action() -> None:
    helpers = (
        "from homeassistant.components.baby_tracker.const import DOMAIN\n"
        "async def call(hass, action, baby='one', **data):\n"
        "    await hass.services.async_call(DOMAIN, action, data, blocking=True)\n"
        "async def undo(hass, **data):\n"
        "    await hass.services.async_call(DOMAIN, 'undo', data, blocking=True)\n"
    )
    test = (
        "from .helpers import call\n"
        "async def test_a(hass, action):\n"
        "    await hass.services.async_call(DOMAIN, 'log_diaper', {})\n"
        "    await hass.services.async_call(DOMAIN, 'log_bottle', {})\n"
        "    await hass.services.async_call(domain='baby_tracker', service='log_sleep')\n"
        "    await call(hass, 'log_nursing')\n"
        "    await call(hass, action='log_pump')\n"
        "    await call(hass, 'log_diaper', 'two')\n"
        "    await call(hass, action)\n"
        "    await hass.services.async_call('switch', 'turn_on', {})\n"
    )
    files = {
        COMPONENT / "services.yaml": "log_diaper:\n  fields: {}\n",
        TESTS / "helpers.py": helpers,
        TESTS / "test_x.py": test,
    }
    assert _actions_called(files) == [
        f"{TESTS / 'helpers.py'}:5: undo",
        f"{TESTS / 'test_x.py'}:4: log_bottle",
        f"{TESTS / 'test_x.py'}:5: log_sleep",
        f"{TESTS / 'test_x.py'}:6: log_nursing",
        f"{TESTS / 'test_x.py'}:7: log_pump",
    ]


def test_the_action_check_follows_parametrize_and_tables_to_the_action() -> None:
    helpers = (
        "from homeassistant.components.baby_tracker.const import DOMAIN\n"
        "async def call(hass, action, baby='one', **data):\n"
        "    await hass.services.async_call(DOMAIN, action, data, blocking=True)\n"
    )
    test = (
        "import pytest\n"
        "from .helpers import call\n"
        "VALID = {}\n"
        "VALID['log_diaper'] = {}\n"
        "VALID['log_bottle'] = {}\n"
        "ACTIONS = list(VALID)\n"
        "LIMITS = {'too long': ('log_pump', {}, 'minutes_too_long')}\n"
        "async def run_valid(hass, name):\n"
        "    await call(hass, name, **VALID[name])\n"
        "@pytest.mark.parametrize('name', ACTIONS)\n"
        "async def test_a(hass, name):\n"
        "    await run_valid(hass, name)\n"
        "@pytest.mark.parametrize('case', list(LIMITS))\n"
        "async def test_b(hass, case):\n"
        "    action, data, key = LIMITS[case]\n"
        "    await call(hass, action, **data)\n"
        "@pytest.mark.parametrize(('action', 'n'), [('log_sleep', 1), ('log_diaper', 2)])\n"
        "async def test_c(hass, action, n):\n"
        "    await call(hass, action)\n"
        "@pytest.mark.parametrize('name', [n for n in ('log_diaper',) if n != 'undo'])\n"
        "async def test_d(hass, name):\n"
        "    await call(hass, name)\n"
        "@pytest.mark.parametrize('kind', ['wet', 'dirty'])\n"
        "async def test_e(hass, kind):\n"
        "    await call(hass, 'log_diaper', type=kind)\n"
    )
    files = {
        COMPONENT / "services.yaml": "log_diaper:\n  fields: {}\n",
        TESTS / "helpers.py": helpers,
        TESTS / "test_x.py": test,
    }
    assert _actions_called(files) == [
        f"{TESTS / 'test_x.py'}:5: log_bottle",
        f"{TESTS / 'test_x.py'}:7: log_pump",
        f"{TESTS / 'test_x.py'}:17: log_sleep",
    ]


def _platform_modules() -> set[str]:
    """The platforms the repository sets up: the members of ``PLATFORMS`` in __init__.py."""
    for node in ast.parse((INTEGRATION / "__init__.py").read_text()).body:
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "PLATFORMS"
            and isinstance(node.value, ast.List)
        ):
            return {
                item.attr.lower() for item in node.value.elts if isinstance(item, ast.Attribute)
            }
    raise AssertionError("__init__.py has no PLATFORMS list of Platform members")


def _service_call_domain(node: ast.AST) -> ast.expr | None:
    """The domain a ``hass.services.async_call`` (or ``call``) names, else None."""
    if not (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"async_call", "call"}
        and isinstance(node.func.value, ast.Attribute)
        and node.func.value.attr == "services"
    ):
        return None
    keywords = {k.arg: k.value for k in node.keywords if k.arg}
    return node.args[0] if node.args else keywords.get("domain")


def _repository_tests() -> dict[Path, str]:
    """Every test module of the repository as it is written, by its path in a core cut."""
    return {
        TESTS / path.relative_to(ROOT / "tests"): path.read_text()
        for path in sorted((ROOT / "tests").glob("*.py"))
    }


def _platforms_used(files: dict[Path, str], written: dict[Path, str] | None = None) -> list[str]:
    """Where a test of the cut works on a platform the cut doesn't have.

    A test works on a platform when it calls one of the platform's actions (``button.press``,
    ``switch.turn_on``) through ``hass.services``, by the platform's name as a string. A helper
    that does so, or that uses a helper that does so (to any depth, called or passed on), counts
    too, at its definition and at every use of it. A helper that takes the domain as a parameter
    counts at the calls that name a platform for it. The platforms are the repository's own, so
    the calls to ``homeassistant`` or to another integration aren't looked at, and a platform is
    in the cut when its module is.

    ``written`` is the tests as they are written, before the cut. A helper the cut leaves out is
    still known by what it needs, so a test that calls it anyway counts too.
    """
    platforms = _platform_modules()
    missing = {name for name in platforms if COMPONENT / f"{name}.py" not in files}
    functions = [function for found in _test_functions(files).values() for function in found]
    kept = {function.name for function in functions}
    # What the cut dropped is still looked up by name, after what it kept.
    functions += [
        function
        for found in _test_functions(written or {}).values()
        for function in found
        if function.name not in kept
    ]

    def literal(node: ast.expr | None) -> str | None:
        ok = isinstance(node, ast.Constant) and isinstance(node.value, str)
        return node.value if ok and node.value in platforms else None  # type: ignore[union-attr]

    def parameters(function: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
        args = function.args
        return [arg.arg for arg in (*args.posonlyargs, *args.args, *args.kwonlyargs)]

    # The helpers that pass a parameter on as the domain: name -> the parameter's name.
    domain_parameter: dict[str, str] = {}
    for function in functions:
        for node in ast.walk(function):
            domain = _service_call_domain(node)
            if isinstance(domain, ast.Name) and domain.id in parameters(function):
                domain_parameter[function.name] = domain.id

    def named_platform(node: ast.Call, helper: str) -> str | None:
        name = domain_parameter[helper]
        position = next(function for function in functions if function.name == helper)
        names = parameters(position)
        keywords = {k.arg: k.value for k in node.keywords if k.arg}
        if name in keywords:
            return literal(keywords[name])
        index = names.index(name)
        return literal(node.args[index]) if index < len(node.args) else None

    def parametrized(function: ast.FunctionDef | ast.AsyncFunctionDef) -> list[tuple[int, str]]:
        """The platforms in the ``parametrize`` values of the argument the function calls with."""
        if (name := domain_parameter.get(function.name)) is None:
            return []
        out: list[tuple[int, str]] = []
        for decorator in function.decorator_list:
            if not (isinstance(decorator, ast.Call) and len(decorator.args) > 1):
                continue
            names = decorator.args[0]
            if isinstance(names, ast.Constant) and isinstance(names.value, str):
                columns = [column.strip() for column in names.value.split(",")]
            elif isinstance(names, ast.Tuple | ast.List):
                columns = [n.value for n in names.elts if isinstance(n, ast.Constant)]
            else:
                continue
            if name not in columns or not isinstance(decorator.args[1], ast.List | ast.Tuple):
                continue
            for row in decorator.args[1].elts:
                cell = row.elts[columns.index(name)] if isinstance(row, ast.Tuple) else row
                if (platform := literal(cell)) is not None:
                    out.append((cell.lineno, platform))
        return out

    def direct(function: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
        found: set[str] = {platform for _, platform in parametrized(function)}
        for node in ast.walk(function):
            if (name := literal(_service_call_domain(node))) is not None or (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in domain_parameter
                and (name := named_platform(node, node.func.id)) is not None
            ):
                found.add(name)
        return found

    needs: dict[str, set[str]] = {}
    for function in functions:
        needs.setdefault(function.name, set()).update(direct(function))
    while True:
        changed = False
        for function in functions:
            for node in ast.walk(function):
                if (
                    isinstance(node, ast.Name)
                    and not needs.get(node.id, set()) <= needs[function.name]
                ):
                    needs[function.name] |= needs[node.id]
                    changed = True
        if not changed:
            break

    found: set[tuple[str, int, str]] = set()
    for path, in_file in _test_functions(files).items():
        for function in in_file:
            for line, platform in parametrized(function):
                if platform in missing:
                    found.add((str(path), line, platform))
            for node in ast.walk(function):
                reached: set[str] = set()
                if (name := literal(_service_call_domain(node))) is not None:
                    reached = {name}
                elif isinstance(node, ast.Name):
                    reached = needs.get(node.id, set())
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id in domain_parameter
                    and (name := named_platform(node, node.func.id)) is not None
                ):
                    reached = {*reached, name}
                for name in reached & missing:
                    found.add((str(path), node.lineno, name))
    return [f"{path}:{line}: {name}" for path, line, name in sorted(found)]


@pytest.mark.parametrize("number", range(1, LAST_PR + 1))
def test_every_planned_cut_uses_only_platforms_it_has(
    planned: Callable[[int], dict[Path, str]], number: int
) -> None:
    # A test that presses a button or turns on a switch before that platform is planned fails in
    # core: the entity it works on doesn't exist yet.
    assert _platforms_used(planned(number), _repository_tests()) == []


def _platform_files(*platforms: str) -> dict[Path, str]:
    return {COMPONENT / f"{name}.py": "" for name in platforms}


def test_the_platform_check_flags_a_call_to_a_platform_the_cut_lacks() -> None:
    test = (
        "async def test_a(hass):\n"
        "    await hass.services.async_call('button', 'press', {}, blocking=True)\n"
        "    await hass.services.async_call('switch', 'turn_on', {})\n"
        "    await hass.services.async_call(domain='switch', service='turn_off')\n"
        "    await hass.services.async_call(DOMAIN, 'log_diaper', {})\n"
        "    await hass.services.async_call('homeassistant', 'update_entity', {})\n"
    )
    files = {**_platform_files("sensor", "switch"), TESTS / "test_x.py": test}
    assert _platforms_used(files) == [f"{TESTS / 'test_x.py'}:2: button"]
    files = {**_platform_files("sensor"), TESTS / "test_x.py": test}
    assert _platforms_used(files) == [
        f"{TESTS / 'test_x.py'}:2: button",
        f"{TESTS / 'test_x.py'}:3: switch",
        f"{TESTS / 'test_x.py'}:4: switch",
    ]


def test_the_platform_check_passes_a_clean_cut() -> None:
    test = (
        "async def test_a(hass):\n"
        "    await hass.services.async_call('button', 'press', {}, blocking=True)\n"
        "    await hass.services.async_call('switch', 'turn_on', {})\n"
    )
    files = {**_platform_files("button", "switch"), TESTS / "test_x.py": test}
    assert _platforms_used(files) == []
    assert _platforms_used({}) == []


def test_the_platform_check_follows_helpers_through_helpers() -> None:
    helpers = (
        "async def turn_on(hass, entity_id):\n"
        "    await hass.services.async_call('switch', 'turn_on', {'entity_id': entity_id})\n"
        "async def toggle(hass, entity_id):\n"
        "    await turn_on(hass, entity_id)\n"
        "async def nurse(hass, *steps):\n"
        "    for step in steps:\n"
        "        await toggle(hass, step)\n"
        "async def unrelated(hass):\n"
        "    return hass\n"
    )
    test = (
        "from .helpers import nurse, unrelated\n"
        "async def test_a(hass):\n"
        "    await unrelated(hass)\n"
        "async def test_b(hass):\n"
        "    await nurse(hass, 'left')\n"
    )
    files = {**_platform_files("sensor"), TESTS / "helpers.py": helpers, TESTS / "test_x.py": test}
    assert _platforms_used(files) == [
        f"{TESTS / 'helpers.py'}:2: switch",
        f"{TESTS / 'helpers.py'}:4: switch",
        f"{TESTS / 'helpers.py'}:7: switch",
        f"{TESTS / 'test_x.py'}:5: switch",
    ]
    assert _platforms_used({**files, **_platform_files("switch")}) == []


def test_the_platform_check_follows_a_domain_passed_to_a_helper() -> None:
    helpers = (
        "async def call_service(hass, domain, service, data):\n"
        "    await hass.services.async_call(domain, service, data, blocking=True)\n"
    )
    test = (
        "from .helpers import call_service\n"
        "async def test_a(hass):\n"
        "    await call_service(hass, 'switch', 'turn_on', {})\n"
        "    await call_service(hass, domain='button', service='press', data={})\n"
        "    await call_service(hass, 'sensor', 'x', {})\n"
    )
    files = {**_platform_files("sensor"), TESTS / "helpers.py": helpers, TESTS / "test_x.py": test}
    assert _platforms_used(files) == [
        f"{TESTS / 'test_x.py'}:3: switch",
        f"{TESTS / 'test_x.py'}:4: button",
    ]


def test_the_platform_check_sees_the_domain_given_positionally_or_as_a_keyword() -> None:
    test = (
        "async def test_a(hass):\n"
        "    await hass.services.async_call('image', 'x')\n"
        "    await hass.services.async_call(service='x', domain='image')\n"
        "    await hass.services.call('image', 'x')\n"
        "    await hass.services.async_call(\n"
        "        'image',\n"
        "        'x',\n"
        "    )\n"
    )
    files = {**_platform_files("sensor"), TESTS / "test_x.py": test}
    assert [line.split(":")[1] for line in _platforms_used(files)] == ["2", "3", "4", "5"]


def test_the_platform_check_follows_parametrize_to_the_platform() -> None:
    test = (
        "import pytest\n"
        "@pytest.mark.parametrize(\n"
        "    ('domain', 'service'), [('switch', 'turn_on'), ('sensor', 'x')]\n"
        ")\n"
        "async def test_a(hass, domain, service):\n"
        "    await hass.services.async_call(domain, service, {})\n"
        "@pytest.mark.parametrize('service', ['turn_on', 'turn_off'])\n"
        "async def test_b(hass, service):\n"
        "    await hass.services.async_call('sensor', service, {})\n"
    )
    files = {**_platform_files("sensor"), TESTS / "test_x.py": test}
    assert _platforms_used(files) == [f"{TESTS / 'test_x.py'}:3: switch"]
    assert _platforms_used({**files, **_platform_files("switch")}) == []


def test_a_helper_in_the_test_module_counts_like_one_in_the_helpers_module() -> None:
    test = (
        "async def press(hass):\n"
        "    await hass.services.async_call('button', 'press', {})\n"
        "async def test_a(hass):\n"
        "    await press(hass)\n"
    )
    files = {**_platform_files("sensor"), TESTS / "test_x.py": test}
    assert _platforms_used(files) == [
        f"{TESTS / 'test_x.py'}:2: button",
        f"{TESTS / 'test_x.py'}:4: button",
    ]


def test_a_test_marked_for_a_later_pr_than_its_module_waits_for_that_pr(
    core_cut: ModuleType,
) -> None:
    test = (
        "async def test_a(hass):\n"
        "    await hass.services.async_call('sensor', 'x')\n"
        "\n"
        "\n"
        "# Core PR 14\n"
        "async def test_b(hass):\n"
        "    await hass.services.async_call('button', 'press', {})\n"
    )
    early = {**_platform_files("sensor"), TESTS / "test_x.py": core_cut.cut_python(test, 11)}
    assert _platforms_used(early) == []
    late = {**_platform_files("sensor"), TESTS / "test_x.py": test}
    assert _platforms_used(late) == [f"{TESTS / 'test_x.py'}:7: button"]
    both = {
        **_platform_files("sensor", "button"),
        TESTS / "test_x.py": core_cut.cut_python(test, 14),
    }
    assert _platforms_used(both) == []


def test_a_helper_marked_for_a_later_pr_leaves_no_platform_call_behind(
    core_cut: ModuleType,
) -> None:
    helpers = (
        "# Core PR 14\n"
        "async def press(hass):\n"
        "    await hass.services.async_call('button', 'press', {})\n"
        "\n"
        "\n"
        "async def other(hass):\n"
        "    await hass.services.async_call('sensor', 'x', {})\n"
    )
    cut = core_cut.cut_python(helpers, 2)
    assert _platforms_used({**_platform_files("sensor"), TESTS / "helpers.py": cut}) == []


def test_a_helper_the_cut_dropped_still_counts_for_the_tests_that_call_it() -> None:
    helpers = (
        "async def press(hass):\n"
        "    await hass.services.async_call('button', 'press', {})\n"
        "async def log(hass):\n"
        "    await hass.services.async_call('sensor', 'x', {})\n"
    )
    test = (
        "from .helpers import log, press\n"
        "async def test_a(hass):\n"
        "    await log(hass)\n"
        "async def test_b(hass):\n"
        "    await press(hass)\n"
    )
    # The cut leaves ``press`` out of the helpers but a test still calls it.
    kept = "async def log(hass):\n    await hass.services.async_call('sensor', 'x', {})\n"
    files = {**_platform_files("sensor"), TESTS / "helpers.py": kept, TESTS / "test_x.py": test}
    written = {TESTS / "helpers.py": helpers, TESTS / "test_x.py": test}
    assert _platforms_used(files) == []
    assert _platforms_used(files, written) == [f"{TESTS / 'test_x.py'}:5: button"]
    assert _platforms_used({**files, **_platform_files("button")}, written) == []


def test_the_platform_check_sees_a_helper_passed_on_as_well_as_called() -> None:
    helpers = (
        "async def toggle(hass):\n"
        "    await (turn_off if hass else turn_on)(hass)\n"
        "async def turn_on(hass):\n"
        "    await hass.services.async_call('switch', 'turn_on', {})\n"
        "async def turn_off(hass):\n"
        "    await hass.services.async_call('switch', 'turn_off', {})\n"
    )
    test = "from .helpers import toggle\nasync def test_a(hass):\n    await toggle(hass)\n"
    files = {**_platform_files("sensor"), TESTS / "helpers.py": helpers, TESTS / "test_x.py": test}
    assert _platforms_used(files) == [
        f"{TESTS / 'helpers.py'}:2: switch",
        f"{TESTS / 'helpers.py'}:4: switch",
        f"{TESTS / 'helpers.py'}:6: switch",
        f"{TESTS / 'test_x.py'}:3: switch",
    ]


def test_the_platforms_are_the_repositorys_platform_modules() -> None:
    assert _platform_modules() == {"binary_sensor", "button", "image", "sensor", "switch"}


def _side_key(value: ast.expr) -> str | None:
    """The key an f-string over ``side`` and plain text gives, with ``{side}`` left in."""
    if not isinstance(value, ast.JoinedStr):
        return None
    sides = [
        part
        for part in value.values
        if isinstance(part, ast.FormattedValue)
        and isinstance(part.value, ast.Name)
        and part.value.id == "side"
        and part.conversion == -1
        and part.format_spec is None
    ]
    text = [part for part in value.values if isinstance(part, ast.Constant)]
    if len(sides) != 1 or len(sides) + len(text) != len(value.values) or not text:
        return None
    return "".join(
        "{side}" if isinstance(part, ast.FormattedValue) else part.value for part in value.values
    )


def _key_sites(files: dict[Path, str]) -> list[tuple[Path, ast.AST, set[str] | None]]:
    """Each place a platform module of the cut sets an entity's translation key.

    With the keys it sets, or None when the check can't tell them: anything but a string
    literal, or an f-string over ``side``, and every ``_attr_translation_key`` and
    ``{"translation_key": ...}``.
    """
    sites: list[tuple[Path, ast.AST, set[str] | None]] = []
    for path, text in files.items():
        if path.parent != COMPONENT or path.suffix != ".py" or path.stem not in _platform_modules():
            continue
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, ast.keyword) and node.arg == "translation_key":
                value = node.value
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    sites.append((path, node.value, {value.value}))
                elif (side := _side_key(value)) is not None:
                    sites.append((path, value, {side.format(side=s) for s in ("left", "right")}))
                else:
                    sites.append((path, value, None))
            elif isinstance(node, ast.Dict):
                sites.extend(
                    (path, key, None)
                    for key in node.keys
                    if isinstance(key, ast.Constant) and key.value == "translation_key"
                )
            elif isinstance(node, ast.Assign | ast.AnnAssign | ast.AugAssign):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                sites.extend(
                    (path, target, None)
                    for target in targets
                    if (isinstance(target, ast.Name) and target.id == "_attr_translation_key")
                    or (
                        isinstance(target, ast.Attribute) and target.attr == "_attr_translation_key"
                    )
                )
    return sites


def _entity_keys(files: dict[Path, str]) -> set[tuple[str, str]]:
    """The (platform, key) of each ``translation_key`` an entity description of the cut gives."""
    return {(path.stem, key) for path, _, keys in _key_sites(files) for key in keys or ()}


def _unresolved_keys(files: dict[Path, str]) -> list[str]:
    """Where a platform module sets a translation key the check can't read, as ``path:line``."""
    return [
        f"{path}:{node.lineno}"
        for path, node, keys in sorted(
            _key_sites(files), key=lambda site: (str(site[0]), site[1].lineno)
        )
        if keys is None
    ]


@pytest.mark.parametrize("number", range(1, LAST_PR + 1))
def test_every_planned_cut_gives_its_entity_keys_in_a_way_the_check_reads(
    planned: Callable[[int], dict[Path, str]], number: int
) -> None:
    assert _unresolved_keys(planned(number)) == []


@pytest.mark.parametrize(
    ("source", "line"),
    [
        ("KEY = 'x'\nA = D(key='x', translation_key=KEY)\n", 2),
        ("def d(key):\n    return D(key=key, translation_key=key)\n", 2),
        ("def d(key):\n    return D(key=key, translation_key=f'{key}_time')\n", 2),
        ("def d(side):\n    return D(translation_key=f'nursing_{side}_{side}')\n", 2),
        ("def d(side):\n    return D(translation_key=f'{side}')\n", 2),
        ("def d(side):\n    return D(translation_key='a' + side)\n", 2),
        ("A = D(**{'translation_key': 'x'})\n", 1),
        ("class E:\n    _attr_translation_key = 'x'\n", 2),
        ("class E:\n    def __init__(self):\n        self._attr_translation_key = 'x'\n", 3),
        ("class E:\n    _attr_translation_key: str = 'x'\n", 2),
    ],
)
def test_the_entity_key_check_names_a_key_it_cannot_resolve(source: str, line: int) -> None:
    files = {COMPONENT / "switch.py": source}
    assert _unresolved_keys(files) == [f"{COMPONENT / 'switch.py'}:{line}"]


def test_the_entity_key_check_accepts_the_keys_it_reads() -> None:
    source = (
        "A = D(key='x', translation_key='x')\n"
        "def d(side):\n"
        "    return D(translation_key=f'nursing_{side}')\n"
        "def e(side):\n"
        "    return D(translation_key=f'last_nursing_{side}_time')\n"
        "B = D(key='y')\n"
    )
    assert _unresolved_keys({COMPONENT / "switch.py": source}) == []


def test_the_entity_key_check_looks_only_at_platform_modules() -> None:
    source = "def d(key):\n    return E(translation_key=key)\n"
    files = {COMPONENT / "writes.py": source, COMPONENT / "entity.py": source}
    assert _unresolved_keys(files) == []


def _unnamed_entities(files: dict[Path, str]) -> list[str]:
    """The entity keys the cut's platforms use that its strings.json has no name for."""
    strings = json.loads(files[COMPONENT / "strings.json"]).get("entity", {})
    return sorted(
        f"entity.{platform}.{key}"
        for platform, key in _entity_keys(files)
        if key not in strings.get(platform, {})
    )


@pytest.mark.parametrize("number", range(1, LAST_PR + 1))
def test_every_planned_cut_names_the_entities_it_creates(
    planned: Callable[[int], dict[Path, str]], number: int
) -> None:
    # In core an entity whose translation key has no string gets no name, so its entity ID is
    # not the one the tests use.
    assert _unnamed_entities(planned(number)) == []


def test_the_entity_name_check_flags_a_key_with_no_string() -> None:
    switch = (
        "A = SwitchEntityDescription(key='sleep', translation_key='sleep')\n"
        "def d(side):\n"
        "    return SwitchEntityDescription(key=side, translation_key=f'nursing_{side}')\n"
        "B = SwitchEntityDescription(key='x')\n"
    )
    strings = {"entity": {"switch": {"sleep": {"name": "Sleep"}, "nursing_left": {}}}}
    files = {
        COMPONENT / "switch.py": switch,
        COMPONENT / "entity.py": "X = dict(translation_key='baby')\n",
        COMPONENT / "strings.json": json.dumps(strings),
    }
    assert _entity_keys(files) == {
        ("switch", "sleep"),
        ("switch", "nursing_left"),
        ("switch", "nursing_right"),
    }
    assert _unnamed_entities(files) == ["entity.switch.nursing_right"]
    strings["entity"]["switch"]["nursing_right"] = {}
    files[COMPONENT / "strings.json"] = json.dumps(strings)
    assert _unnamed_entities(files) == []


def _registered_actions(files: dict[Path, str]) -> set[str]:
    """The actions the cut's services.py registers, by name."""
    if COMPONENT / "services.py" not in files:
        return set()
    names = {
        item.targets[0].id: item.value.value
        for node in ast.parse(files[COMPONENT / "const.py"]).body
        if isinstance(node, ast.ClassDef) and node.name == "BabyTrackerService"
        for item in node.body
        if isinstance(item, ast.Assign)
        and isinstance(item.targets[0], ast.Name)
        and isinstance(item.value, ast.Constant)
    }
    return {
        names[node.args[1].attr]
        for node in ast.walk(ast.parse(files[COMPONENT / "services.py"]))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "async_register"
        and isinstance(node.args[1], ast.Attribute)
    }


@pytest.mark.parametrize("number", range(1, LAST_PR + 1))
def test_every_planned_cut_registers_the_actions_it_describes(
    planned: Callable[[int], dict[Path, str]], number: int
) -> None:
    # Core rejects an action with no services.yaml entry or strings, and a described action
    # that isn't registered is a broken promise.
    files = planned(number)
    described = set(yaml.safe_load(files.get(COMPONENT / "services.yaml", "")) or {})
    strings = json.loads(files[COMPONENT / "strings.json"]).get("services", {})
    icons = json.loads(files.get(COMPONENT / "icons.json", "{}")).get("services", {})
    assert _registered_actions(files) == described
    assert set(strings) == described
    assert set(icons) == described


def test_the_registration_check_reads_the_registered_names() -> None:
    files = {
        COMPONENT / "const.py": (
            "class BabyTrackerService(StrEnum):\n"
            "    LOG_DIAPER = 'log_diaper'\n"
            "    LOG_BOTTLE = 'log_bottle'\n"
        ),
        COMPONENT / "services.py": (
            "def async_setup_services(hass):\n"
            "    hass.services.async_register(DOMAIN, BabyTrackerService.LOG_DIAPER, f)\n"
            "    hass.services.async_register(\n"
            "        DOMAIN, BabyTrackerService.LOG_BOTTLE, g, schema=S\n"
            "    )\n"
        ),
    }
    assert _registered_actions(files) == {"log_diaper", "log_bottle"}
    assert _registered_actions({}) == set()


def test_the_action_check_reads_every_service_registry_call_that_names_an_action() -> None:
    test = (
        "async def test_a(hass):\n"
        "    assert hass.services.has_service(DOMAIN, 'log_diaper')\n"
        "    assert hass.services.has_service(DOMAIN, 'undo')\n"
        "    hass.services.async_remove(DOMAIN, 'log_pump')\n"
        "    assert hass.services.supports_response(domain=DOMAIN, service='log_sleep')\n"
        "    assert not hass.services.has_service('switch', 'undo')\n"
        "    assert other.has_service(DOMAIN, 'undo')\n"
        "    assert not hass.services.has_service(DOMAIN, 'log_milestone')\n"
    )
    files = {
        COMPONENT / "services.yaml": "log_diaper:\n  fields: {}\n",
        TESTS / "test_x.py": test,
    }
    assert _actions_called(files) == [
        f"{TESTS / 'test_x.py'}:3: undo",
        f"{TESTS / 'test_x.py'}:4: log_pump",
        f"{TESTS / 'test_x.py'}:5: log_sleep",
    ]


def test_a_cut_with_no_services_yaml_has_no_actions() -> None:
    test = "async def test_a(hass):\n    await hass.services.async_call(DOMAIN, 'log_diaper')\n"
    assert _actions_called({TESTS / "test_x.py": test}) == [f"{TESTS / 'test_x.py'}:2: log_diaper"]


def test_the_field_check_ignores_the_same_words_where_no_action_gets_them() -> None:
    test = (
        "from homeassistant.components.baby_tracker.const import DOMAIN\n"
        "async def call(hass, action, baby='one', **data):\n"
        "    await hass.services.async_call(DOMAIN, action, data, blocking=True)\n"
        "async def test_a(hass, server):\n"
        "    server.add_record(baby='one')\n"
        "    await hass.services.async_call('switch', 'turn_on', {'device': 'x'})\n"
        "    await call(hass, 'log_diaper', baby='one')\n"
        "    assert issue.translation_placeholders == {'baby': 'Homer'}\n"
        "    assert 'baby' not in payload\n"
        "    await call(hass, 'log_pump', device='x')\n"
    )
    found = _fields_sent({TESTS / "test_x.py": test}, {"baby", "device"})
    assert found == [f"{TESTS / 'test_x.py'}:10: device"]


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
