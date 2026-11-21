"""The banner's removable-worktree survey reads a cache that a detached refresh fills.

Every test points `CLAUDE_CONFIG_DIR` and `XDG_CACHE_HOME` at scratch directories, so none
reads the host's pruner or its cache.
"""

import os
import sys
import time

import pytest

from lib.proc_testing import write_exec

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hooklib import worktree_lines

BRIEF = [
    "WARN: 1 merged worktree(s) can be removed:",
    "  old-thing — worktree-old-thing merged, clean, unlocked",
]


@pytest.fixture
def cache(tmp_path, monkeypatch):
    """The survey cache path, with a stand-in pruner that prints `BRIEF`."""
    write_exec(
        tmp_path / "claude" / "hooks" / "prune-worktrees.py",
        "#!/bin/sh\n" + "".join(f"echo '{line}'\n" for line in BRIEF),
    )
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    return worktree_lines.survey_cache_path()


EPOCH = 1_795_000_000


def _write(cache, text, age_s=0):
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(text, encoding="utf-8")
    os.utime(cache, (EPOCH - age_s, EPOCH - age_s))


def _survey(started):
    return worktree_lines.stale_worktree_lines(
        "/repo",
        start=lambda argv, cwd: started.append((argv, cwd)),
        clock=lambda: EPOCH,
    )


def test_a_fresh_survey_is_read_without_starting_a_refresh(cache):
    _write(cache, "\n".join([*BRIEF, "exit 0"]))
    started = []
    assert _survey(started) == BRIEF
    assert started == []


def test_a_stale_survey_is_read_and_starts_one_locked_refresh(cache):
    _write(cache, "\n".join([*BRIEF, "exit 0"]), age_s=worktree_lines.SURVEY_TTL_S + 60)
    started = []
    assert _survey(started) == BRIEF
    [(argv, cwd)] = started
    assert argv[:3] == ["flock", "--nonblock", f"{cache}.lock"]
    assert cwd == "/repo"


def test_no_survey_yet_says_one_is_running_and_starts_it(cache):
    started = []
    [line] = _survey(started)
    assert "running in the background" in line
    assert len(started) == 1


def test_a_clean_survey_prints_nothing(cache):
    _write(cache, "exit 0\n")
    assert _survey([]) == []


def test_a_pruner_that_failed_is_named_rather_than_read_as_nothing_to_remove(cache):
    # Exit 78 is a deployed copy older than this repo needs.
    _write(cache, "this pruner is interface 1\nexit 78\n")
    assert _survey([]) == ["  ⚠ the worktree pruner failed: this pruner is interface 1"]


def test_a_host_with_no_pruner_is_named_rather_than_read_as_nothing_to_remove(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    [line] = _survey([])
    assert "no worktree pruner at" in line and "chezmoi apply" in line


def test_the_refresh_writes_the_survey_a_later_start_reads(cache, tmp_path):
    worktree_lines.stale_worktree_lines(str(tmp_path))
    deadline = time.monotonic() + 20
    while not cache.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert _survey([]) == BRIEF
