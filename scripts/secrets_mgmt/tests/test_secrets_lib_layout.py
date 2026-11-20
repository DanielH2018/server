"""The rotation tool's two libraries live in `scripts/secrets_mgmt/secrets_lib/`.

`secret_classify` and `secret_registry` are libraries, so they move into a PEP 420 namespace
package beside the entry points, the way `deploy_tools/land_lib/` holds `land.py`'s modules.
`secret_rotation.py` stays where it is, because the cron and the root CLAUDE.md name it. The
move changes no behaviour, so these tests check where the code is and that it still runs from
there.

The tests find each library by the function it defines rather than by its file name, so the
move may keep or shorten the basenames.

Run: uv run pytest scripts/secrets_mgmt/tests/test_secrets_lib_layout.py
"""

import ast
import os
import subprocess
import sys

import pytest

from lib.repo_paths import REPO

SECRETS_MGMT = REPO / "scripts" / "secrets_mgmt"
SECRETS_LIB = SECRETS_MGMT / "secrets_lib"


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


def test_the_library_glob_reaches_its_subjects():
    assert len(sorted(SECRETS_LIB.glob("*.py"))) >= 2
