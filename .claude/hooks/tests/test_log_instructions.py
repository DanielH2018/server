#!/usr/bin/env python3
"""Tests for where `log-instructions.py` puts `instructions.log`.

The fixture repo is built under tmp_path with a linked worktree, through `lib.git_testing`,
so the caller's `GIT_*` variables and git config never reach it.

Run: uv run pytest .claude/hooks/tests/test_log_instructions.py
"""

import importlib.util
import os
import sys

from lib.git_testing import git, init_repo

_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _HERE)  # log-instructions.py imports _hook_common

_spec = importlib.util.spec_from_file_location(
    "log_instructions", os.path.join(_HERE, "log-instructions.py")
)
assert _spec and _spec.loader, "spec_from_file_location found no loader"
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


def test_a_worktree_session_logs_to_the_primary_checkout_is_flagged(tmp_path):
    primary = init_repo(tmp_path / "primary", initial_commit="init")
    worktree = tmp_path / "wt"
    git(primary, "worktree", "add", "-q", "-b", "wt", str(worktree))

    expected = os.path.join(
        os.path.realpath(primary), ".claude", "logs", "instructions.log"
    )
    assert os.path.realpath(_mod.log_path(str(worktree))) == expected
    assert os.path.realpath(_mod.log_path(str(primary))) == expected


def test_a_directory_outside_git_falls_back_beside_the_hook_is_clean(tmp_path):
    assert _mod.log_path(str(tmp_path)) == os.path.normpath(
        os.path.join(_HERE, "..", "logs", "instructions.log")
    )
