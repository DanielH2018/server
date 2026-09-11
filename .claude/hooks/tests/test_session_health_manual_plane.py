#!/usr/bin/env python3
"""Tests for the SessionStart banner's pending-setup-role line (issue #1774).

A range carrying `roles/setup/k3s/` or `roles/setup/common/` no longer parks the deployer: the
tick fast-forwards and records the role in `/var/lib/gitops-deploy/manual_plane`. So
`behind_since` is empty, the park line says nothing, and the change sits merged and unapplied
where no session but the one that landed it is told. This line is where it reaches the rest.

Every test drives `parked_deployer_problems` through its injected seams rather than patching
the module, because the monkeypatch ratchet (`ansible/tests/_ratchet.py`) caps a new test
module at zero patches on a first-party module.

Run: uv run pytest .claude/hooks/tests/test_session_health_manual_plane.py
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

# What the deployer writes for the two roles that can reach this marker: k3s is applied by
# k3s-bringup.yml, common by no playbook at all.
_K3S = "abc1230000000000000000000000000000000000 ansible/k3s-bringup.yml k3s 1000"
_COMMON = "beef1230000000000000000000000000000000000 none common 2000"


def _problems(manual=None, marker=None, now=1000.0):
    """The banner lines, with a clean primary checkout and no park unless one is passed."""
    return _mod.parked_deployer_problems(
        list_worktrees=lambda: _WORKTREES,
        status=lambda path: "",
        read_marker=lambda: marker,
        now=now,
        read_manual=lambda: manual,
    )


def test_a_pending_role_is_flagged_with_its_playbook_and_the_clear_command():
    (line,) = _problems(manual=_K3S, now=1000 + 3 * 3600)
    assert "`k3s`" in line
    assert "ansible/k3s-bringup.yml" in line
    assert "3h" in line
    assert "gitops_state.py clear-manual-plane k3s" in line, (
        "the way out belongs in the line: the session reading it is usually not the "
        "session that landed the change"
    )


def test_a_role_no_playbook_applies_still_says_what_to_do():
    (line,) = _problems(manual=_COMMON, now=2000 + 600)
    assert "`common`" in line
    assert "apply the role by hand" in line
    assert "none" not in line, "the marker's placeholder is not an instruction"
    assert "10 min" in line


def test_every_pending_role_gets_its_own_line_oldest_first():
    lines = _problems(manual=f"{_COMMON}\n{_K3S}", now=3000.0)
    assert len(lines) == 2
    assert "`k3s`" in lines[0] and "`common`" in lines[1]


def test_a_freshly_recorded_role_is_still_flagged():
    """Not age-gated, unlike the park line: the tick never records a routine role here."""
    assert _problems(manual=_K3S, now=1000.0)


def test_an_empty_marker_is_clean():
    assert _problems(manual=None) == []
    assert _problems(manual="") == []


def test_a_garbled_marker_is_clean():
    """The must-not-fire half: a line naming no parsable role names nothing to clear."""
    assert _problems(manual="three fields only\nsha book role later") == []


def test_a_park_and_a_pending_role_are_both_reported():
    """Two deferrals of the same tick; a host can be in both, and the park is named first."""
    park = f"abc1230000000000000000000000000000000000 {1000 - _mod.BEHIND_PARK_SECONDS * 2}"
    lines = _problems(manual=_K3S, marker=park, now=1000.0)
    assert len(lines) == 2
    assert "not fast-forwarded" in lines[0]
    assert "`k3s`" in lines[1]


def test_a_raising_manual_read_does_not_take_the_dirty_line_with_it():
    """A SessionStart banner must never block a session; the lines already gathered survive."""

    def boom():
        raise OSError("state dir exploded")

    lines = _mod.parked_deployer_problems(
        list_worktrees=lambda: _WORKTREES,
        status=lambda path: " M ansible/tests/deploy/test_x.py\n",
        read_marker=lambda: None,
        now=0.0,
        read_manual=boom,
    )
    assert len(lines) == 1 and "primary checkout" in lines[0]
