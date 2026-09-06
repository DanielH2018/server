"""`claim` against an issue somebody else already holds: reap a stale claim, refuse a live one.

`next` offers an issue whose claim is stale, on purpose. Until #1274 `claim` refused any claim
at all, so a session did exactly what `next` told it to and got exit 3 with no route forward —
and nothing in the repo invoked `reap`, so the issue stayed offered and unclaimable forever.

The pairing is the point: a `claim` that reaps everything and one that reaps nothing are
indistinguishable from a fixture where every claim is stale.

Run: uv run pytest scripts/dev/tests/test_findings_claim_reap_then_claim.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from _findings_fakes import Fakes, build_tools, facts, make_issue

from dev.findings import main
from dev.findings_lib.issue_model import claim_comment
from dev.prune_worktrees import Worktree

MINE = "worktree-mine"
OTHER = "worktree-issue-1132"

# `MINE` is checked out and held by a live session in every fixture here. `cmd_claim`'s
# stale-at-birth guard reads `--worktree` before it reads any issue (#1278, #1281), so
# without this every test below would refuse for a reason none of them is about — they are
# all about what happens to SOMEBODY ELSE's claim.
MINE_TREE = Worktree(
    path="/w/mine", head="abc1234", branch=MINE, locked=True, lock_reason="held"
)


def _tree(branch=OTHER):
    return Worktree(
        path=f"/w/{branch}", head="abc1234", branch=branch, locked=False, lock_reason=""
    )


def _facts(*trees, **kwargs):
    """`facts(...)` with `MINE_TREE` always present, so only `OTHER`'s state varies."""
    return facts([MINE_TREE, *trees], **kwargs)


# `OTHER` has no worktree, so its claim is stale. Git itself worked (`ok=True`).
STALE = _facts()


def _held_by_other(number=1132):
    return make_issue(number, comments=[claim_comment(OTHER, None, "t")])


def test_claim_reaps_a_stale_claim_and_then_takes_the_issue(capsys):
    """The accepting half: no worktree holds `OTHER`, so its claim is stale."""
    issue = _held_by_other()
    tools, calls = build_tools(Fakes(issues=[issue], view=issue, worktree_facts=STALE))
    assert main(["claim", "1132", "--worktree", MINE], tools) == 0
    out = capsys.readouterr().out
    assert f"reaped stale claim by `{OTHER}`" in out
    assert f"#1132 claimed by `{MINE}`" in out
    # The release comes first, so the claim lands on an issue nobody holds.
    released = next(
        i for i, c in enumerate(calls.gh) if f"Released: `{OTHER}`" in " ".join(c)
    )
    claimed = next(
        i for i, c in enumerate(calls.gh) if f"Claim: `{MINE}`" in " ".join(c)
    )
    assert released < claimed
    assert ["issue", "edit", "1132", "--add-label", "claimed"] in calls.gh


def test_claim_still_refuses_a_live_claim(capsys):
    """The rejecting half: a dirty worktree still holds it, so nothing is reaped.

    Without this, a reap-everything bug would pass the test above while stealing issues out
    from under live sessions — the exact double-assignment the claim protocol prevents.
    """
    issue = _held_by_other()
    tools, calls = build_tools(
        Fakes(issues=[issue], view=issue, worktree_facts=_facts(_tree(), dirty=True))
    )
    assert main(["claim", "1132", "--worktree", MINE], tools) == 3
    assert f"already claimed by `{OTHER}`" in capsys.readouterr().out
    assert not any("Released: `" in a for c in calls.gh for a in c)
    assert not any(f"Claim: `{MINE}`" in a for c in calls.gh for a in c)


def test_claim_does_not_reap_when_the_git_read_fails(capsys):
    """A transient git error must not be read as "every worktree is gone".

    `cmd_reap` refuses outright on the same signal. Here the claim simply refuses, which is
    what it did before this path existed.
    """
    issue = _held_by_other()
    tools, calls = build_tools(
        Fakes(issues=[issue], view=issue, worktree_facts=_facts(ok=False))
    )
    assert main(["claim", "1132", "--worktree", MINE], tools) == 3
    captured = capsys.readouterr()
    assert f"already claimed by `{OTHER}`" in captured.out
    assert "read failed" in captured.err
    assert not any("Released: `" in a for c in calls.gh for a in c)


