#!/usr/bin/env python3
"""Tests for where `instructions.log` goes: `_hook_common.instructions_log_path`.

`log-instructions.py` and `inject-nested-docs.py` both append through it. The fixture repo is
built under tmp_path with a linked worktree, through `lib.git_testing`, so the caller's
`GIT_*` variables and git config never reach it.

Run: uv run pytest .claude/hooks/tests/test_log_instructions.py
"""

import os
import sys
import tempfile

import pytest

from lib.git_testing import git, init_repo

_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _HERE)

from _hook_common import (  # noqa: E402
    INSTRUCTIONS_LOG_ENV,
    INSTRUCTIONS_LOG_MAX_BYTES,
    append_instructions_row,
    instructions_log_path,
)


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
    scratch = str(tmp_path / "instructions.log")
    monkeypatch.setenv(INSTRUCTIONS_LOG_ENV, scratch)
    assert instructions_log_path(str(tmp_path)) == scratch


@pytest.mark.parametrize(
    "target",
    [
        "outside/instructions.log",  # the right name outside the temp dir
        "tmp/claude-nested-docs-s1",  # a hook state file inside it
    ],
)
def test_an_override_naming_another_file_is_neither_written_nor_rotated_is_flagged(
    tmp_path, monkeypatch, target
):
    # A leaked value naming a large state file must not reach it: the appender would add a
    # row and then rename the file aside (#3805). The row lands in the normal log instead.
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path / "tmp"))
    primary = init_repo(tmp_path / "primary", initial_commit="init")
    outside = tmp_path / target
    outside.parent.mkdir()
    content = b"x" * (INSTRUCTIONS_LOG_MAX_BYTES + 1)
    outside.write_bytes(content)
    monkeypatch.setenv(INSTRUCTIONS_LOG_ENV, str(outside))

    append_instructions_row(
        "session_start", "Project", "CLAUDE.md", "s1", start=str(primary)
    )

    assert outside.read_bytes() == content
    assert not os.path.exists(str(outside) + ".1")
    log = os.path.join(primary, ".claude", "logs", "instructions.log")
    with open(log, encoding="utf-8") as fh:
        assert "session_start" in fh.read()
