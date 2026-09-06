#!/usr/bin/env python3
"""Tests for the SessionStart hook's parked-deployer warning (issues #1416, #1418).

A single stray file in the primary checkout `/home/ubuntu/server` makes `gitops_deploy` skip
every tick, so nothing deploys and every session's `land.sh` fails with `deploy.sh` exit 4
naming its OWN worktree. The isolation guard refuses a git command targeting the shared
checkout, so the sessions that hit the failure structurally cannot inspect its cause. This
banner is the only place that cause reaches them.

Every test drives `parked_deployer_problems` through its four injected seams rather than
patching the module, because the monkeypatch ratchet (`ansible/tests/_ratchet.py`) caps a new
test module at zero patches on a first-party module.

Run: uv run pytest .claude/hooks/tests/test_session_health_parked_deployer.py
"""

import importlib.util
import os
import subprocess

_HOOK = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "session-health.py"
)
_spec = importlib.util.spec_from_file_location("session_health", _HOOK)
assert _spec and _spec.loader, "spec_from_file_location found no loader"
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

# Two entries, the way this repo's checkout really looks: the primary first, then a session's
# worktree under .claude/worktrees/.
_WORKTREES = """worktree /home/ubuntu/server
HEAD abc1230000000000000000000000000000000000
branch refs/heads/master

worktree /home/ubuntu/server/.claude/worktrees/agent-1
HEAD def4560000000000000000000000000000000000
branch refs/heads/worktree-agent-1
"""

_DIRTY = " M ansible/tests/services/test_traefik_edge_selfcheck.py\n"


def _problems(worktrees=_WORKTREES, porcelain="", marker=None, now=0.0):
    return _mod.parked_deployer_problems(
        list_worktrees=lambda: worktrees,
        status=lambda path: porcelain,
        read_marker=lambda: marker,
        now=now,
    )


# ── the primary checkout's path: the subject this check finds by parsing, so prove it is found ──


def test_primary_worktree_path_is_the_first_entry_of_real_git_output():
    """Non-vacuity: run the real `git worktree list --porcelain` and resolve a real directory.

    This check locates its subject by parsing, so a change in git's output format (or a parser
    that silently stops matching) would return None and take the whole banner line with it —
    green, and checking nothing. Asserting against live git output is what makes that fail.
    """
    repo = _mod.REPO
    out = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    ).stdout
    primary = _mod.primary_worktree_path(out)
    assert primary, "no primary checkout resolved from live `git worktree list` output"
    assert os.path.isdir(primary)
    # The primary is the checkout this worktree hangs off, never the worktree itself.
    assert ".claude/worktrees/" not in primary
    assert os.path.realpath(repo).startswith(os.path.realpath(primary))


def test_primary_worktree_path_is_none_when_no_entry_is_named():
    assert _mod.primary_worktree_path("") is None


# ── the red-proof pair: a dirty primary is flagged, a clean one is not ──


def test_dirty_primary_checkout_is_flagged():
    lines = _problems(porcelain=_DIRTY)
    assert len(lines) == 1
    assert "/home/ubuntu/server" in lines[0]
    assert "ansible/tests/services/test_traefik_edge_selfcheck.py" in lines[0]
    # The consequence, not just the state: exit 4 names the reader's own tree, which is what
    # sent three landings for PR #1408 to the wrong repair.
    assert "exit 4" in lines[0]


def test_clean_primary_checkout_is_not_flagged():
    assert _problems(porcelain="") == []


def test_untracked_only_primary_checkout_is_flagged_with_its_code():
    """`git status --porcelain` counts untracked files, so the tree can be dirty with nothing
    modified — the case that surprised the operator on 2026-08-30."""
    lines = _problems(porcelain="?? site/index.html\n")
    assert len(lines) == 1
    assert "?? site/index.html" in lines[0]


def test_dirty_line_summarises_beyond_the_path_limit():
    porcelain = "".join(f" M file{i}.py\n" for i in range(_mod.PRIMARY_DIRTY_LIMIT + 3))
    lines = _problems(porcelain=porcelain)
    assert "+3 more" in lines[0]
    assert f"file{_mod.PRIMARY_DIRTY_LIMIT + 2}.py" not in lines[0]


# ── the red-proof pair for the park age ──


def test_behind_since_older_than_the_park_threshold_is_flagged():
    marker = "bec43b1c 0.0"
    lines = _mod.behind_park_lines(marker, _mod.BEHIND_PARK_SECONDS + 600)
    assert len(lines) == 1
    assert "park, not a queue" in lines[0]
    assert "55 min" in lines[0]


def test_behind_since_within_one_tick_is_not_flagged():
    """A routine push is behind for a tick or two. Only a stamp that survives several ticks is
    a park, so the recent case must stay silent or the banner fires on every normal merge."""
    marker = "bec43b1c 0.0"
    assert _mod.behind_park_lines(marker, _mod.BEHIND_PARK_SECONDS - 60) == []


def test_absent_behind_marker_is_not_flagged():
    assert _mod.behind_park_lines(None, 1e9) == []


def test_torn_behind_marker_is_not_flagged():
    assert _mod.behind_park_lines("not-a-timestamp", 1e9) == []


# ── failure modes: never block, but never go silently absent on a broken import ──


def test_a_failed_read_degrades_to_silence_rather_than_raising():
    def boom():
        raise OSError("state dir unreadable")

    lines = _mod.parked_deployer_problems(
        list_worktrees=lambda: _WORKTREES,
        status=lambda path: "",
        read_marker=boom,
        now=0.0,
    )
    assert lines == []


def test_a_dirty_line_survives_a_failing_marker_read():
    def boom():
        raise FileNotFoundError("behind_since")

    lines = _mod.parked_deployer_problems(
        list_worktrees=lambda: _WORKTREES,
        status=lambda path: _DIRTY,
        read_marker=boom,
        now=0.0,
    )
    assert len(lines) == 1
    assert "is dirty" in lines[0]


def test_both_lines_are_reported_together():
    lines = _problems(porcelain=_DIRTY, marker="bec43b1c 0.0", now=1e6)
    assert len(lines) == 2
