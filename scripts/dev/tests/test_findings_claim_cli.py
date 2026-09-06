"""The claim, release, claims and reap subcommands, driven through main()."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from _findings_fakes import (
    Fakes,
    build_tools,
    facts,
    fake_verify,
    live_worktree,
    make_issue,
)

from dev.findings import main
from dev.findings_model import claim_comment
from dev.prune_worktrees import Worktree

WT = "worktree-issue-1132"

# No worktrees at all, so every claim reads stale. Git itself worked (`ok=True`).
# `LIVE` is the counterpart cmd_claim needs: WT itself checked out and held by a live
# session, so the stale-at-birth guard lets the claim through (#1278, #1281).
STALE = facts()
LIVE = live_worktree(WT)


def test_claim_writes_a_comment_and_a_label():
    issue = make_issue(1132)
    tools, calls = build_tools(Fakes(issues=[issue], view=issue, worktree_facts=LIVE))
    assert main(["claim", "1132", "--worktree", WT], tools) == 0
    assert calls.gh[-2][:3] == ["issue", "comment", "1132"]
    assert calls.gh[-1] == ["issue", "edit", "1132", "--add-label", "claimed"]


def test_claim_creates_the_claimed_label_before_it_adds_it():
    # gh issue edit --add-label fails on a label the repo lacks; cmd_close already syncs
    # for this reason. Without it the FIRST real claim posts its comment, fails the label
    # edit, and the retry says "already claimed" — so the label is never added at all.
    issue = make_issue(1132)
    tools, calls = build_tools(
        Fakes(issues=[issue], view=issue, labels=set(), worktree_facts=LIVE)
    )
    main(["claim", "1132", "--worktree", WT], tools)
    created = [c for c in calls.gh if c[:2] == ["label", "create"]]
    assert any(c[2] == "claimed" for c in created)
    assert calls.gh.index(
        next(c for c in created if c[2] == "claimed")
    ) < calls.gh.index(["issue", "edit", "1132", "--add-label", "claimed"])


def test_claim_refuses_a_manual_issue_and_exits_3(capsys):
    issue = make_issue(1132, labels=["manual"])
    tools, calls = build_tools(Fakes(issues=[issue], view=issue, worktree_facts=LIVE))
    assert main(["claim", "1132", "--worktree", WT], tools) == 3
    assert not any(c[:2] == ["issue", "comment"] for c in calls.gh)
    assert "manual" in capsys.readouterr().out


def test_claim_takes_every_issue_it_can_and_still_exits_3_on_a_refusal():
    good, bad = make_issue(1132), make_issue(1140, labels=["manual"])
    tools, calls = build_tools(
        Fakes(issues=[good, bad], view={1132: good, 1140: bad}, worktree_facts=LIVE)
    )
    assert main(["claim", "1132", "1140", "--worktree", WT], tools) == 3
    assert any(c[:3] == ["issue", "comment", "1132"] for c in calls.gh)
    assert not any(c[:3] == ["issue", "comment", "1140"] for c in calls.gh)


def test_claim_refuses_a_closed_issue():
    issue = make_issue(1132, state="CLOSED")
    tools, calls = build_tools(Fakes(issues=[issue], view=issue, worktree_facts=LIVE))
    assert main(["claim", "1132", "--worktree", WT], tools) == 3
    assert not any(c[:2] == ["issue", "comment"] for c in calls.gh)


def test_dry_run_writes_nothing():
    issue = make_issue(1132)
    tools, calls = build_tools(Fakes(issues=[issue], view=issue, worktree_facts=LIVE))
    assert main(["claim", "1132", "--worktree", WT, "--dry-run"], tools) == 0
    assert calls.gh == []


def test_release_removes_the_label():
    issue = make_issue(
        1132, labels=["claimed"], comments=[claim_comment(WT, None, "t")]
    )
    tools, calls = build_tools(Fakes(issues=[issue], view=issue))
    assert main(["release", "1132", "--worktree", WT], tools) == 0
    assert ["issue", "edit", "1132", "--remove-label", "claimed"] in calls.gh


def test_release_refuses_another_worktrees_claim_and_exits_3(capsys):
    """Pairs with `test_release_removes_the_label` above, which exits 0.

    Only the plan-level refusal was covered, so nothing proved `cmd_release` turns a
    `ClaimRefused` into exit 3 rather than a traceback (#1275).
    """
    issue = make_issue(
        1132,
        labels=["claimed"],
        comments=[claim_comment("worktree-someone-else", None, "t")],
    )
    tools, calls = build_tools(Fakes(issues=[issue], view=issue))
    assert main(["release", "1132", "--worktree", WT], tools) == 3
    assert "claimed by `worktree-someone-else`" in capsys.readouterr().out
    assert not any(c[:2] == ["issue", "comment"] for c in calls.gh)


def test_release_refuses_an_unclaimed_issue_and_exits_3(capsys):
    tools, calls = build_tools(Fakes(issues=[make_issue(1132)], view=make_issue(1132)))
    assert main(["release", "1132", "--worktree", WT], tools) == 3
    assert "not claimed" in capsys.readouterr().out
    assert calls.gh == []


def test_claims_prints_one_row_per_claimed_issue(capsys):
    held = make_issue(1132, comments=[claim_comment(WT, None, "t")])
    tools, _ = build_tools(Fakes(issues=[held, make_issue(1140)], worktree_facts=STALE))
    assert main(["claims"], tools) == 0
    out = capsys.readouterr().out
    assert "1132" in out
    assert "1140" not in out


def test_claims_json_carries_every_field_of_a_row(capsys):
    """`claims --json` had no test at all (#1275), so nothing pinned the field names a
    consumer reads — `live` and `reason` above all, since they carry the verdict."""
    held = make_issue(1132, comments=[claim_comment(WT, None, "t")])
    tools, _ = build_tools(Fakes(issues=[held], worktree_facts=STALE))
    assert main(["claims", "--json"], tools) == 0
    rows = json.loads(capsys.readouterr().out)
    assert [r["number"] for r in rows] == [1132]
    assert rows[0]["worktree"] == WT
    assert rows[0]["live"] is False
    assert "no worktree" in rows[0]["reason"]
    assert rows[0]["age_days"] is None


def test_claims_says_so_when_nothing_is_claimed(capsys):
    tools, _ = build_tools(Fakes(issues=[make_issue(1132)], worktree_facts=STALE))
    assert main(["claims"], tools) == 0
    assert "no open claims" in capsys.readouterr().out


def test_reap_releases_a_stale_claim_and_leaves_a_live_one():
    """The pair.

    A reap that releases everything and a reap that releases nothing are indistinguishable
    from a fixture where every claim is stale, which is what an earlier draft of this test
    had.
    """
    live_wt = "worktree-issue-1140"
    tree = Worktree(
        path="/w/1140", head="abc", branch=live_wt, locked=False, lock_reason=""
    )
    stale = make_issue(1132, comments=[claim_comment(WT, None, "t")])
    live = make_issue(1140, comments=[claim_comment(live_wt, None, "t")])
    tools, calls = build_tools(
        Fakes(issues=[stale, live], worktree_facts=facts([tree], dirty=True))
    )
    assert main(["reap"], tools) == 0
    assert ["issue", "edit", "1132", "--remove-label", "claimed"] in calls.gh
    assert ["issue", "edit", "1140", "--remove-label", "claimed"] not in calls.gh


def test_claim_loses_a_race_and_reports_who_won(capsys):
    """The read-back's SECOND `issue view` sees a rival's claim land first.

    The rival's `Claim:` comment must not be visible on the FIRST read: `plan_claim` would
    refuse outright before ever posting, and the race this guards against — a rival winning
    between our own read and our own write — would never be exercised.
    """
    rival = "worktree-issue-9999"
    before = make_issue(1132)
    after_rival_won = make_issue(1132, comments=[claim_comment(rival, None, "t")])
    tools, calls = build_tools(
        Fakes(
            issues=[before],
            view={1132: [before, after_rival_won]},
            worktree_facts=LIVE,
        )
    )
    assert main(["claim", "1132", "--worktree", WT], tools) == 3
    assert f"lost the race to `{rival}`" in capsys.readouterr().out
    # The comment was posted before the race was discovered lost — cmd_claim writes first,
    # then reads back to check who actually holds it.
    assert any(c[:3] == ["issue", "comment", "1132"] for c in calls.gh)


def test_release_dry_run_writes_nothing_and_says_so(capsys):
    issue = make_issue(
        1132, labels=["claimed"], comments=[claim_comment(WT, None, "t")]
    )
    tools, calls = build_tools(Fakes(issues=[issue], view=issue))
    assert main(["release", "1132", "--worktree", WT, "--dry-run"], tools) == 0
    assert calls.gh == []
    assert "would be released" in capsys.readouterr().out


def test_reap_dry_run_writes_nothing_and_says_so(capsys):
    stale = make_issue(1132, comments=[claim_comment(WT, None, "t")])
    tools, calls = build_tools(Fakes(issues=[stale], worktree_facts=STALE))
    assert main(["reap", "--dry-run"], tools) == 0
    assert calls.gh == []
    assert "would" in capsys.readouterr().out


def test_reap_refuses_to_release_anything_when_git_fails():
    """A transient git failure must not read as "every worktree is gone".

    `_worktree_facts` returns `ok=False` on a git failure, not merely an empty worktree
    list — without that distinction `reap` cannot tell a real git error from a register
    where nothing is claimed, and would release every live claim in it.
    """
    stale = make_issue(1132, comments=[claim_comment(WT, None, "t")])
    tools, calls = build_tools(Fakes(issues=[stale], worktree_facts=facts(ok=False)))
    assert main(["reap"], tools) != 0
    assert not any(c[:2] == ["issue", "edit"] for c in calls.gh)


def test_reap_releases_when_the_worktree_list_is_genuinely_empty():
    """The other half of the pair above: `ok=True` with zero worktrees still reaps.

    Distinguishes "git failed" from "git succeeded and found nothing" — a fixture that
    conflated the two would pass on a reap that never fires as readily as one that always
    does.
    """
    stale = make_issue(1132, comments=[claim_comment(WT, None, "t")])
    tools, calls = build_tools(Fakes(issues=[stale], worktree_facts=STALE))
    assert main(["reap"], tools) == 0
    assert ["issue", "edit", "1132", "--remove-label", "claimed"] in calls.gh


def test_claims_warns_but_still_renders_when_git_fails(capsys):
    """A read can afford to render on a stale guess; it just has to say so.

    Silently showing STALE rows during a git outage reads as fact rather than a guess an
    operator could act on — releasing a claim by hand that was never actually stale.
    """
    held = make_issue(1132, comments=[claim_comment(WT, None, "t")])
    tools, _ = build_tools(Fakes(issues=[held], worktree_facts=facts(ok=False)))
    assert main(["claims"], tools) == 0
    captured = capsys.readouterr()
    assert "1132" in captured.out
    assert "read failed" in captured.err


def test_reap_dry_run_still_refuses_when_git_fails():
    """The git-failure refusal runs before any dry-run branching."""
    stale = make_issue(1132, comments=[claim_comment(WT, None, "t")])
    tools, calls = build_tools(Fakes(issues=[stale], worktree_facts=facts(ok=False)))
    assert main(["reap", "--dry-run"], tools) != 0
    assert calls.gh == []


def test_closing_a_claimed_issue_releases_the_claim():
    issue = make_issue(
        1132, labels=["claimed"], comments=[claim_comment(WT, None, "t")]
    )
    tools, calls = build_tools(Fakes(issues=[issue], view=issue))
    assert main(["close", "1132", "--fixed", "--pr", "1270"], tools) == 0
    assert ["issue", "edit", "1132", "--remove-label", "claimed"] in calls.gh
    assert any(f"Released: `{WT}`" in a for c in calls.gh for a in c)


def test_closing_an_unclaimed_issue_writes_no_release():
    issue = make_issue(1132)
    tools, calls = build_tools(Fakes(issues=[issue], view=issue))
    assert main(["close", "1132", "--fixed"], tools) == 0
    assert not any("Released: `" in a for c in calls.gh for a in c)


def test_closing_a_claimed_issue_as_refuted_releases_before_it_closes():
    """The `if held:` branch is outcome-agnostic; --fixed alone doesn't prove that."""
    issue = make_issue(
        1132, labels=["claimed"], comments=[claim_comment(WT, None, "t")]
    )
    tools, calls = build_tools(Fakes(issues=[issue], view=issue))
    argv = ["close", "1132", "--refuted", "--reason", "disproved"]
    assert main(argv, tools) == 0
    assert any(f"Released: `{WT}`" in a for c in calls.gh for a in c)
    remove_claimed = calls.gh.index(
        ["issue", "edit", "1132", "--remove-label", "claimed"]
    )
    add_refuted = calls.gh.index(["issue", "edit", "1132", "--add-label", "refuted"])
    closed = next(i for i, c in enumerate(calls.gh) if c[:2] == ["issue", "close"])
    assert remove_claimed < add_refuted < closed


# --- the stale-at-birth guard: #1278 and #1281, one guard ---------------------------------


def test_claim_is_clean_when_the_worktree_is_live():
    """The accepting half. `WT` is checked out and held, so the claim will read live."""
    issue = make_issue(1132)
    tools, calls = build_tools(Fakes(issues=[issue], view=issue, worktree_facts=LIVE))
    assert main(["claim", "1132", "--worktree", WT], tools) == 0
    assert any(f"Claim: `{WT}`" in a for c in calls.gh for a in c)


def test_claim_is_flagged_when_the_worktree_name_matches_no_branch(capsys):
    """#1278: the spec's own table puts worktree `issue-1132` beside branch
    `worktree-issue-1132`, and `claim_is_live` matches on the BRANCH. So the wrong one of two
    adjacent names wrote a claim that `claim_states` immediately called stale — `reap`
    released it and `next` re-offered the issue while the session was still working it.
    """
    issue = make_issue(1132)
    tools, calls = build_tools(
        Fakes(issues=[issue], view=issue, worktree_facts=live_worktree(WT))
    )
    assert main(["claim", "1132", "--worktree", "issue-1132"], tools) == 3
    assert "no worktree" in capsys.readouterr().err
    assert calls.none()


def test_claim_is_flagged_when_the_worktree_state_makes_the_claim_stale(capsys):
    """#1281: the name matches a real branch, but `classify` calls it REMOVABLE.

    Merged, clean and unlocked is what the primary checkout looks like (`--worktree master`)
    and what a crashed-and-resumed orchestrator looks like — the lock names a pid and a
    process start time, and a container restart on 2026-09-05 brought 14 worktrees back
    without theirs. Distinct from the test above: there the name matches nothing at all.
    """
    tree = Worktree(path="/w/wt", head="abc", branch=WT, locked=False, lock_reason="")
    issue = make_issue(1132)
    tools, calls = build_tools(
        Fakes(issues=[issue], view=issue, worktree_facts=facts([tree], merged=True))
    )
    assert main(["claim", "1132", "--worktree", WT], tools) == 3
    assert "merged, clean, unlocked" in capsys.readouterr().err
    assert calls.none()


def test_claim_force_overrides_the_stale_at_birth_refusal():
    """The way out. A resumed orchestrator legitimately re-claims without its lock, so the
    guard is a door and not a wall — and the refusal message names this flag."""
    issue = make_issue(1132)
    tools, calls = build_tools(Fakes(issues=[issue], view=issue, worktree_facts=STALE))
    assert main(["claim", "1132", "--worktree", WT, "--force"], tools) == 0
    assert any(f"Claim: `{WT}`" in a for c in calls.gh for a in c)


def test_claim_proceeds_when_the_worktree_read_fails(capsys):
    """The guard fails OPEN, the opposite direction from `reap` and for the opposite reason.

    `reap` WRITES on a bad read, so it refuses. This guard only declines to warn, and a
    transient git error must not stop a session claiming work at all.
    """
    issue = make_issue(1132)
    tools, calls = build_tools(
        Fakes(issues=[issue], view=issue, worktree_facts=facts(ok=False))
    )
    assert main(["claim", "1132", "--worktree", WT], tools) == 0
    assert "read failed" in capsys.readouterr().err
    assert any(f"Claim: `{WT}`" in a for c in calls.gh for a in c)


# --- #1277: every path that closes or reopens releases the claim ---------------------------


def test_verify_close_releases_the_claim_it_closes():
    """`verify --close` called `plan_close` directly, skipping the release `close` makes.

    `claims`, `reap` and `next` all read OPEN issues, so the claim it left behind was
    invisible to every view at once — and a later `open` reopening the issue brought it back
    LIVE, blocking `claim` for as long as the claiming worktree existed.

    The claim here is STALE — `STALE` registers no worktree at all — because a LIVE claim
    withholds the close entirely since #1302, and the release this test is about only
    happens on a close that goes ahead.
    """
    issue = make_issue(
        1132,
        labels=["claimed"],
        comments=[claim_comment(WT, None, "t")],
    )
    issue["body"] = "details\n\n## Verify-by\n```\ntrue\n```\n"
    tools, calls = build_tools(
        Fakes(issues=[issue], view=issue, verify=fake_verify, worktree_facts=STALE)
    )
    assert main(["verify", "1132", "--close"], tools) == 0
    assert any(f"Released: `{WT}`" in a for c in calls.gh for a in c)
    assert ["issue", "edit", "1132", "--remove-label", "claimed"] in calls.gh


def test_verify_close_writes_no_release_for_an_unclaimed_issue():
    """The rejecting half: nothing to release, so nothing is posted."""
    issue = make_issue(1132)
    issue["body"] = "details\n\n## Verify-by\n```\ntrue\n```\n"
    tools, calls = build_tools(Fakes(issues=[issue], view=issue, verify=fake_verify))
    assert main(["verify", "1132", "--close"], tools) == 0
    assert not any("Released: `" in a for c in calls.gh for a in c)
    assert any(c[:2] == ["issue", "close"] for c in calls.gh)


# --- #1284: the CLI half of the hardening -------------------------------------------------


def test_claim_refuses_a_worktree_name_the_trailer_cannot_carry(capsys):
    """The rejecting half of #1284.3: nothing is written, and the exit says bad argument.

    This name used to write the comment AND the label, then fail to parse its own trailer on
    read-back and report `lost the race to \\`None\\`` — a race against a rival that does not
    exist, with the claim already posted.
    """
    issue = make_issue(1132)
    tools, calls = build_tools(Fakes(issues=[issue], view=issue))
    assert main(["claim", "1132", "--worktree", "worktree-`bad"], tools) == 2
    assert calls.none()
    assert "backtick" in capsys.readouterr().err


def test_release_refuses_the_same_name(capsys):
    """The same trailer, so the same refusal — a release must name a parseable worktree."""
    issue = make_issue(1132)
    tools, calls = build_tools(Fakes(issues=[issue], view=issue))
    assert main(["release", "1132", "--worktree", "worktree-`bad"], tools) == 2
    assert calls.none()


def test_a_read_back_that_finds_no_claim_does_not_report_a_race(capsys):
    """The rejecting half of the message: an empty read-back is not a rival.

    The second `issue view` comes back with no claim at all, which is what an unparseable
    trailer or a comment past gh's page cap looks like. Saying `lost the race to \\`None\\``
    told the operator a rival took the issue.
    """
    before = make_issue(1132)
    tools, _ = build_tools(
        Fakes(
            issues=[before],
            view={1132: [before, make_issue(1132)]},
            worktree_facts=LIVE,
        )
    )
    assert main(["claim", "1132", "--worktree", WT], tools) == 3
    out = capsys.readouterr().out
    assert "read-back found no claim" in out
    assert "lost the race" not in out


def test_release_creates_the_claimed_label_before_it_removes_it():
    """The accepting half of #1284.4: `--remove-label` fails on a label the repo lacks.

    A claim can arrive with the label never created — hand-posted, or after an `--add-label`
    that failed — and without this sync the release comment is posted and THEN the label
    edit exits 1: released in the fold, labelled in GitHub.
    """
    issue = make_issue(
        1132, labels=["claimed"], comments=[claim_comment(WT, None, "t")]
    )
    tools, calls = build_tools(Fakes(issues=[issue], view=issue, labels=set()))
    assert main(["release", "1132", "--worktree", WT], tools) == 0
    created = [c for c in calls.gh if c[:2] == ["label", "create"]]
    assert any(c[2] == "claimed" for c in created)
    assert calls.gh.index(
        next(c for c in created if c[2] == "claimed")
    ) < calls.gh.index(["issue", "edit", "1132", "--remove-label", "claimed"])


def test_reap_creates_the_claimed_label_before_it_removes_it():
    """The same for `reap`, which removes the label on every stale claim it clears."""
    issue = make_issue(
        1132, labels=["claimed"], comments=[claim_comment(WT, None, "t")]
    )
    tools, calls = build_tools(
        Fakes(issues=[issue], labels=set(), worktree_facts=STALE)
    )
    assert main(["reap"], tools) == 0
    created = [c for c in calls.gh if c[:2] == ["label", "create"]]
    assert any(c[2] == "claimed" for c in created)
    assert calls.gh.index(
        next(c for c in created if c[2] == "claimed")
    ) < calls.gh.index(["issue", "edit", "1132", "--remove-label", "claimed"])
