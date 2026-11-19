"""`lib.snapshot_uv` against a real `uv`, in a scratch `package = true` project.

The repo's own project is `package = false`, which installs no `.pth` for the helper to
protect, so each test builds a caller and a snapshot of it that share the caller's venv the
way `land_lib.tools.gate_snapshot` shares the primary's. `UV_OFFLINE` holds: uv builds a
`uv_build` project with its own built-in backend, so nothing is downloaded.

Run: uv run pytest scripts/lib/tests/test_snapshot_uv.py
"""

import os
import shutil
from pathlib import Path

import pytest

from lib import snapshot_uv
from lib.proc_testing import run

PYPROJECT = """\
[project]
name = "scratch"
version = "0"
requires-python = ">=3.11"

[build-system]
requires = ["uv_build"]
build-backend = "uv_build"

[tool.uv]
package = true

[tool.uv.build-backend]
module-root = "scripts"
module-name = "lib"
namespace = true
"""

# The running session's settings that would point uv at another project or venv.
PROJECT_VARS = frozenset(
    {
        "VIRTUAL_ENV",
        "UV_PROJECT",
        "UV_PROJECT_ENVIRONMENT",
        "UV_NO_SYNC",
        "UV_FROZEN",
        "UV_LOCKED",
        "UV_NO_INSTALL_PROJECT",
        "UV_NO_EDITABLE",
        "PYTHONPATH",
    }
)
WHO = ("python", "-c", "import lib.who; print(lib.who.WHO)")


def _tree(root: Path, who: str) -> Path:
    (root / "scripts" / "lib").mkdir(parents=True)
    (root / "pyproject.toml").write_text(PYPROJECT)
    (root / "scripts" / "lib" / "who.py").write_text(f"WHO = {who!r}\n")
    return root


@pytest.fixture
def scratch(tmp_path):
    """(caller, snapshot, env, the caller's `.pth`), the caller synced into its own venv."""
    caller = _tree(tmp_path / "caller", "caller")
    snapshot = _tree(tmp_path / "snapshot", "snapshot")
    env = {k: v for k, v in os.environ.items() if k not in PROJECT_VARS}
    env |= {"UV_OFFLINE": "1", "UV_PROJECT_ENVIRONMENT": str(caller / ".venv")}
    run(["uv", "sync", "--quiet"], cwd=caller, env=env, check=True)
    shutil.copy(caller / "uv.lock", snapshot / "uv.lock")
    [pth] = (caller / ".venv").glob("lib/python*/site-packages/*.pth")
    assert str(caller / "scripts") in pth.read_text()
    return caller, snapshot, env, pth


@pytest.mark.parametrize("sync", [True, False])
def test_a_run_through_the_helper_imports_the_snapshot_and_leaves_the_pth_alone(
    scratch, sync
):
    _, snapshot, env, pth = scratch
    before = pth.read_bytes()
    done = run(snapshot_uv.argv(snapshot, *WHO, sync=sync), cwd=snapshot, env=env)
    assert (done.returncode, done.stdout.strip()) == (0, "snapshot"), done.stderr
    assert pth.read_bytes() == before


def test_a_plain_uv_run_from_the_snapshot_repoints_the_callers_pth(scratch):
    _, snapshot, env, pth = scratch
    before = pth.read_bytes()
    run(["uv", "run", *WHO], cwd=snapshot, env=env, check=True)
    assert pth.read_bytes() != before
    assert str(snapshot / "scripts") in pth.read_text()


def test_a_failed_sync_runs_nothing(scratch):
    _, snapshot, env, _ = scratch
    (snapshot / "uv.lock").unlink()
    done = run(snapshot_uv.argv(snapshot, "echo", "ran"), cwd=snapshot, env=env)
    assert done.returncode != 0
    assert "ran" not in done.stdout
