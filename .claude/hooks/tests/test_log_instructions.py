#!/usr/bin/env python3
"""Tests for where `instructions.log` goes: `_hook_common.instructions_log_path`.

`log-instructions.py` and `inject-nested-docs.py` both append through it. The fixture repo is
built under tmp_path with a linked worktree, through `lib.git_testing`, so the caller's
`GIT_*` variables and git config never reach it.

Run: uv run pytest .claude/hooks/tests/test_log_instructions.py
"""

import json
import os
import re
import subprocess
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


def _thousands(n):
    """A regex matching `n` alone, written with or without thousands separators."""
    return r"(?<![\d,])" + "{:,}".format(n).replace(",", ",?") + r"(?![\d,])"


def _load(log, path, session_id):
    """Run log-instructions.py on one InstructionsLoaded payload, appending to `log`."""
    payload = {
        "hook_event_name": "InstructionsLoaded",
        "session_id": session_id,
        "cwd": str(path.parent),
        "file_path": str(path),
        "load_reason": "session_start",
        "memory_type": "Project",
    }
    subprocess.run(
        [sys.executable, os.path.join(_HERE, "log-instructions.py")],
        input=json.dumps(payload),
        text=True,
        check=True,
        timeout=30,
        env={**os.environ, INSTRUCTIONS_LOG_ENV: str(log)},
    )


def test_each_load_row_carries_the_byte_size_of_the_file_it_loaded(tmp_path):
    # #4610: nothing recorded how much instruction context a session loads. Two files of
    # different sizes, so a row carrying a running sum or the other file's size fails.
    log = tmp_path / "instructions.log"
    big = tmp_path / "big" / "CLAUDE.md"
    small = tmp_path / "small" / "CLAUDE.md"
    big.parent.mkdir()
    small.parent.mkdir()
    big.write_bytes(b"b" * 98765)
    small.write_bytes(b"s" * 4321)
    _load(log, big, "sessiona-x")
    _load(log, small, "sessiona-x")

    rows = log.read_text().splitlines()
    assert len(rows) == 2
    for row, size in zip(rows, (98765, 4321), strict=True):
        assert re.search(_thousands(size), row), row


def test_a_file_it_cannot_stat_keeps_its_row_without_a_size(tmp_path):
    # Paired with a file it can stat, so the test fails when the hook drops the unreadable
    # file's row, gives it a made-up size, or records no size at all.
    log = tmp_path / "instructions.log"
    present = tmp_path / "present" / "CLAUDE.md"
    present.parent.mkdir()
    present.write_bytes(b"p" * 4321)
    _load(log, present, "sessiona-x")
    _load(log, tmp_path / "gone" / "CLAUDE.md", "sessiona-x")

    rows = log.read_text().splitlines()
    assert len(rows) == 2, rows
    assert rows[0].endswith(" bytes=4321"), rows[0]
    assert rows[1].endswith("CLAUDE.md") and "bytes=" not in rows[1], rows[1]
