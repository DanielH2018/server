#!/usr/bin/env python3
"""Exit 77 carries the snapshot command's own stderr (issue #2094).

`make_snapshot` ran `git worktree add --detach ... >/dev/null 2>&1`, so a run that could not
snapshot printed only the snapshot root and a guess at what to check. On 2026-09-19 two
landings released from the tick's tree lock in the same second, one snapshot failed, and
the swallowed stderr left "a `.git/worktrees` lock collision" a hypothesis. The message now
carries the `fatal:` line, and this pair proves the capture reaches it.

Run: uv run pytest scripts/deploy_tools/tests/test_deploy_snapshot_failure_names_its_cause.py
"""

import os
import subprocess
from pathlib import Path

import pytest

from _deploy_sh_fakes import (
    FAKE_RECAP,
    FLOCK_STUB,
    UV_DEPLOY_LOCKS_ARM,
    deploy_sh_env,
    make_snapshot_repo,
)

_REPO = Path(__file__).resolve().parents[3]
_DEPLOY_SH = _REPO / "scripts" / "deploy.sh"
_SNAPSHOT_FAILED = 77

_UV_STUB = """#!/bin/bash
case "$*" in
  *ansible-playbook*) {recap}; exit 0 ;;
{locks}
  *) exit 0 ;;
esac
""".replace("{recap}", FAKE_RECAP).replace("{locks}", UV_DEPLOY_LOCKS_ARM)


def _run(tmp_path: Path, snapshot_root_mode: int) -> subprocess.CompletedProcess:
    """A scoped run of deploy.sh whose snapshot root already exists with `snapshot_root_mode`."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "flock").write_text(FLOCK_STUB)
    (bin_dir / "uv").write_text(_UV_STUB)
    for stub in ("flock", "uv"):
        (bin_dir / stub).chmod(0o755)
    repo = make_snapshot_repo(tmp_path / "repo")
    env = deploy_sh_env(tmp_path, bin_dir)
    root = Path(env["HOMELAB_DEPLOY_SNAPSHOT_ROOT"])
    root.mkdir()
    root.chmod(snapshot_root_mode)
    try:
        return subprocess.run(
            [
                str(_DEPLOY_SH),
                "--tags",
                "x",
                "--skip-tag-check",
                "--skip-staleness-check",
            ],
            cwd=repo,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    finally:
        # pytest's tmp_path cleanup cannot remove what it cannot write.
        root.chmod(0o755)


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes into a 0555 directory")
def test_an_unwritable_snapshot_root_names_the_add_that_refused(tmp_path):
    """RED half: git's own `fatal:` line reaches the exit-77 message."""
    result = _run(tmp_path, 0o555)
    assert result.returncode == _SNAPSHOT_FAILED, result.stderr
    assert "nothing was deployed" in result.stderr
    assert "fatal:" in result.stderr, result.stderr
    # The guess the message used to make is gone: the cause above is what to fix.
    assert "Check the directory is writable" not in result.stderr


def test_a_writable_snapshot_root_carries_no_fatal_line(tmp_path):
    """Clean half: a run that snapshots prints no captured stderr at all."""
    result = _run(tmp_path, 0o755)
    assert result.returncode == 0, result.stderr
    assert "fatal:" not in result.stderr
    assert "could not snapshot" not in result.stderr
