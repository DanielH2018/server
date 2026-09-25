#!/usr/bin/env python3
"""Tests for the SessionStart banner's deferred-image-bump line (issue #2470).

A BROAD tick returns before the k8s arm runs, so it merges a promoted image bump and never
applies it. `behind_since` is empty on that path, the defer-and-alert post fires once, and no
later tick's range carries the bump again — `k8s_deferred` is the only durable record.
monitor-bridge read it from the day it existed (#2449); this line is where it reaches the
session that can clear it with one deploy.

Every test drives `parked_deployer_problems` through its injected seams rather than patching
the module, because the monkeypatch ratchet (`ansible/tests/_ratchet.py`) caps a new test
module at zero patches on a first-party module.

Run: uv run pytest .claude/hooks/tests/test_session_health_k8s_deferred.py
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

_SHA = "abc1230000000000000000000000000000000000"
_SONARR = f"{_SHA} sonarr 1000.0"


def _problems(k8s_deferred=None, now=1000.0):
    return _mod.parked_deployer_problems(
        list_worktrees=lambda: _WORKTREES,
        status=lambda path: "",
        read_marker=lambda: None,
        now=now,
        read_manual=lambda: None,
        read_contention=lambda: None,
        read_k8s_deferred=lambda: k8s_deferred,
    )


def test_a_deferred_bump_names_the_service_the_deploy_and_the_clear():
    """FLAGGED half: the line says which service, how long, and both halves of the way out."""
    (line,) = _problems(_SONARR, now=1000 + 600)
    assert "`sonarr`" in line
    assert "10 min ago" in line
    assert './scripts/deploy.sh --tags "sonarr"' in line
    assert "gitops_state.py clear-k8s-deferred sonarr" in line


def test_a_bump_deferred_minutes_ago_is_already_reported():
    """The ungated half of the DECIDED note in `deployer_park.k8s_deferred_lines`.

    monitor-bridge gates this marker at seven hours because it pages. The banner does not,
    for the reason `manual_plane_lines` does not: a bump merged one tick ago is exactly the
    one this session can still clear cheaply, and a passive line costs nothing if the next
    tick clears it first.
    """
    assert _problems(_SONARR, now=1000 + 60) != []


def test_every_pending_bump_gets_a_line_oldest_first():
    marker = f"{_SHA} radarr 2000.0\n{_SONARR}\n"
    lines = _problems(marker, now=9000.0)
    assert [("sonarr" in line) for line in lines] == [True, False], lines
    assert "`radarr`" in lines[1]


def test_an_absent_or_garbled_marker_is_clean():
    """A line this cannot parse is skipped, never guessed at: a deploy command naming no
    service is worse than silence."""
    assert _problems(None, now=1e9) == []
    assert _problems(f"{_SHA} sonarr not-a-stamp", now=1e9) == []
    assert _problems("garbage", now=1e9) == []


def test_a_raising_deferred_read_keeps_the_lines_gathered_before_it():
    def boom():
        raise OSError("state dir unreadable")

    lines = _mod.parked_deployer_problems(
        list_worktrees=lambda: _WORKTREES,
        status=lambda path: " M CLAUDE.md\n",
        read_marker=lambda: None,
        now=1.0,
        read_manual=lambda: None,
        read_contention=lambda: None,
        read_k8s_deferred=boom,
    )
    assert any("CLAUDE.md" in line for line in lines)
