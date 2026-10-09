#!/usr/bin/env python3
"""Tests for where `instructions.log` goes: `_hook_common.instructions_log_path`.

`log-instructions.py` and `inject-nested-docs.py` both append through it. The fixture repo is
built under tmp_path with a linked worktree, through `lib.git_testing`, so the caller's
`GIT_*` variables and git config never reach it.

Run: uv run pytest .claude/hooks/tests/test_log_instructions.py
"""

import os
import sys

from lib.git_testing import git, init_repo

_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _HERE)

from _hook_common import INSTRUCTIONS_LOG_ENV, instructions_log_path  # noqa: E402


def test_a_worktree_session_logs_to_the_primary_checkout_is_flagged(
    tmp_path, monkeypatch
):
    monkeypatch.delenv(INSTRUCTIONS_LOG_ENV, raising=False)
    primary = init_repo(tmp_path / "primary", initial_commit="init")
    worktree = tmp_path / "wt"
    git(primary, "worktree", "add", "-q", "-b", "wt", str(worktree))

    expected = os.path.join(
        os.path.realpath(primary), ".claude", "logs", "instructions.log"
    )
    assert os.path.realpath(instructions_log_path(str(worktree))) == expected
    assert os.path.realpath(instructions_log_path(str(primary))) == expected


def test_a_directory_outside_git_falls_back_beside_the_hook_is_clean(
    tmp_path, monkeypatch
):
    monkeypatch.delenv(INSTRUCTIONS_LOG_ENV, raising=False)
    assert instructions_log_path(str(tmp_path)) == os.path.normpath(
        os.path.join(_HERE, "..", "logs", "instructions.log")
    )


def test_the_environment_variable_redirects_the_log(tmp_path, monkeypatch):
    # The seam every hook test uses to keep its rows out of the real primary log.
    scratch = str(tmp_path / "scratch.log")
    monkeypatch.setenv(INSTRUCTIONS_LOG_ENV, scratch)
    assert instructions_log_path(str(tmp_path)) == scratch
