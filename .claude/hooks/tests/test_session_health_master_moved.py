#!/usr/bin/env python3
"""Tests for the SessionStart hook's behind-master warning (`master_moved_problems`).

These tests share one subject and one seam (`git`), and the module-length ratchet says
split rather than list.

Run: uv run pytest .claude/hooks
"""

import importlib.util
import os
import subprocess
import sys
import types

import pytest

_HOOK = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "session-health.py"
)
_spec = importlib.util.spec_from_file_location("session_health", _HOOK)
assert _spec and _spec.loader, "spec_from_file_location found no loader"
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


def _result(stdout, returncode=0):
    return types.SimpleNamespace(stdout=stdout, stderr="", returncode=returncode)


def _git(behind, branch=(), master=(), diff_rc=0):
    """A fake `lib.git.git`: the behind count, then the paths each side changed."""

    def run(*args, **kwargs):
        if args[0] == "rev-list":
            return _result(f"{behind}\n")
        paths = branch if args[2] == "origin/master...HEAD" else master
        return _result("\n".join(paths) + "\n", returncode=diff_rc)

    return run


# The count comes from `lib.git.git`; each test passes a fake through `master_moved_problems`' `git` seam.
def test_master_moved_silent_when_current():
    assert _mod.master_moved_problems(_git(0, ["a.py"], ["a.py"])) == []


def test_master_moved_silent_when_behind_but_master_touched_none_of_the_branch_files():
    assert _mod.master_moved_problems(_git(5, ["a.py"], ["b.py"])) == []


def test_master_moved_names_only_the_files_both_sides_changed():
    lines = _mod.master_moved_problems(
        _git(3, ["a.py", "shared.py"], ["shared.py", "c.py"])
    )
    assert len(lines) == 1
    assert "3 commits behind origin/master" in lines[0]
    assert "1 file this branch also changes: shared.py" in lines[0]
    assert "a.py" not in lines[0] and "c.py" not in lines[0]


def test_master_moved_singular_commit():
    lines = _mod.master_moved_problems(_git(1, ["x.py"], ["x.py"]))
    assert "1 commit behind" in lines[0]  # not "1 commits"


def test_master_moved_names_three_shared_files_and_counts_the_rest():
    shared = [f"f{n}.py" for n in range(5)]
    lines = _mod.master_moved_problems(_git(2, shared, shared))
    assert "f0.py, f1.py, f2.py and 2 more" in lines[0]


def test_master_moved_silent_when_a_diff_read_fails():
    assert _mod.master_moved_problems(_git(3, ["a.py"], ["a.py"], diff_rc=128)) == []


def test_master_moved_reads_this_checkout_without_fetching():
    # The hook reads the local object store, never the network, and `cwd` alone decides the tree
    # (`lib.git.git` strips `GIT_*`).
    seen = []
    _mod.master_moved_problems(lambda *a, **k: seen.append((a, k)) or _result("0\n"))
    assert seen and "fetch" not in seen[0][0]
    assert seen[0][1].get("cwd") == _mod.REPO


def test_master_moved_silent_on_nonzero_git_exit():
    # No origin/master ref is not this hook's job to diagnose; `check=False` makes that reachable,
    # because `lib.git.git` raises on a non-zero exit.
    assert _mod.master_moved_problems(lambda *a, **k: _result("", returncode=128)) == []


def test_master_moved_leaves_a_timeout_for_the_banner_to_name():
    def boom(*a, **k):
        raise subprocess.TimeoutExpired("git", 5)

    with pytest.raises(subprocess.TimeoutExpired):
        _mod.master_moved_problems(boom)


def test_master_moved_silent_on_unparseable_output():
    assert _mod.master_moved_problems(lambda *a, **k: _result("not a number\n")) == []


def test_master_moved_reports_a_broken_import_rather_than_going_quiet(monkeypatch):
    """An ImportError says so instead of dropping the section.

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
