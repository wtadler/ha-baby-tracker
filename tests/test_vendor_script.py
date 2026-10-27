"""The vendoring script copies the library and records where it came from."""

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "vendor_library.sh"


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _make_library(tmp_path: Path) -> Path:
    library = tmp_path / "lib"
    package = library / "src" / "pybabytracker"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("VALUE = 1\n")
    (package / "__pycache__").mkdir()
    (package / "__pycache__" / "junk.pyc").write_text("junk")
    _git(library, "init", "-q")
    _git(library, "add", ".")
    _git(library, "commit", "-q", "-m", "first")
    return library


def _run(library: Path, target: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPT), str(library)],
        cwd=ROOT,
        env={
            "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
            "VENDOR_TARGET": str(target),
        },
        capture_output=True,
        text=True,
    )


def test_copies_the_package_and_writes_the_source_commit(tmp_path: Path) -> None:
    library = _make_library(tmp_path)
    target = tmp_path / "out" / "pybabytracker"
    result = _run(library, target)
    assert result.returncode == 0, result.stderr
    assert (target / "__init__.py").read_text() == "VALUE = 1\n"
    commit = _git(library, "rev-parse", "HEAD")
    assert (target / "VENDORED_FROM").read_text().strip() == commit


def test_leaves_out_bytecode_caches(tmp_path: Path) -> None:
    library = _make_library(tmp_path)
    target = tmp_path / "out" / "pybabytracker"
    assert _run(library, target).returncode == 0
    assert not (target / "__pycache__").exists()


def test_replaces_a_previous_copy(tmp_path: Path) -> None:
    library = _make_library(tmp_path)
    target = tmp_path / "out" / "pybabytracker"
    target.mkdir(parents=True)
    (target / "stale.py").write_text("old")
    assert _run(library, target).returncode == 0
    assert not (target / "stale.py").exists()


def test_fails_when_given_no_library_path() -> None:
    result = subprocess.run(["bash", str(SCRIPT)], cwd=ROOT, capture_output=True, text=True)
    assert result.returncode != 0
    assert "usage" in result.stderr.lower()


def test_fails_when_the_path_has_no_package(tmp_path: Path) -> None:
    result = _run(tmp_path, tmp_path / "out")
    assert result.returncode != 0
    assert "src/pybabytracker" in result.stderr
