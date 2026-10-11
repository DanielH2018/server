"""The rotation tool's two libraries live in `scripts/secrets_mgmt/secrets_lib/` (#4350).

`secret_classify` and `secret_registry` are libraries, so they move into a PEP 420 namespace
package beside the entry points, the way `deploy_tools/land_lib/` holds `land.py`'s modules.
`secret_rotation.py` and `secret_bearing_host_paths.py` stay where they are, because the cron
and the root CLAUDE.md name them. The move changes no behaviour, so these tests check where
the code is, who still reaches for the old location, and that the code still runs from there.

The tests find each library by the function it defines rather than by its file name, so the
move may keep or shorten the basenames.

Run: uv run pytest scripts/secrets_mgmt/tests/test_secrets_lib_layout.py
"""

import ast
import os
import subprocess
import sys
from pathlib import Path

import pytest

from lib.repo_paths import REPO

SECRETS_MGMT = REPO / "scripts" / "secrets_mgmt"
SECRETS_LIB = SECRETS_MGMT / "secrets_lib"
ENTRYPOINTS = {"secret_rotation.py", "secret_bearing_host_paths.py"}
OLD_MODULES = {"secret_classify", "secret_registry"}
OLD_PATHS = tuple(f"scripts/secrets_mgmt/{m}.py" for m in sorted(OLD_MODULES))


def _tracked(*pathspec: str) -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "-z", "--", *pathspec],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    ).stdout
    return [p for p in out.split("\0") if p]


def _lib_file_defining(func: str):
    """The module file under secrets_lib/ that defines `func` at top level, or None."""
    if not SECRETS_LIB.is_dir():
        return None
    for path in sorted(SECRETS_LIB.glob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        if any(isinstance(n, ast.FunctionDef) and n.name == func for n in tree.body):
            return path
    return None


def _env_without_pythonpath() -> dict:
    return {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}


def test_libraries_are_tracked_in_secrets_lib_without_an_init_file():
    lib = set(_tracked("scripts/secrets_mgmt/secrets_lib"))
    classify_file = _lib_file_defining("classify")
    registry_file = _lib_file_defining("due_date")
    assert classify_file is not None, "no secrets_lib module defines classify()"
    assert registry_file is not None, "no secrets_lib module defines due_date()"
    assert str(classify_file.relative_to(REPO)) in lib
    assert str(registry_file.relative_to(REPO)) in lib
    assert "scripts/secrets_mgmt/secrets_lib/__init__.py" not in lib


def test_only_the_two_entrypoints_keep_the_secret_prefix_at_the_top_level():
    top = {
        p.rsplit("/", 1)[1]
        for p in _tracked("scripts/secrets_mgmt/secret_*.py")
        if p.count("/") == 2
    }
    assert top == ENTRYPOINTS


def _old_location_imports(source: str) -> list[str]:
    hits = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module in {f"secrets_mgmt.{m}" for m in OLD_MODULES} | OLD_MODULES:
                hits.append(node.module)
            elif node.module == "secrets_mgmt":
                hits += [
                    f"secrets_mgmt.{a.name}"
                    for a in node.names
                    if a.name in OLD_MODULES
                ]
        elif isinstance(node, ast.Import):
            for a in node.names:
                if a.name in {f"secrets_mgmt.{m}" for m in OLD_MODULES} | OLD_MODULES:
                    hits.append(a.name)
    return hits


def test_no_tracked_module_imports_a_library_from_its_old_location():
    offenders = {}
    for rel in _tracked("*.py"):
        path = REPO / rel
        if not path.is_file():
            continue
        hits = _old_location_imports(path.read_text())
        if hits:
            offenders[rel] = hits
    assert offenders == {}


def test_no_hand_written_file_names_an_old_library_path():
    me = str(Path(__file__).resolve().relative_to(REPO))
    offenders = []
    for rel in _tracked():
        if rel == me or rel.startswith("docs/reference/"):
            continue
        if rel == "scripts/dev/pytest_shard_weights.json":
            continue
        path = REPO / rel
        if not path.is_file():
            continue
        try:
            text = path.read_text()
        except UnicodeDecodeError:
            continue
        if "generated_from:" in text:
            continue
        offenders += [f"{rel}: {old}" for old in OLD_PATHS if old in text]
    assert offenders == []


@pytest.mark.parametrize("func", ["classify", "due_date"])
def test_each_library_imports_when_run_directly_from_another_directory(func, tmp_path):
    # A directly-run file gets only its own directory on sys.path, so a library one level
    # deeper than before needs its own bootstrap to reach `secrets_mgmt` and `lib`.
    path = _lib_file_defining(func)
    assert path is not None, f"no secrets_lib module defines {func}()"
    proc = subprocess.run(
        [sys.executable, str(path)],
        cwd=tmp_path,
        env=_env_without_pythonpath(),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr


@pytest.mark.parametrize("where", ["repo_root", "elsewhere"])
def test_secret_rotation_sync_help_runs_on_the_moved_registry(where, tmp_path):
    cwd = REPO if where == "repo_root" else tmp_path
    proc = subprocess.run(
        [
            sys.executable,
            "-X",
            "importtime",
            str(SECRETS_MGMT / "secret_rotation.py"),
            "sync",
            "--help",
        ],
        cwd=cwd,
        env=_env_without_pythonpath(),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    imported = {
        line.rsplit("|", 1)[-1].strip()
        for line in proc.stderr.splitlines()
        if "|" in line
    }
    assert any(
        ".secrets_lib." in m or m.startswith("secrets_lib.") for m in imported
    ), sorted(m for m in imported if "secret" in m)


def test_the_scans_above_reach_their_subjects():
    # Each scan above passes vacuously on an empty set: a glob over a renamed directory, or a
    # `git ls-files` run that matched nothing. Floor them so a move cannot empty them silently.
    assert len(sorted(SECRETS_LIB.glob("*.py"))) >= 2
    assert "scripts/secrets_mgmt/secret_rotation.py" in _tracked("*.py")
    assert len(_tracked()) >= 1000
