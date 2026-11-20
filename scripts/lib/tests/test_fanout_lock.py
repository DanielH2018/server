"""Tests for `lib.fanout_lock`: which locks are a fan-out batch's, and who judges them.

The rule itself, a unit-owned lock is dead once its unit is inactive and the lock is a day
old, is the dotfiles `claude_worktree.unit_lock_is_live`, tested there. These tests hold what
this repo adds: the pattern that names a fan-out lock, that pattern reaching both
`lib.worktrees.lock_is_live` and the pruner's policy file, and the /proc reader for a tree
another user owns.

Run: uv run pytest scripts/lib/tests/test_fanout_lock.py
"""

import functools
import json
import os

import pytest
from dev.fanout_lib.launch import unit_name
from dev.fanout_lib.target import SERVER_TARGET, Target
from lib import fanout_lock
from lib.repo_paths import REPO
from lib.worktree_label import LABEL_FILE
from lib.worktree_owner import SESSION_BRANCH_PREFIXES
from lib.worktrees import (
    KEEP,
    REMOVABLE,
    UNIT_LOCK_GRACE_S,
    Worktree,
    classify,
    lock_is_live,
)

NOW = 1_800_000_000.0
OLD = NOW - 2 * UNIT_LOCK_GRACE_S
OTHER_REPO = Target("DanielH2018/work-laptop-config", "/c", "origin/main")
POLICY = json.loads((REPO / ".claude" / "prune-worktrees.json").read_text())


def _batch_worktree(tmp_path) -> Worktree:
    """A locked worktree whose `.git` file points at an admin dir holding an old lock file."""
    tree, admin = tmp_path / "fanout-8529", tmp_path / "admin"
    tree.mkdir()
    admin.mkdir()
    (tree / ".git").write_text(f"gitdir: {admin}\n")
    (admin / "locked").write_text("fanout-8529")
    os.utime(admin / "locked", (OLD, OLD))
    return Worktree(str(tree), "abc", "worktree-fanout-8529", True, "fanout-8529")


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


def test_the_pruner_policy_carries_the_fanout_lock_pattern():
    # The pruner judges a fan-out lock by the pattern the policy file hands it, and
    # `lock_is_live` by REASON. Two patterns would give two answers for one tree.
    assert POLICY["unit_locks"] == fanout_lock.REASON.pattern


def test_the_pruner_policy_carries_the_session_branch_prefixes_and_the_label_file():
    assert tuple(POLICY["branch_prefixes"]) == SESSION_BRANCH_PREFIXES
    assert POLICY["label_file"] == LABEL_FILE


def _proc_root(tmp_path, *units: str) -> str:
    """A fake /proc holding one process per unit under ubuntu's manager, and one kernel thread."""
    proc = tmp_path / "proc"
    (proc / "2").mkdir(parents=True)
    (proc / "2" / "cgroup").write_text("0::/\n")
    for pid, unit in enumerate(units, start=100):
        (proc / str(pid)).mkdir()
        (proc / str(pid) / "cgroup").write_text(
            f"0::/user.slice/user-1000.slice/user@1000.service/app.slice/{unit}.service\n"
        )
    return str(proc)


def test_another_users_unit_that_holds_a_process_reads_active(tmp_path):
    state = fanout_lock.process_unit_state(_proc_root(tmp_path, "fanout-8529"))
    assert state("fanout-8529") == "active"


def test_another_users_unit_that_holds_no_process_reads_inactive(tmp_path):
    state = fanout_lock.process_unit_state(_proc_root(tmp_path, "fanout-8530"))
    assert state("fanout-8529") == "inactive"


def test_an_unreadable_proc_gives_no_state(tmp_path):
    state = fanout_lock.process_unit_state(str(tmp_path / "no-proc"))
    assert state("fanout-8529") is None
