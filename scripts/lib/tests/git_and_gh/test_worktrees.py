"""Tests for `lib.worktrees`: what this repo adds over the dotfiles `claude_worktree` library.

The library itself (the readers, the landed ladder, the holds, `classify` and `remove`) is
tested in the dotfiles repo beside it; `test_claude_worktree_import.py` covers the bootstrap
and `test_fanout_lock.py` the fan-out lock pattern. What is tested here is this repo's glue:
`classify` and `is_merged` as `findings.py` and `fanout.py` call them, the claim-liveness
reader `held_by_readable_process`, and `worktree_facts`, which separates a failed read from
an empty one.

Run: uv run pytest scripts/lib/tests/git_and_gh/test_worktrees.py
"""

import os
import subprocess
from contextlib import contextmanager
from pathlib import Path

from lib.git_testing import commit, git, init_repo, scrub_process_git_env
from lib.proc_testing import live_session_lock
from lib.worktrees import (
    COORDINATOR_LOCK,
    FOREIGN_UID,
    KEEP,
    REMOVABLE,
    Worktree,
    classify,
    has_live_session,
    held_by_readable_process,
    is_merged,
    lock_is_live,
    worktree_facts,
)


@contextmanager
def _sleeper(cwd: Path):
    """A live process with `cwd`, killed on exit."""
    env = dict(os.environ)
    env.pop("CLAUDE_PROJECT_DIR", None)
    proc = subprocess.Popen(["sleep", "60"], cwd=cwd, env=env)
    try:
        yield proc
    finally:
        proc.kill()
        proc.wait()


def _tree(branch="worktree-x", locked=False):
    return Worktree(path="/w", head="abc", branch=branch, locked=locked)


def test_a_merged_clean_unlocked_tree_is_removable():
    assert classify(_tree(), merged=True, dirty=False)[0] == REMOVABLE


def test_an_unmerged_tree_is_kept():
    assert classify(_tree(), merged=False, dirty=False) == (
        KEEP,
        "worktree-x not merged",
    )


def test_is_merged_counts_a_squash_landed_branch(tmp_path, monkeypatch):
    # PRs land here squashed, so the tip is never an ancestor of master and no patch-id
    # matches. A claim on such a branch must read stale, or `reap` never releases it.
    scrub_process_git_env(monkeypatch)
    monkeypatch.setenv("GH_BIN", "false")
    repo = tmp_path / "repo"
    init_repo(repo)
    commit(repo, "init", **{"a.txt": "one\n"})
    git(repo, "checkout", "-q", "-b", "feature")
    commit(repo, "first half", **{"b.txt": "two\n"})
    head = commit(repo, "second half", **{"c.txt": "three\n"})
    git(repo, "checkout", "-q", "master")
    commit(repo, "squash of feature", **{"b.txt": "two\n", "c.txt": "three\n"})

    assert is_merged(str(repo), head, "feature", base="master")


def test_is_merged_rejects_a_branch_holding_unlanded_work(tmp_path, monkeypatch):
    scrub_process_git_env(monkeypatch)
    monkeypatch.setenv("GH_BIN", "false")
    repo = tmp_path / "repo"
    init_repo(repo)
    commit(repo, "init", **{"a.txt": "one\n"})
    git(repo, "checkout", "-q", "-b", "feature")
    head = commit(repo, "open work", **{"b.txt": "two\n"})
    git(repo, "checkout", "-q", "master")

    assert not is_merged(str(repo), head, "feature", base="master")


def test_a_readable_process_in_the_tree_holds_it_is_flagged():
    holds = [(4242, "/w/a/scripts", "cwd"), (4243, "/w/ab", "CLAUDE_PROJECT_DIR")]

    assert held_by_readable_process("/w/a", holds=lambda: holds)


def test_an_unreadable_hold_or_a_sibling_tree_does_not_hold_it_is_clean():
    # A foreign-uid hold names no path, so `holds_tree` matches it against every tree.
    holds = [(4242, "", FOREIGN_UID), (4243, "/w/ab", "cwd")]

    assert not held_by_readable_process("/w/a", holds=lambda: holds)


def test_a_hold_in_the_main_worktree_does_not_hold_it_is_clean(tmp_path):
    (tmp_path / ".git").mkdir()
    holds = [
        (4242, str(tmp_path), "cwd"),
        (4243, f"{tmp_path}/.claude/worktrees/s", "cwd"),
    ]

    assert not held_by_readable_process(str(tmp_path), holds=lambda: holds)


def test_worktree_facts_ok_is_false_when_the_git_call_fails(monkeypatch):
    """`reap` refuses on `ok=False` rather than release the whole register.

    A failing `git worktree list` must read as "the read failed", not as "there are no
    worktrees" — the two produce the same empty stdout under `check=False`.
    """
    monkeypatch.setattr("lib.worktrees.primary_checkout", lambda: "/repo")
    monkeypatch.setattr(
        "lib.worktrees.git",
        lambda *a, **k: subprocess.CompletedProcess(a, 1, stdout="", stderr="fatal\n"),
    )
    trees, _dirty, _merged, ok = worktree_facts()
    assert ok is False
    assert trees == []


def test_worktree_facts_ok_is_true_when_git_succeeds_with_no_worktrees(monkeypatch):
    """The other half of the pair above: a real empty list still reads `ok=True`."""
    monkeypatch.setattr("lib.worktrees.primary_checkout", lambda: "/repo")
    monkeypatch.setattr(
        "lib.worktrees.git",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout="", stderr=""),
    )
    trees, _dirty, _merged, ok = worktree_facts()
    assert ok is True
    assert trees == []


def _project_tree(path):
    reason = f"{COORDINATOR_LOCK}Backups"
    return Worktree(str(path), "abc", "claude/backups", locked=True, lock_reason=reason)


def test_a_coordinator_project_with_no_process_in_its_tree_is_flagged(tmp_path):
    # `lock_is_live` reads the same lock as live: its reason names no pid.
    assert lock_is_live(_project_tree(tmp_path))

    assert not has_live_session(_project_tree(tmp_path))


def test_a_coordinator_project_an_idle_process_stands_in_is_clean(tmp_path):
    with _sleeper(cwd=tmp_path):
        assert has_live_session(_project_tree(tmp_path))


def _cron_tree(path):
    reason = "docs-refresh's own tree"
    return Worktree(str(path), "abc", None, locked=True, lock_reason=reason)


def test_a_lock_naming_no_session_or_batch_with_no_process_is_flagged(tmp_path):
    assert lock_is_live(_cron_tree(tmp_path))

    assert not has_live_session(_cron_tree(tmp_path), in_use=lambda path: [])


def test_a_session_lock_is_judged_by_its_own_reason(tmp_path):
    held = Worktree(
        str(tmp_path), "abc", "b", locked=True, lock_reason=live_session_lock()
    )
    gone = Worktree(
        str(tmp_path),
        "abc",
        "b",
        locked=True,
        lock_reason="claude session gone (pid 1 start 0)",
    )

    assert has_live_session(held, in_use=lambda path: [])
    assert not has_live_session(gone, in_use=lambda path: [(1, "cwd")])
