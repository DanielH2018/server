"""Tests for `lib.fanout_lock`: a fan-out batch's lock protects its tree only while it may run.

Run: uv run pytest scripts/lib/tests/test_fanout_lock.py
"""

import functools
import os

import pytest
from fanout_lib.launch import unit_name
from fanout_lib.target import SERVER_TARGET, Target
from lib import fanout_lock
from lib.worktrees import KEEP, REMOVABLE, Worktree, classify, lock_is_live

NOW = 1_800_000_000.0
OLD = NOW - 2 * fanout_lock.GRACE_S
OTHER_REPO = Target("DanielH2018/work-laptop-config", "/c", "origin/main")


def _batch_tree(tmp_path, locked_at: float = OLD) -> str:
    """A worktree directory whose `.git` file points at an admin dir holding a lock file."""
    tree, admin = tmp_path / "fanout-8529", tmp_path / "admin"
    tree.mkdir()
    admin.mkdir()
    (tree / ".git").write_text(f"gitdir: {admin}\n")
    (admin / "locked").write_text("fanout-8529")
    os.utime(admin / "locked", (locked_at, locked_at))
    return str(tree)


def _batch_worktree(tmp_path) -> Worktree:
    return Worktree(
        _batch_tree(tmp_path), "abc", "worktree-fanout-8529", True, "fanout-8529"
    )


def _is_live(tree: str, state: str | None) -> bool:
    return fanout_lock.batch_is_live(tree, "fanout-8529", lambda unit: state, NOW)


@pytest.mark.parametrize("target", [SERVER_TARGET, OTHER_REPO])
@pytest.mark.parametrize("batch", ["8529", "8381-8384-8383"])
def test_every_unit_name_launch_locks_with_is_a_fanout_reason(target, batch):
    assert fanout_lock.is_fanout_reason(unit_name(batch, target))


def test_a_claude_session_reason_is_not_a_fanout_reason():
    assert not fanout_lock.is_fanout_reason("claude session x (pid 1 start 2)")


def test_a_merged_tree_whose_batch_unit_is_gone_is_removable(tmp_path):
    live = functools.partial(lock_is_live, state=lambda unit: "inactive", now=NOW)
    verdict, _ = classify(
        _batch_worktree(tmp_path), merged=True, dirty=False, lock_live=live
    )
    assert verdict == REMOVABLE


def test_a_merged_tree_whose_batch_unit_runs_is_kept(tmp_path):
    live = functools.partial(lock_is_live, state=lambda unit: "active", now=NOW)
    verdict, why = classify(
        _batch_worktree(tmp_path), merged=True, dirty=False, lock_live=live
    )
    assert (verdict, why) == (KEEP, "in use — fanout-8529")


@pytest.mark.parametrize("state", ["inactive", "failed"])
def test_a_finished_unit_releases_an_old_lock(tmp_path, state):
    assert not _is_live(_batch_tree(tmp_path), state)


def test_a_manager_that_cannot_be_asked_keeps_the_tree(tmp_path):
    assert _is_live(_batch_tree(tmp_path), None)


def test_a_lock_inside_the_grace_keeps_the_tree_before_its_unit_starts(tmp_path):
    assert _is_live(_batch_tree(tmp_path, locked_at=NOW - 60), "inactive")


def test_an_unreadable_lock_file_keeps_the_tree(tmp_path):
    tree = _batch_tree(tmp_path)
    (tmp_path / "admin" / "locked").unlink()
    assert _is_live(tree, "inactive")
