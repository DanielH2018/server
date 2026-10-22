#!/usr/bin/env python3
"""Tests for the SessionStart hook's other-live-sessions and unlabelled-worktree lines.

They share the `run` seam that `other_live_sessions` and `unlabelled_worktree_lines` take,
and moved out of test_session_health.py when it hit the module-length cap.

Run: uv run pytest .claude/hooks
"""

import importlib.util
import os
import subprocess
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


_OTHER_WORKTREE_PORCELAIN = (
    "worktree /repo\nHEAD aaa\nbranch refs/heads/master\n\n"
    "worktree /repo/.claude/worktrees/other\nHEAD bbb\nbranch refs/heads/feature\n"
    "locked some-other-session\n\n"
)


def _fake_run_for_other_sessions(diff_stdout):
    def fake(cmd, *_args, **_kwargs):
        return _result(
            _OTHER_WORKTREE_PORCELAIN if cmd[:2] == ["git", "worktree"] else diff_stdout
        )

    return fake


# The per-tree dirty check delegates to `lib.git.git_dirty`, not its own `git status
# --porcelain` read. Pins "(+ uncommitted)" to follow git_dirty's answer, including a
# git_dirty failure (tree vanished mid-scan) degrading to not-dirty rather than crashing the
# section. `run` and `git_dirty` are `other_live_sessions`' own seams.
def test_other_live_sessions_dirty_marker_follows_lib_git():
    def scan(git_dirty):
        run = _fake_run_for_other_sessions("file.py\n")
        return _mod.other_live_sessions("/repo", run=run, git_dirty=git_dirty)[0]

    assert "(+ uncommitted)" in scan(lambda *a, **k: True)
    assert "(+ uncommitted)" not in scan(lambda *a, **k: False)

    def boom(*a, **k):
        raise subprocess.CalledProcessError(128, ["git", "status"])

    assert "(+ uncommitted)" not in scan(boom)


def test_other_live_sessions_shows_a_trees_label_beside_its_name(tmp_path):
    tree = tmp_path / "agent-a1"
    tree.mkdir()
    (tree / ".claim").write_text("#1345 Fix the cert watcher\n")
    porcelain = (
        "worktree /repo\nHEAD aaa\nbranch refs/heads/master\n\n"
        f"worktree {tree}\nHEAD bbb\nbranch refs/heads/worktree-agent-a1\n"
        "locked some-other-session\n\n"
    )

    def run(cmd, *_a, **_k):
        return _result(porcelain if cmd[:2] == ["git", "worktree"] else "")

    lines = _mod.other_live_sessions("/repo", run=run, git_dirty=lambda *a, **k: False)
    assert lines == [
        "  • worktree-agent-a1 [#1345 Fix the cert watcher] — no commits yet"
    ]


def _toplevel(path):
    return lambda *_a, **_k: _result(f"{path}\n")


def test_an_unlabelled_generated_tree_is_asked_to_label_itself(tmp_path):
    tree = tmp_path / "bridge-cse_01"
    tree.mkdir()
    lines = _mod.unlabelled_worktree_lines(str(tree), run=_toplevel(tree))
    assert len(lines) == 1
    assert "label_worktree.py" in lines[0]


def test_a_labelled_or_session_named_tree_is_not_asked(tmp_path):
    labelled = tmp_path / "agent-a1"
    labelled.mkdir()
    (labelled / ".claim").write_text("#1 x\n")
    named = tmp_path / "claim-labels"
    named.mkdir()
    assert _mod.unlabelled_worktree_lines(str(labelled), run=_toplevel(labelled)) == []
    assert _mod.unlabelled_worktree_lines(str(named), run=_toplevel(named)) == []
