#!/usr/bin/env python3
"""Tests for the SessionStart banner's busy-service-lock line (issue #1847).

A tick that finds a service lock busy for its whole budget resets its tree and returns 0, so
`last_run` advances, `hold_sha` stays empty and only `behind_since` ages — toward a six-hour
page sized for a dirty tree. The `contention_since` marker is the durable trace of that
streak, and this line is where it reaches a session that did not start the wedged deploy.

Every test drives `parked_deployer_problems` through its injected seams rather than patching
the module, because the monkeypatch ratchet (`ansible/tests/_ratchet.py`) caps a new test
module at zero patches on a first-party module.

Run: uv run pytest .claude/hooks/tests/test_session_health_contention.py
"""

import importlib.util
import os

_HOOK = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "session-health.py"
)
_spec = importlib.util.spec_from_file_location("session_health", _HOOK)
assert _spec and _spec.loader, "spec_from_file_location found no loader"
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

_WORKTREES = """worktree /home/ubuntu/server
HEAD abc1230000000000000000000000000000000000
branch refs/heads/master
"""

_STREAK = "abc1230000000000000000000000000000000000 sonarr 1000.0 2800.0 3"


def _problems(contention=None, now=1000.0):
    return _mod.parked_deployer_problems(
        list_worktrees=lambda: _WORKTREES,
        status=lambda path: "",
        read_marker=lambda: None,
        now=now,
        read_manual=lambda: None,
        read_contention=lambda: contention,
        read_manual_tags=lambda: None,
        read_k8s_deferred=lambda: None,
        read_k8s_unapplied=lambda: None,
    )


def test_a_streak_past_the_threshold_names_the_lock_and_the_clear_command():
    """FLAGGED half: the line says which lock, how long, and the way out."""
    (line,) = _problems(_STREAK, now=1000 + _mod.CONTENTION_PARK_SECONDS + 60)
    assert "`sonarr`" in line
    assert "3 consecutive" in line
    assert "server-deploy-sonarr.lock" in line
    assert "gitops_state.py clear-contention" in line


def test_a_streak_under_the_threshold_is_clean():
    """CLEAN half: one operator deploy holding a lock for a tick is routine."""
    assert _problems(_STREAK, now=1000 + _mod.CONTENTION_PARK_SECONDS - 60) == []


def test_an_absent_or_garbled_marker_is_clean():
    assert _problems(None, now=1e9) == []
    assert _problems("abc123 sonarr not-a-stamp 1 1", now=1e9) == []


def test_a_raising_contention_read_keeps_the_lines_gathered_before_it():
    def boom():
        raise OSError("state dir unreadable")

    lines = _mod.parked_deployer_problems(
        list_worktrees=lambda: _WORKTREES,
        status=lambda path: " M CLAUDE.md\n",
        read_marker=lambda: None,
        now=1.0,
        read_manual=lambda: None,
        read_contention=boom,
        read_manual_tags=lambda: None,
        read_k8s_deferred=lambda: None,
        read_k8s_unapplied=lambda: None,
    )
    assert any("CLAUDE.md" in line for line in lines)
