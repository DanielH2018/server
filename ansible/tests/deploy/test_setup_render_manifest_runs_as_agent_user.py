"""test_setup_render_manifest.py must not go red on an environment gap of the session running it.

Its `--list-tasks` test parses `k3s-bringup.yml`, which resolves `community.general.ufw`. The
`claude` agent user had no collections where ansible looks, so the file failed in every fresh
fan-out worktree for a reason no change caused (#4226). Either fix closes it: install the
collections for that user, or skip when the collection is absent. Both leave the file with no
failures, which is what this checks by running it as the user running the suite.
"""

import os
import subprocess
import sys

from _helpers import REPO

_TARGET = "ansible/tests/deploy/test_setup_render_manifest.py"


def test_the_render_manifest_file_reports_no_failures_for_this_user():
    # A nested run must not inherit the outer session's per-test or worker state.
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST_")}
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            _TARGET,
            "-q",
            "-rfE",
            "-p",
            "no:cacheprovider",
        ],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=900,
    )
    # Exit 0 covers passed and skipped alike; 1 is a failed test, 2-5 an error or empty run.
    assert result.returncode == 0, (
        f"{_TARGET} exited {result.returncode} as this user:\n{result.stdout[-2000:]}"
    )
