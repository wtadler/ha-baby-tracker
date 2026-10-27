#!/usr/bin/env python3
"""Cut a planned core PR out of this repository into a local Home Assistant core checkout.

Usage:
    scripts/core_cut.py <pr-number> <core-checkout>
    scripts/core_cut.py --quality-scales

The first form replaces ``homeassistant/components/nighp_baby_tracker/`` and
``tests/components/nighp_baby_tracker/`` in the checkout with everything planned for PRs 1 to
<pr-number>, then formats them with the checkout's ruff. The second regenerates
``core-prep/quality_scale/prNN.yaml``. The plan is ``core-prep/plan.yaml``.

Python files are cut by markers, so the cut is mechanical:

- A comment line ``# Core PR N`` (or ``# HACS-only``, ``# HACS only``) marks the statement right
  under it. Any comments directly above that statement go with it.
- The same marker as a trailing comment marks its line. When a statement starts on that line, it
  marks the whole statement.
- A function or class with a docstring line that starts with ``HACS-only:`` is HACS-only, and so
  is a test marked ``@pytest.mark.engine`` (it moves to the library's repository).

A statement for a later PR, or for HACS only, is left out. Markers left in are removed.

Where core needs a line of its own (core's API has moved past the oldest Home Assistant this
repository supports), a ``# Core: <code>`` comment gives it: as a line of its own it becomes
``<code>``, and as a trailing comment it replaces its line. Pair it with a ``HACS-only`` line.
"""

import argparse
import ast
import io
import json
import re
import shutil
import subprocess
import sys
import tokenize
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
INTEGRATION = ROOT / "custom_components" / "nighp_baby_tracker"
TESTS = ROOT / "tests"
CORE_PREP = ROOT / "core-prep"
DOMAIN = "nighp_baby_tracker"
COMPONENT = Path("homeassistant/components") / DOMAIN
CORE_TESTS = Path("tests/components") / DOMAIN
OWN_TEST_FOLDER = Path("components") / DOMAIN
STRICT_TYPING_LINE = f"homeassistant.components.{DOMAIN}.*"
GENERATED = {"manifest.json", "strings.json", "icons.json", "services.yaml", "quality_scale.yaml"}
HACS = "hacs"

MARKER = re.compile(r"#\s*(?:Core PR (?P<pr>\d+)\b|(?P<hacs>HACS[- ]only)\b)")
DOCSTRING_HACS = re.compile(r"^\s*HACS[- ]only:", re.MULTILINE)
CORE_LINE = re.compile(r"#\s*Core:\s?(?P<code>.*)$")
MODULE_DOCSTRING_PR = re.compile(r'^(?P<quote>"""|\'\'\')Core PRs? [^:]*: ')
IMPORT_REWRITES = (
    ("custom_components.nighp_baby_tracker.pybabytracker", "pybabytracker"),
    ("from .pybabytracker import", "from pybabytracker import"),
    ("custom_components.nighp_baby_tracker", "homeassistant.components.nighp_baby_tracker"),
    ("pytest_homeassistant_custom_component.", "pytest_homeassistant."),
)

type Owner = int | str


def load_plan() -> dict[str, Any]:
    """Return the plan in ``core-prep/plan.yaml``."""
    return yaml.safe_load((CORE_PREP / "plan.yaml").read_text())


PLAN = load_plan()


def _included(owner: Owner, pr: int) -> bool:
    return owner != HACS and int(owner) <= pr


# Python


def rewrite_imports(text: str) -> str:
    """Point imports of the integration, the vendored library, and test helpers at core's."""
    for old, new in IMPORT_REWRITES:
        text = text.replace(old, new)
    return text


def _marker_owner(comment: str) -> Owner | None:
    match = MARKER.match(comment.strip())
    if match is None:
        return None
    return HACS if match["hacs"] else int(match["pr"])


def _statements(tree: ast.AST) -> dict[int, tuple[int, int]]:
    """Map each line a statement starts on (decorators included) to its widest line range."""
    spans: dict[int, tuple[int, int]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.stmt):
            continue
        decorators = getattr(node, "decorator_list", [])
        start = min([node.lineno, *(decorator.lineno for decorator in decorators)])
        end = node.end_lineno or node.lineno
        if start not in spans or end > spans[start][1]:
            spans[start] = (start, end)
    return spans


def _comments(text: str) -> list[tuple[int, int, str]]:
    """Return each comment's line, column, and text."""
    tokens = tokenize.generate_tokens(io.StringIO(text).readline)
    return [
        (tok.start[0], tok.start[1], tok.string) for tok in tokens if tok.type == tokenize.COMMENT
    ]


