"""`test_uv_python.py` must pass inside a fan-out batch session (#6146).

`block-batch-full-suite` recognises a batch from the payload's `cwd` or from
`CLAUDE_PROJECT_DIR`. A test that drives `bash-pretool.py` inherits both from whatever
session runs it, so a batch that ran `.claude/hooks/tests` saw the dispatcher test refused
by the batch arm rather than measuring the rewrite.

The suite runs in a subprocess so the batch marker reaches it the way a batch session's
environment does. `CLAUDE_PROJECT_DIR` names a batch worktree that need not exist: the arm
matches the path, so this reproduces a batch on any checkout, CI's included.
"""

import os
import subprocess
import sys

HOOKS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_REPO = os.path.dirname(os.path.dirname(HOOKS))
_SUITE = os.path.join(HOOKS, "tests", "test_uv_python.py")


def test_the_uv_python_suite_passes_inside_a_fanout_batch(tmp_path):
    env = dict(os.environ)
    env["CLAUDE_PROJECT_DIR"] = str(tmp_path / ".claude" / "worktrees" / "fanout-6146")
    run = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", _SUITE],
        cwd=_REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert run.returncode == 0, run.stdout[-3000:] + run.stderr[-2000:]
