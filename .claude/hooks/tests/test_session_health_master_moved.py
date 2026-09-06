#!/usr/bin/env python3
"""Tests for the SessionStart hook's behind-master warning (`master_moved_problems`).

Split out of test_session_health.py when issue #1306's rejecting half pushed that file past
the 500-line test cap: these seven tests share one subject and one patch target, and the
module-length ratchet says split rather than list.

Run: uv run pytest .claude/hooks
"""

import importlib.util
import os
import subprocess
import sys
import types

_HOOK = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "session-health.py"
)
_spec = importlib.util.spec_from_file_location("session_health", _HOOK)
assert _spec and _spec.loader, "spec_from_file_location found no loader"
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


def _result(stdout, returncode=0):
    return types.SimpleNamespace(stdout=stdout, stderr="", returncode=returncode)


# Issue #1263: the count comes from `lib.git.git`, patched by STRING target (see `git_dirty`).
def test_master_moved_silent_when_current(monkeypatch):
    monkeypatch.setattr("lib.git.git", lambda *a, **k: _result("0\n"))
    assert _mod.master_moved_problems() == []


def test_master_moved_reports_commit_count(monkeypatch):
    monkeypatch.setattr("lib.git.git", lambda *a, **k: _result("3\n"))
    lines = _mod.master_moved_problems()
    assert len(lines) == 1
    assert "3 commits behind origin/master" in lines[0]


def test_master_moved_singular_commit(monkeypatch):
    monkeypatch.setattr("lib.git.git", lambda *a, **k: _result("1\n"))
    lines = _mod.master_moved_problems()
    assert "1 commit behind" in lines[0]  # not "1 commits"


def test_master_moved_reads_this_checkout_without_fetching(monkeypatch):
    # Two properties off one call, because the monkeypatch ratchet caps this file: the hook
    # reads the local object store, never the network, and `cwd` names this checkout (which is
    # load-bearing — `lib.git.git` strips `GIT_*`, so `cwd` alone decides the tree it reads).
    seen = []
    monkeypatch.setattr(
        "lib.git.git", lambda *a, **k: seen.append((a, k)) or _result("0\n")
    )
    _mod.master_moved_problems()
    assert seen and "fetch" not in seen[0][0]
    assert seen[0][1].get("cwd") == _mod.REPO


def test_master_moved_silent_on_nonzero_git_exit(monkeypatch):
    # No origin/master ref in this checkout at all, say — not this hook's job to diagnose.
    # `check=False` is what makes this reachable: `lib.git.git` raises on a non-zero exit.
    monkeypatch.setattr("lib.git.git", lambda *a, **k: _result("", returncode=128))
    assert _mod.master_moved_problems() == []


def test_master_moved_silent_on_timeout(monkeypatch):
    def boom(*a, **k):
        raise subprocess.TimeoutExpired("git", 5)

    monkeypatch.setattr("lib.git.git", boom)
    assert _mod.master_moved_problems() == []


def test_master_moved_silent_on_unparseable_output(monkeypatch):
    monkeypatch.setattr("lib.git.git", lambda *a, **k: _result("not a number\n"))
    assert _mod.master_moved_problems() == []


def test_master_moved_reports_a_broken_import_rather_than_going_quiet(monkeypatch):
    """Issue #1306's rejecting half: an ImportError says so instead of dropping the section.

    The git READ still fails silently (the six tests above); only the import is loud, the way
    `other_live_sessions` is. `sys.modules[name] = None` is what makes `from lib.git import
    git` raise ImportError after the module has already been imported for real.
    """
    monkeypatch.setitem(sys.modules, "lib.git", None)
    lines = _mod.master_moved_problems()
    assert lines, (
        "a failed import returned no lines — indistinguishable from 'this branch is current'"
    )
    assert "behind-master detection is broken" in lines[0]