def _hacs_by_definition(tree: ast.AST) -> list[int]:
    """Return the start lines of HACS-only definitions: by docstring, or engine tests."""
    starts = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            continue
        docstring = ast.get_docstring(node, clean=False) or ""
        engine = any(ast.unparse(d) == "pytest.mark.engine" for d in node.decorator_list)
        if engine or DOCSTRING_HACS.search(docstring):
            starts.append(min([node.lineno, *(d.lineno for d in node.decorator_list)]))
    return starts


def cut_python(text: str, pr: int) -> str:
    """Return the module as it is in core PR ``pr``: see the module docstring for the markers."""
    lines = text.splitlines(keepends=True)
    tree = ast.parse(text)
    spans = _statements(tree)

    def full_line_comment(number: int) -> bool:
        return 1 <= number <= len(lines) and lines[number - 1].lstrip().startswith("#")

    def core_line(number: int) -> bool:
        return full_line_comment(number) and bool(CORE_LINE.match(lines[number - 1].strip()))

    def attached_start(start: int) -> int:
        while full_line_comment(start - 1) and not core_line(start - 1):
            start -= 1
        return start

    dropped: set[int] = set()
    edits: dict[int, str] = {}

    def drop_statement(start: int) -> None:
        first, last = spans[start]
        dropped.update(range(attached_start(first), last + 1))

    for start in _hacs_by_definition(tree):
        drop_statement(start)

    for line, column, comment in _comments(text):
        if core := CORE_LINE.match(comment):
            text_line = lines[line - 1]
            indent = text_line[: len(text_line) - len(text_line.lstrip())]
            edits[line] = f"{indent}{core['code'].rstrip()}\n"
            continue
        owner = _marker_owner(comment)
        if owner is None:
            continue
        keep = _included(owner, pr)
        if full_line_comment(line) and lines[line - 1].lstrip() == lines[line - 1][column:]:
            below = line + 1
            while full_line_comment(below):
                below += 1
            if below not in spans:
                raise ValueError(f"line {line}: {comment!r} marks no statement")
            if keep:
                dropped.add(line)
            else:
                drop_statement(below)
        elif keep:
            edits[line] = lines[line - 1][:column].rstrip() + "\n"
        elif line in spans:
            drop_statement(line)
        else:
            dropped.add(line)

    out = "".join(
        edits.get(number, line)
        for number, line in enumerate(lines, start=1)
        if number not in dropped
    )
    out = MODULE_DOCSTRING_PR.sub(r"\g<quote>Tests for ", out, count=1)
    try:
        ast.parse(out)
    except SyntaxError as err:
        raise ValueError(f"the cut for PR {pr} doesn't parse: {err}") from err
    return out


# Keys


def key_owner(key: str) -> Owner:
    """Return the PR that brings a strings, icons, or services key into core."""
    matches = [entry for entry in PLAN["keys"] if key == entry or key.startswith(f"{entry}.")]
    if not matches:
        raise KeyError(f"{key} belongs to no planned PR; add it to core-prep/plan.yaml")
    return PLAN["keys"][max(matches, key=len)]


def _json(data: Any, indent: str = "") -> str:
    """Return JSON as core's formatter (prettier) leaves it: lists of plain values on one line."""
    inner = indent + "  "
    if isinstance(data, dict):
        if not data:
            return "{}"
        items = [f"{inner}{json.dumps(key)}: {_json(value, inner)}" for key, value in data.items()]
        return "{\n" + ",\n".join(items) + f"\n{indent}}}"
    if isinstance(data, list) and not any(isinstance(item, dict | list) for item in data):
        return "[" + ", ".join(json.dumps(item) for item in data) + "]"
    if isinstance(data, list):
        return "[\n" + ",\n".join(inner + _json(item, inner) for item in data) + f"\n{indent}]"
    return json.dumps(data, ensure_ascii=False)


def _subset(data: dict[str, Any], pr: int, prefix: str = "") -> dict[str, Any]:
    """Return the keys planned by PR ``pr``, sorted the way core's formatter sorts JSON."""
    out: dict[str, Any] = {}
    for key, value in sorted(data.items()):
        path = f"{prefix}{key}"
        if isinstance(value, dict):
            if kept := _subset(value, pr, f"{path}."):
                out[key] = kept
        elif _included(key_owner(path), pr):
            out[key] = value
    return out


def _set(data: dict[str, Any], path: str, value: str) -> None:
    *parents, leaf = path.split(".")
    for parent in parents:
        if parent not in data:
            return
        data = data[parent]
    if leaf in data:
        data[leaf] = value


def core_strings(pr: int) -> dict[str, Any]:
    """Return strings.json for core PR ``pr``."""
    strings = _subset(json.loads((INTEGRATION / "strings.json").read_text()), pr)
    for path, reference in PLAN["references"].items():
        _set(strings, path, reference)
    for path, override in PLAN["overrides"].items():
        if pr < override["until"]:
            _set(strings, path, override["text"])
    return strings


