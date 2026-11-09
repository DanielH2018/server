#!/usr/bin/env python3
"""Every timestamp that names a deploy artifact reads UTC.

`deploy.sh`'s Python halves name its snapshot worktree and its `--detach` log after a stamp.
A stamp in host-local time is ambiguous across a DST fold; UTC makes it independent of where
the script runs. Both hosts run `Etc/UTC` today, so this guards a future zone change rather
than a live defect.

Run: uv run pytest scripts/deploy_tools/tests/test_deploy_stamps_are_utc.py
"""

import re
from pathlib import Path

import pytest

from lib.repo_paths import REPO

# deploy.sh stamps its snapshot and its --detach log in Python.
_PYTHON_STAMPERS = (
    REPO / "scripts" / "deploy_tools" / "deploy_lib" / "under_locks.py",
    REPO / "scripts" / "deploy_tools" / "deploy_lib" / "detach_run.py",
)
_NOW_CALL = re.compile(r"\bdatetime\.now\(([^)]*)\)")


@pytest.mark.parametrize("path", _PYTHON_STAMPERS, ids=lambda p: p.name)
def test_every_python_stamp_is_utc(path: Path):
    calls = _NOW_CALL.findall(path.read_text())
    assert calls, f"{path.name}: found no datetime.now() call to check"
    assert all(arg == "UTC" for arg in calls), f"datetime.now() without UTC: {calls}"


def test_the_python_pattern_flags_a_local_time_stamp():
    assert _NOW_CALL.findall("datetime.now().strftime('%Y')") == [""]
