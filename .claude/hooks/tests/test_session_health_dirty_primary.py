#!/usr/bin/env python3
"""Tests for the SessionStart hook's dirty-primary warning.

A single stray file in the primary checkout `/home/ubuntu/server` makes `gitops_deploy` skip
every tick, so nothing deploys and every session's `land.sh` fails with `deploy.sh` exit 4
naming its OWN worktree. The isolation guard refuses a git command targeting the shared
checkout, so the sessions that hit the failure structurally cannot inspect its cause. This
banner is the only place that cause reaches them.

Every test drives `dirty_primary_problems` through its injected seams rather than
patching the module, because the monkeypatch ratchet (`ansible/tests/_ratchet.py`) caps a new
test module at zero patches on a first-party module.

Run: uv run pytest .claude/hooks/tests/test_session_health_dirty_primary.py
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

# The primary checkout the dirty-primary read is pointed at.
_PRIMARY = "/home/ubuntu/server"

_DIRTY = " M ansible/tests/services/test_traefik_edge_selfcheck.py\n"


def _problems(porcelain=""):
    return _mod.dirty_primary_problems(
        primary=lambda: _PRIMARY, status=lambda path: porcelain
    )


# ── the red-proof pair: a dirty primary is flagged, a clean one is not ──


def test_dirty_primary_checkout_is_flagged():
    lines = _problems(porcelain=_DIRTY)
    assert len(lines) == 1
    assert "/home/ubuntu/server" in lines[0]
    assert "ansible/tests/services/test_traefik_edge_selfcheck.py" in lines[0]
    # The consequence, not just the state: exit 4 names the reader's own tree.
    assert "exit 4" in lines[0]


def test_clean_primary_checkout_is_not_flagged():
    assert _problems(porcelain="") == []


def test_untracked_only_primary_checkout_is_flagged_with_its_code():
    """`git status --porcelain` counts untracked files, so the tree can be dirty with nothing
    modified."""
    lines = _problems(porcelain="?? site/index.html\n")
    assert len(lines) == 1
    assert "?? site/index.html" in lines[0]


def test_dirty_line_summarises_beyond_the_path_limit():
    porcelain = "".join(f" M file{i}.py\n" for i in range(_mod.PRIMARY_DIRTY_LIMIT + 3))
    lines = _problems(porcelain=porcelain)
    assert "+3 more" in lines[0]
    assert f"file{_mod.PRIMARY_DIRTY_LIMIT + 2}.py" not in lines[0]


def test_a_failed_status_read_degrades_to_silence_rather_than_raising():
    def boom(path):
        raise OSError("git unreadable")

    assert _mod.dirty_primary_problems(primary=lambda: _PRIMARY, status=boom) == []


def test_the_real_status_read_takes_no_optional_locks_on_the_shared_checkout():
    """The default `status` seam must pass `--no-optional-locks`, before the subcommand.

    A plain `git status` refreshes and WRITES `.git/index` in the tree it reads. This hook reads
    the SHARED primary checkout from inside a worktree session, so without the flag it would
    write the one tree the isolation guard exists to keep a worktree session out of, and could
    collide on `index.lock` with the deployer's own `merge --ff-only`. This asserts the real
    default rather than an injected fake, because the flag is exactly what a fake would hide.
    """
    argv = _mod.PRIMARY_STATUS_ARGV
    assert argv[0] == "--no-optional-locks", f"a lock-taking status read: {argv}"
    assert argv[1] == "status", (
        "the flag is global to git and must precede the subcommand"
    )
    assert "--porcelain" in argv