def test_claim_does_not_reap_a_manual_issue(capsys):
    """`plan_claim` stays the authority on `manual`, so the claim is left in place.

    Reaping first would release a claim off an issue the very next step refuses anyway.
    """
    issue = make_issue(
        1132, labels=["manual"], comments=[claim_comment(OTHER, None, "t")]
    )
    tools, calls = build_tools(Fakes(issues=[issue], view=issue, worktree_facts=STALE))
    assert main(["claim", "1132", "--worktree", MINE], tools) == 3
    assert "manual" in capsys.readouterr().out
    assert not any("Released: `" in a for c in calls.gh for a in c)


def test_claim_does_not_reap_an_issue_outside_the_register(capsys):
    """The third refusal `another_claim_blocks` has to know about, beside `manual` and closed.

    `_load_issue` has no label filter, so `claim 3` reaches an issue outside the `claude`
    register — Renovate's Dependency Dashboard is the live example (#1277). If one carried a
    stale `Claim:`, reaping first would post a release comment on an issue the very next step
    refuses anyway. `another_claim_blocks` and `plan_claim` must list the same three.
    """
    outsider = make_issue(1132, comments=[claim_comment(OTHER, None, "t")])
    outsider["labels"] = []
    tools, calls = build_tools(
        Fakes(issues=[outsider], view=outsider, worktree_facts=STALE)
    )
    assert main(["claim", "1132", "--worktree", MINE], tools) == 3
    assert "claude" in capsys.readouterr().out
    assert not any("Released: `" in a for c in calls.gh for a in c)


def test_claim_does_not_reap_a_closed_issue(capsys):
    issue = make_issue(1132, state="CLOSED", comments=[claim_comment(OTHER, None, "t")])
    tools, calls = build_tools(Fakes(issues=[issue], view=issue, worktree_facts=STALE))
    assert main(["claim", "1132", "--worktree", MINE], tools) == 3
    assert "closed" in capsys.readouterr().out
    assert not any("Released: `" in a for c in calls.gh for a in c)


def test_claim_dry_run_reaps_nothing_and_says_what_it_would_do(capsys):
    """Under `--dry-run` the release is printed rather than posted.

    So a re-read would still show the old claim and `plan_claim` would refuse something that
    will really succeed. The dry run reports both steps instead of reading back.
    """
    issue = _held_by_other()
    tools, calls = build_tools(Fakes(issues=[issue], view=issue, worktree_facts=STALE))
    assert main(["claim", "1132", "--worktree", MINE, "--dry-run"], tools) == 0
    out = capsys.readouterr().out
    assert f"reaped stale claim by `{OTHER}`" in out
    assert f"would be claimed by `{MINE}`" in out
    assert calls.gh == []


def test_reclaiming_an_issue_this_worktree_holds_still_reads_the_worktrees_once(capsys):
    """The idempotent re-claim, and the ONE git read now in front of every claim.

    This test used to wire `worktree_facts` to raise, pinning that a batch nothing blocked
    never paid for the read at all — `another_claim_blocks` answered False for the caller's
    own claim and the lazy read was never reached. The stale-at-birth guard (#1278, #1281)
    retires that: it asks about `--worktree` itself, which every batch has, so the read is
    unconditional and hoisted out of the loop. What survives is the bound that mattered —
    ONE read, not one per issue — which `test_claim_reads_the_worktrees_once_for_a_whole_batch`
    states for a batch of two and this states for the idempotent path.
    """
    reads = []

    def _counted():
        reads.append(1)
        return STALE()

    issue = make_issue(1132, comments=[claim_comment(MINE, None, "t")])
    tools, calls = build_tools(
        Fakes(issues=[issue], view=issue, worktree_facts=_counted)
    )
    assert main(["claim", "1132", "--worktree", MINE], tools) == 0
    assert f"already claimed by `{MINE}`" in capsys.readouterr().out
    assert not any(c[:2] == ["issue", "comment"] for c in calls.gh)
    assert len(reads) == 1


def test_claim_reads_the_worktrees_once_for_a_whole_batch():
    """One worktree read per batch, not one per issue: it shells out to git per tree."""
    reads = []

    def _counted():
        reads.append(1)
        return STALE()

    a, b = _held_by_other(1132), _held_by_other(1140)
    tools, _ = build_tools(
        Fakes(issues=[a, b], view={1132: a, 1140: b}, worktree_facts=_counted)
    )
    assert main(["claim", "1132", "1140", "--worktree", MINE], tools) == 0
    assert len(reads) == 1