def core_icons(pr: int) -> dict[str, Any]:
    """Return icons.json for core PR ``pr``."""
    return _subset(json.loads((INTEGRATION / "icons.json").read_text()), pr)


def core_services(pr: int) -> str | None:
    """Return services.yaml for core PR ``pr``, or None when it has no actions yet."""
    chunks: list[list[str]] = []
    for line in (INTEGRATION / "services.yaml").read_text().splitlines():
        if re.match(r"^[a-z_]+:", line) or not chunks:
            chunks.append([])
        chunks[-1].append(line)
    kept = [
        "\n".join(chunk).rstrip("\n")
        for chunk in chunks
        if _included(key_owner(f"services.{chunk[0].split(':')[0]}"), pr)
    ]
    return "\n\n".join(kept) + "\n" if kept else None


def _at(changes: dict[int, Any], pr: int) -> Any:
    """Return the value set by the latest PR at or before ``pr``."""
    return changes[max(number for number in changes if number <= pr)]


def library_version() -> str:
    """Return the version of the vendored library, which core pins."""
    text = (INTEGRATION / "pybabytracker" / "_version.py").read_text()
    match = re.search(r'__version__ = "([^"]+)"', text)
    assert match is not None
    return match[1]


def core_manifest(pr: int) -> dict[str, Any]:
    """Return manifest.json for core PR ``pr``."""
    manifest = json.loads((INTEGRATION / "manifest.json").read_text())
    for key in ("version", "issue_tracker"):
        manifest.pop(key, None)
    planned = PLAN["manifest"]
    manifest["documentation"] = planned["documentation"]
    manifest["loggers"] = planned["loggers"]
    manifest["categories"] = planned["categories"]
    manifest["quality_scale"] = _at(planned["quality_scale"], pr)
    manifest["requirements"] = [f"pybabytracker=={library_version()}"]
    head = {key: manifest.pop(key) for key in ("domain", "name")}
    return head | dict(sorted(manifest.items()))


def _rule_status(pr: int) -> dict[str, Any]:
    return {
        rule: _at(changes, pr)
        for tier in PLAN["quality_scale"].values()
        for rule, changes in tier.items()
    }


def _scalar(text: str) -> str:
    plain = re.fullmatch(r"[A-Za-z][^:#\n]*", text) and not text.endswith(" ")
    return text if plain else json.dumps(text)


def quality_scale_yaml(pr: int) -> str:
    """Return quality_scale.yaml for core PR ``pr``, in the order of core's template."""
    out = ["rules:"]
    for number, (tier, rules) in enumerate(PLAN["quality_scale"].items()):
        if number:
            out.append("")
        out.append(f"  # {tier.capitalize()}")
        for rule, changes in rules.items():
            value = _at(changes, pr)
            if isinstance(value, str):
                out.append(f"  {rule}: {value}")
            else:
                out.append(f"  {rule}:")
                out.append(f"    status: {value['status']}")
                out.append(f"    comment: {_scalar(value['comment'])}")
    return "\n".join(out) + "\n"


def write_quality_scales() -> None:
    """Regenerate core-prep/quality_scale/prNN.yaml for every planned PR."""
    folder = CORE_PREP / "quality_scale"
    folder.mkdir(exist_ok=True)
    for pr in range(1, PLAN["prs"] + 1):
        (folder / f"pr{pr:02d}.yaml").write_text(quality_scale_yaml(pr))


# Files


def integration_file_owner(name: str) -> Owner:
    """Return the PR that brings a file of the integration into core."""
    if name not in PLAN["integration_files"]:
        raise KeyError(f"{name} belongs to no planned PR; add it to core-prep/plan.yaml")
    return PLAN["integration_files"][name]


def test_file_owner(relative: Path) -> Owner:
    """Return the PR that brings a test file into core, from the plan or its docstring."""
    if str(relative) in PLAN["test_files"]:
        return PLAN["test_files"][str(relative)]
    if relative.suffix == ".py":
        first = (TESTS / relative).read_text().split("\n", 1)[0]
        if match := re.match(r'"""Core PRs? (\d+)', first):
            return int(match[1])
        if re.match(r'"""HACS[- ]only', first):
            return HACS
    raise KeyError(f"tests/{relative} belongs to no planned PR; add it to core-prep/plan.yaml")


def _overlays(pr: int) -> dict[Path, Path]:
    """Return the overlay files for ``pr``: core-only versions, the latest PR's winning."""
    found: dict[Path, Path] = {}
    folders = sorted((CORE_PREP / "overlay").glob("pr[0-9][0-9]"))
    for folder in folders:
        if int(folder.name[2:]) > pr:
            continue
        for path in folder.rglob("*"):
            if path.is_file():
                relative = path.relative_to(folder)
                parts = relative.parts
                base = COMPONENT if parts[0] == "integration" else CORE_TESTS
                found[base.joinpath(*parts[1:])] = path
    return found


