"""When a claim expires.

The pairing matters: a rule that calls everything stale and a rule that calls nothing
stale are indistinguishable from the passing side of a single test.
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from dev.findings_claim import claim_is_live, claim_states
from dev.findings_model import claim_comment
from dev.prune_worktrees import Worktree

WT = "worktree-issue-1132"
PATH = "/home/ubuntu/server/.claude/worktrees/issue-1132"


def _tree(locked=True, reason="claude session x (pid 1 start 1)", branch=WT):
    return Worktree(
        path=PATH, head="abc1234", branch=branch, locked=locked, lock_reason=reason
    )


def _own_start():
    """This process's start time from /proc, the field `session_is_alive` compares."""
    stat_line = Path(f"/proc/{os.getpid()}/stat").read_text()
    return stat_line.rsplit(")", 1)[1].split()[19]


def _never(_arg):
    return False


def _always(_arg):
    return True


def test_claim_is_stale_when_its_worktree_is_gone():
    live, reason = claim_is_live(WT, [], dirty=_never, merged=_never)
    assert live is False
    assert "no worktree" in reason


def test_claim_is_live_when_its_worktree_exists_and_is_dirty():
    live, reason = claim_is_live(WT, [_tree()], dirty=_always, merged=_never)
    assert live is True
    assert "uncommitted" in reason


def test_claim_is_not_stale_when_its_worktree_is_dirty_with_a_dead_owner():
    # The 2026-09-05 restart: 14 agents died, their worktrees kept uncommitted edits, and
    # every session was resumed in place. Expiring these is the failure this rule prevents.
    dead = _tree(reason="claude session x (pid 999999 start 999999)")
    live, reason = claim_is_live(WT, [dead], dirty=_always, merged=_never)
    assert live is True
    assert "uncommitted" in reason


def test_claim_is_stale_when_its_worktree_is_merged_and_clean():
    live, reason = claim_is_live(
        WT, [_tree(locked=False)], dirty=_never, merged=_always
    )
    assert live is False
    assert "unmerged" not in reason
    assert "merged, clean" in reason


def test_claim_is_live_while_a_session_still_holds_the_worktree_lock():
    # The condition an earlier draft dropped. An orchestrator worktree is clean and at
    # master's tip, so `merged` says True — only the live lock keeps its claim alive for
    # the duration of the fan-out it is running.
    alive = _tree(reason=f"claude session x (pid {os.getpid()} start {_own_start()})")
    live, reason = claim_is_live(WT, [alive], dirty=_never, merged=_always)
    assert live is True
    assert "in use" in reason


def test_claim_is_live_when_its_worktree_is_clean_but_unmerged():
    live, _ = claim_is_live(WT, [_tree(locked=False)], dirty=_never, merged=_never)
    assert live is True


def test_a_worktree_on_a_different_branch_does_not_satisfy_the_claim():
    other = _tree(branch="worktree-something-else")
    live, reason = claim_is_live(WT, [other], dirty=_always, merged=_never)
    assert live is False
    assert "no worktree" in reason


def test_claim_states_reports_one_row_per_claimed_issue_and_skips_the_rest():
    issues = [
        {"number": 1132, "comments": [{"body": claim_comment(WT, None, "t")}]},
        {"number": 1140, "comments": []},
    ]
    rows = claim_states(issues, [_tree()], dirty=_always, merged=_never)
    assert [r.number for r in rows] == [1132]
    assert rows[0].worktree == WT
    assert rows[0].live is True
    assert rows[0].age_days is None  # the fixture comment carries no createdAt
