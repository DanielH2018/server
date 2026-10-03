#!/usr/bin/env python3
"""Tests for where `log-instructions.py` puts `instructions.log`.

The fixture repo is built under tmp_path with a linked worktree. Git identity goes in through
`GIT_AUTHOR_*`/`GIT_COMMITTER_*` and signing is disabled on the command line, so no fixture
writes a git config, and every `GIT_*` var the caller carries is stripped first.

Run: uv run pytest .claude/hooks/tests/test_log_instructions.py
"""

import importlib.util
import os
import subprocess
import sys

_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _HERE)  # log-instructions.py imports _hook_common

_spec = importlib.util.spec_from_file_location(
    "log_instructions", os.path.join(_HERE, "log-instructions.py")
)
assert _spec and _spec.loader, "spec_from_file_location found no loader"
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

_GIT_ENV = {
    **{k: v for k, v in os.environ.items() if not k.startswith("GIT_")},
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.invalid",
}


def _git(*args, cwd):
    subprocess.run(
        ["git", "-c", "commit.gpgsign=false", *args],
        cwd=cwd,
        env=_GIT_ENV,
        check=True,
        capture_output=True,
    )


def test_a_worktree_session_logs_to_the_primary_checkout_is_flagged(tmp_path):
    primary = tmp_path / "primary"
    primary.mkdir()
    _git("init", "-q", cwd=primary)
    _git("commit", "-q", "--allow-empty", "-m", "init", cwd=primary)
    worktree = tmp_path / "wt"
    _git("worktree", "add", "-q", "-b", "wt", str(worktree), cwd=primary)

    expected = os.path.join(
        os.path.realpath(primary), ".claude", "logs", "instructions.log"
    )
    assert os.path.realpath(_mod.log_path(str(worktree))) == expected
    assert os.path.realpath(_mod.log_path(str(primary))) == expected


def test_a_directory_outside_git_falls_back_beside_the_hook_is_clean(tmp_path):
    assert _mod.log_path(str(tmp_path)) == os.path.normpath(
        os.path.join(_HERE, "..", "logs", "instructions.log")
    )
