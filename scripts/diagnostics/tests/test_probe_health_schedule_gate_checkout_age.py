"""The schedule-gate test file must pass however far its checkout is behind master (#7396).

The file runs in a copy of the tree under its own pytest, because `lib.repo_paths.REPO` follows
the module's location and only a copy can sit behind an `origin/master` of its own.

Run: uv run pytest scripts/diagnostics/tests/test_probe_health_schedule_gate_checkout_age.py
"""

import os
import shutil
import subprocess
import sys

from diagnostics.probe_lib import health_checkout
from lib.git_testing import commit, git, init_repo
from lib.repo_paths import REPO

ROLE = "longhorn-restore-drill"
ROLES = "k8s/roles"
SCHEDULE_GATE_TESTS = (
    "scripts/diagnostics/tests/test_probe_health_cronjob_schedule_gate.py"
)


def _copy_behind_origin_on_the_role(dest):
    """Copy the tracked tree to `dest`, as a repo whose `origin/master` changed `ROLE` after HEAD."""
    for name in git(REPO, "ls-files", "-z").stdout.split("\0"):
        source = REPO / name
        if source.is_file() or source.is_symlink():
            (dest / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, dest / name, follow_symlinks=False)
    init_repo(dest)
    here = commit(dest, "this checkout")
    origin = commit(
        dest,
        "master changes the role",
        **{f"{ROLES}/{ROLE}/defaults/origin.yml": "---\n"},
    )
    git(dest, "update-ref", "refs/remotes/origin/master", origin)
    git(dest, "checkout", "-q", "--detach", here)


def test_schedule_gate_tests_pass_when_origin_master_changed_the_role(tmp_path):
    _copy_behind_origin_on_the_role(tmp_path)
    assert health_checkout.role_changed_on_origin(ROLE, tmp_path / ROLES) is not None

    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-n0",
            "-p",
            "no:cacheprovider",
            SCHEDULE_GATE_TESTS,
        ],
        cwd=tmp_path,
        # The parent's xdist run id would share its render cache with this copy's renders.
        env={k: v for k, v in os.environ.items() if not k.startswith("PYTEST_")},
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout
