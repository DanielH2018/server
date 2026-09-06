"""When a claim expires.

The pairing matters: a rule that calls everything stale and a rule that calls nothing
stale are indistinguishable from the passing side of a single test.
"""

import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from _findings_fakes import foreign_comment, operator_comment

from dev.findings_claim import claim_is_live, claim_states
from dev.findings_model import claim_comment, release_comment
from dev.prune_worktrees import Worktree

WT = "worktree-issue-1132"
PATH = "/home/ubuntu/server/.claude/worktrees/issue-1132"


def _foreign_claim(worktree=WT, created=None):
    """A claim trailer any GitHub account could post on this public repo (#1280)."""
    fields = {"createdAt": created} if created else {}
    return foreign_comment(claim_comment(worktree, None, "t"), **fields)


def _claimed(worktree=WT, created=None):
    """The operator's claim comment on ``worktree``, optionally aged."""
    fields = {"createdAt": created} if created else {}
    return operator_comment(claim_comment(worktree, None, "t"), **fields)


def _released(worktree=WT, created=None):
    """The operator's release comment for ``worktree``, optionally aged."""
    fields = {"createdAt": created} if created else {}
    return operator_comment(release_comment(worktree, "t", None), **fields)


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
        {"number": 1132, "comments": [_claimed()]},
        {"number": 1140, "comments": []},
    ]
    rows = claim_states(issues, [_tree()], dirty=_always, merged=_never)
    assert [r.number for r in rows] == [1132]
    assert rows[0].worktree == WT
    assert rows[0].live is True
    assert rows[0].age_days is None  # the fixture comment carries no createdAt


def test_age_days_computed_from_createdAt():
    """Verify age_days is computed from the claim comment's createdAt.

    This test proves the computing path of age_days is active. If the path is neutered
    (e.g. _claim_age_days returns None as its first line), this test fails.
    """

    # Create a comment with createdAt timestamp from 5 days ago.
    five_days_ago = (datetime.now(UTC) - timedelta(days=5)).isoformat()

    issues = [{"number": 1132, "comments": [_claimed(created=five_days_ago)]}]
    rows = claim_states(issues, [_tree()], dirty=_always, merged=_never)
    assert len(rows) == 1
    assert rows[0].age_days == 5


def test_age_days_from_currently_open_episode_after_claim_release_reclaim():
    """Verify age_days comes from the current episode, not the first claim.

    A worktree that claims, releases, then reclaims the same issue should report
    the age from the third comment (the reclaim), not the first comment (the original claim).
    """

    # First claim (5 days ago), then release (3 days ago), then reclaim (1 day ago).
    five_days_ago = (datetime.now(UTC) - timedelta(days=5)).isoformat()
    three_days_ago = (datetime.now(UTC) - timedelta(days=3)).isoformat()
    one_day_ago = (datetime.now(UTC) - timedelta(days=1)).isoformat()

    issues = [
        {
            "number": 2000,
            "comments": [
                _claimed(created=five_days_ago),
                _released(created=three_days_ago),
                _claimed(created=one_day_ago),
            ],
        }
    ]
    rows = claim_states(issues, [_tree(branch=WT)], dirty=_always, merged=_never)
    assert len(rows) == 1
    # The age should come from the reclaim (1 day ago), not the first claim (5 days ago).
    assert rows[0].age_days == 1


def test_age_days_returns_none_for_naive_datetime():
    """Verify age_days returns None when createdAt is a naive datetime.

    A date-only string like "2026-09-01" parses as a naive datetime. The subtraction
    against datetime.now(UTC) would raise TypeError, which is caught and treated as
    None.
    """
    # A date-only string (naive datetime).
    issues = [{"number": 1132, "comments": [_claimed(created="2026-09-01")]}]
    rows = claim_states(issues, [_tree()], dirty=_always, merged=_never)
    assert len(rows) == 1
    assert rows[0].age_days is None


def _raises_missing_dir(arg):
    """What `lib.git.git_dirty` does on a prunable worktree: git with check=True, no cwd."""
    raise FileNotFoundError(2, "No such file or directory", str(arg))


def test_a_prunable_worktree_holds_its_claim_instead_of_crashing():
    """The accepting half: an unreadable worktree is a held claim, not a traceback.

    `git worktree list --porcelain` keeps listing a worktree whose directory was removed by
    hand, so `dirty` runs git in a directory that does not exist and raises. That took
    `claims`, `reap` and `next` down at once under an error blaming gh (#1276).
    """
    live, reason = claim_is_live(
        WT, [_tree()], dirty=_raises_missing_dir, merged=_never
    )
    assert live is True
    assert "unreadable" in reason
    assert "git worktree prune" in reason


def test_a_merged_read_that_raises_also_holds_the_claim():
    """`merged` raises too — `is_merged` shells out to git several times per tree."""
    live, reason = claim_is_live(
        WT, [_tree(locked=False)], dirty=_never, merged=_raises_missing_dir
    )
    assert live is True
    assert "unreadable" in reason


def test_a_readable_worktree_still_gets_the_ordinary_verdict():
    """The rejecting half: the guard must not collapse every tree into "live".

    Without this, a try/except that swallowed the whole verdict — or one that returned True
    unconditionally — would pass the two tests above while `reap` never released anything.
    """
    live, reason = claim_is_live(
        WT, [_tree(locked=False)], dirty=_never, merged=_always
    )
    assert live is False
    assert "unreadable" not in reason


def test_a_foreign_claim_produces_no_claim_row_at_all():
    """`claim_states` folds through `current_claim`, so a drive-by claim is not a row.

    Without this, a single foreign comment made `claims` show a claim, `next` withhold the
    issue and `claim` refuse it (#1280).
    """
    issues = [{"number": 1132, "comments": [_foreign_claim()]}]
    assert claim_states(issues, [_tree()], dirty=_always, merged=_never) == []


def test_the_age_of_a_claim_ignores_a_foreign_comment_between_its_episodes():
    """`_claim_age_days` must skip exactly what `current_claim` skips.

    A foreign `Released:` between the operator's claim and now would, if folded, close the
    episode `_claim_age_days` is measuring — so the row would age the claim from a later
    comment than the one the register thinks is current, or report no age at all.
    """
    five_days_ago = (datetime.now(UTC) - timedelta(days=5)).isoformat()
    two_days_ago = (datetime.now(UTC) - timedelta(days=2)).isoformat()
    issues = [
        {
            "number": 1132,
            "comments": [
                _claimed(created=five_days_ago),
                foreign_comment(
                    release_comment(WT, "t", "not mine"), createdAt=two_days_ago
                ),
            ],
        }
    ]
    rows = claim_states(issues, [_tree()], dirty=_always, merged=_never)
    assert [r.worktree for r in rows] == [WT]
    assert rows[0].age_days == 5


def test_claim_is_stale_when_locked_with_dead_owner_clean_and_merged():
    """Verify a worktree with dead owner, clean, and merged is stale.

    This is the steady state of a worktree whose session died after its PR landed.
    It must be judged STALE so `reap` can release the claim.
    """
    dead_owner = _tree(reason="claude session x (pid 999999 start 999999)", locked=True)
    live, reason = claim_is_live(WT, [dead_owner], dirty=_never, merged=_always)
    assert live is False
    assert "lock owner is dead" in reason