def _source(path: Path, pr: int) -> str:
    text = path.read_text()
    if path.suffix == ".py":
        try:
            text = cut_python(text, pr)
        except ValueError as err:
            raise ValueError(f"{path.relative_to(ROOT)}: {err}") from err
        text = rewrite_imports(text)
    return text


def planned_files(pr: int) -> dict[Path, str]:
    """Return every file of core PR ``pr``, by its path in the core checkout."""
    files: dict[Path, str] = {}
    for path in sorted(INTEGRATION.iterdir()):
        if path.name == "__pycache__" or path.name in GENERATED:
            continue
        if _included(integration_file_owner(path.name), pr):
            files[COMPONENT / path.name] = _source(path, pr)
    files[COMPONENT / "manifest.json"] = _json(core_manifest(pr)) + "\n"
    files[COMPONENT / "strings.json"] = _json(core_strings(pr)) + "\n"
    if icons := core_icons(pr):
        files[COMPONENT / "icons.json"] = _json(icons) + "\n"
    if (services := core_services(pr)) is not None:
        files[COMPONENT / "services.yaml"] = services
    files[COMPONENT / "quality_scale.yaml"] = quality_scale_yaml(pr)
    for path in sorted(TESTS.rglob("*")):
        if path.is_dir() or "__pycache__" in path.parts:
            continue
        relative = path.relative_to(TESTS)
        if _included(test_file_owner(relative), pr):
            # Fixtures sit where the test helpers look for an integration's: in core that's next
            # to the tests, so the integration's own folder drops out of the path.
            if relative.is_relative_to(OWN_TEST_FOLDER):
                target = relative.relative_to(OWN_TEST_FOLDER)
            else:
                target = relative
            files[CORE_TESTS / target] = _source(path, pr)
    for target, overlay in _overlays(pr).items():
        files[target] = overlay.read_text()
    return files


def _strict_typing(core: Path, wanted: bool) -> None:
    path = core / ".strict-typing"
    lines = [line for line in path.read_text().splitlines() if line != STRICT_TYPING_LINE]
    if wanted:
        components = [
            number
            for number, line in enumerate(lines)
            if line.startswith("homeassistant.components.")
        ]
        later = [number for number in components if lines[number] > STRICT_TYPING_LINE]
        if later:
            index = later[0]
        elif components:
            index = components[-1] + 1
        else:
            index = len(lines)
        lines.insert(index, STRICT_TYPING_LINE)
    path.write_text("\n".join(lines) + "\n")


def cut(pr: int, core: Path, *, ruff: str | None) -> None:
    """Replace the integration and its tests in ``core`` with the cut for PR ``pr``.

    With ``ruff``, the Python files are then sorted and formatted with the checkout's settings.
    """
    if not 1 <= pr <= PLAN["prs"]:
        raise ValueError(f"PR {pr} isn't planned: the plan has PRs 1 to {PLAN['prs']}")
    files = planned_files(pr)
    for folder in (COMPONENT, CORE_TESTS):
        shutil.rmtree(core / folder, ignore_errors=True)
    for relative, text in files.items():
        target = core / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    strict = _rule_status(pr)["strict-typing"]
    status = strict if isinstance(strict, str) else strict["status"]
    _strict_typing(core, status == "done")
    if ruff is not None:
        folders = [str(COMPONENT), str(CORE_TESTS)]
        subprocess.run(
            [ruff, "check", "--fix", "--select", "I", "--no-cache", "--quiet", *folders],
            cwd=core,
            check=True,
        )
        subprocess.run([ruff, "format", "--no-cache", "--quiet", *folders], cwd=core, check=True)


def main(argv: list[str] | None = None) -> int:
    """Run the command line."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("pr", nargs="?", type=int, help="the planned core PR to cut")
    parser.add_argument("core", nargs="?", type=Path, help="a local home-assistant/core checkout")
    parser.add_argument(
        "--quality-scales",
        action="store_true",
        help="regenerate core-prep/quality_scale/prNN.yaml and stop",
    )
    args = parser.parse_args(argv)
    if args.quality_scales:
        write_quality_scales()
        return 0
    if args.pr is None or args.core is None:
        parser.error("give a PR number and a core checkout")
    core = args.core.resolve()
    if not (core / "homeassistant" / "components").is_dir():
        parser.error(f"{core} isn't a home-assistant/core checkout")
    venv_ruff = core / ".venv" / "bin" / "ruff"
    ruff = str(venv_ruff) if venv_ruff.exists() else shutil.which("ruff")
    if ruff is None:
        parser.error("no ruff found: set up the core checkout's virtual environment first")
    cut(args.pr, core, ruff=ruff)
    print(f"Cut PR {args.pr} into {core / COMPONENT} and {core / CORE_TESTS}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
