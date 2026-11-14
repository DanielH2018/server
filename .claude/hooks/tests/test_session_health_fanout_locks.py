#!/usr/bin/env python3
"""Tests for how the other-live-sessions scan judges a fan-out worktree lock (#8576).

A `fanout-<batch>` lock carries no pid, so its batch unit's systemd state decides whether the
tree is live. The fake `systemctl` on `PATH` stands in for the user manager: it reports
`active` for one unit and `inactive` for every other, which is what a collected transient
unit reads.

Run: uv run pytest .claude/hooks/tests/test_session_health_fanout_locks.py
"""

import importlib.util
import os
import types
from datetime import UTC, datetime
from pathlib import Path

from lib.proc_testing import fake_bin, path_with

_HOOK = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "session-health.py"
)
_spec = importlib.util.spec_from_file_location("session_health", _HOOK)
assert _spec and _spec.loader, "spec_from_file_location found no loader"
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

_ACTIVE_UNIT = "fanout-200"
_SYSTEMCTL = f"""#!/bin/sh
case "$*" in
  *{_ACTIVE_UNIT}.service*) echo active ;;
  *) echo inactive ;;
esac
"""
# `other_live_sessions` takes no `now=`, so the lock is dated far enough back to stay past
# `lib.fanout_lock.GRACE_S` whenever the suite runs.
_LOCKED_AT = datetime(2026, 1, 1, tzinfo=UTC).timestamp()


def _result(stdout):
    return types.SimpleNamespace(stdout=stdout, stderr="", returncode=0)


def _own_session_reason():
    fields = Path("/proc/self/stat").read_text().rpartition(")")[2].split()
    return f"claude session other (pid {os.getpid()} start {fields[19]})"


def _locked_tree(root, name, reason):
    """A worktree directory whose lock file `git worktree lock` wrote two days ago."""
    tree = root / name
    gitdir = root / "gitdirs" / name
    tree.mkdir()
    gitdir.mkdir(parents=True)
    (tree / ".git").write_text(f"gitdir: {gitdir}\n")
    lock = gitdir / "locked"
    lock.write_text(reason)
    os.utime(lock, (_LOCKED_AT, _LOCKED_AT))
    return f"worktree {tree}\nHEAD bbb\nbranch refs/heads/{name}\nlocked {reason}\n\n"


def _scan(tmp_path, monkeypatch, show_all):
    stubs = fake_bin(tmp_path / "bin", systemctl=_SYSTEMCTL)
    monkeypatch.setenv("PATH", path_with(stubs))
    porcelain = "worktree /repo\nHEAD aaa\nbranch refs/heads/master\n\n" + "".join(
        [
            _locked_tree(tmp_path, "finished-batch", "fanout-100"),
            _locked_tree(tmp_path, "running-batch", _ACTIVE_UNIT),
            _locked_tree(tmp_path, "claude-session", _own_session_reason()),
        ]
    )

    def run(cmd, *_a, **_k):
        return _result(porcelain if cmd[:2] == ["git", "worktree"] else "")

    return _mod.other_live_sessions(
        "/repo", run=run, git_dirty=lambda *a, **k: False, show_all=show_all
    )


def test_the_banner_counts_live_sessions_plus_active_fanout_units(
    tmp_path, monkeypatch
):
    assert _scan(tmp_path, monkeypatch, show_all=False) == [
        "  2 other live sessions, no overlap with this branch — "
        f"`{_mod.SESSIONS_COMMAND}` lists them"
    ]


def test_the_sessions_listing_omits_a_tree_whose_fanout_unit_finished(
    tmp_path, monkeypatch
):
    lines = _scan(tmp_path, monkeypatch, show_all=True)
    assert sorted(lines) == [
        "  • claude-session — no commits yet",
        "  • running-batch — no commits yet",
    ]
