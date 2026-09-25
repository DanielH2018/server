#!/usr/bin/env python3
"""The SessionStart banner's line for a k8s role the deployer will never apply (#2570).

A hand-edited or denylisted k8s role is fast-forwarded and dropped: the defer-and-alert post
fires once per SHA, no later tick's range carries the change, and `Release Staleness Drift` is
already DOWN for any stale record anywhere in the fleet, so a new deferral adds nothing a
reader can see on that tile. Nothing pages on `k8s_unapplied` either, by construction — this
banner and the deployer's journal are the whole of its reach, which is what makes the line
below load-bearing rather than a convenience.

Every test drives `parked_deployer_problems` through its injected seams rather than patching
the module, for the reason `test_session_health_k8s_deferred.py` beside it gives.

Run: uv run pytest .claude/hooks/tests/test_session_health_k8s_unapplied.py
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
_AUTHELIA = f"{_SHA} authelia 1000.0"


def _problems(k8s_unapplied=None, k8s_deferred=None, now=1000.0):
    return _mod.parked_deployer_problems(
        list_worktrees=lambda: _WORKTREES,
        status=lambda path: "",
        read_marker=lambda: None,
        now=now,
        read_manual=lambda: None,
        read_contention=lambda: None,
        read_k8s_deferred=lambda: k8s_deferred,
        read_k8s_unapplied=lambda: k8s_unapplied,
        read_manual_tags=lambda: None,
    )


def test_an_unapplied_role_names_the_service_the_age_and_the_deploy():
    """FLAGGED half: the line has to carry the command, because no page ever will."""
    (line,) = _problems(_AUTHELIA, now=1000 + 600)
    assert "`authelia`" in line
    assert "10 min ago" in line
    assert './scripts/deploy.sh --tags "authelia"' in line
    assert "gitops_state.py clear-k8s-unapplied authelia" in line


def test_an_absent_or_garbled_marker_is_clean():
    """CLEAN half: a line this cannot parse names no service, and a deploy command naming no
    service is worse than silence."""
    assert _problems(None, now=1e9) == []
    assert _problems(f"{_SHA} authelia not-a-stamp", now=1e9) == []


def test_both_k8s_markers_report_side_by_side():
    """A host can hold a deferred bump and an unapplied role at once, and the two mean
    different things — one the tick chose, one it was never allowed to make."""
    lines = _problems(_AUTHELIA, k8s_deferred=f"{_SHA} sonarr 1000.0", now=9000.0)
    assert [("sonarr" in line) for line in lines] == [True, False], lines
    assert "`authelia`" in lines[1]
