"""The claim, release, claims and reap subcommands, driven through main()."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from _findings_fakes import Fakes, build_tools, make_issue

from dev.findings import main
from dev.findings_model import claim_comment

WT = "worktree-issue-1132"


def _stale_facts():
    """No worktrees at all, so every claim reads stale. Git itself worked (`ok=True`)."""
    return [], lambda _p: False, lambda _t: False, True


def test_claim_writes_a_comment_and_a_label():
    issue = make_issue(1132)
    tools, calls = build_tools(Fakes(issues=[issue], view=issue))
    assert main(["claim", "1132", "--worktree", WT], tools) == 0
    assert calls.gh[-2][:3] == ["issue", "comment", "1132"]
    assert calls.gh[-1] == ["issue", "edit", "1132", "--add-label", "claimed"]


def test_claim_creates_the_claimed_label_before_it_adds_it():
    # gh issue edit --add-label fails on a label the repo lacks; cmd_close already syncs
    # for this reason. Without it the FIRST real claim posts its comment, fails the label
    # edit, and the retry says "already claimed" — so the label is never added at all.
    issue = make_issue(1132)
    tools, calls = build_tools(Fakes(issues=[issue], view=issue, labels=set()))
    main(["claim", "1132", "--worktree", WT], tools)
    created = [c for c in calls.gh if c[:2] == ["label", "create"]]
    assert any(c[2] == "claimed" for c in created)
    assert calls.gh.index(
        next(c for c in created if c[2] == "claimed")
    ) < calls.gh.index(["issue", "edit", "1132", "--add-label", "claimed"])


def test_claim_refuses_a_manual_issue_and_exits_3(capsys):
    issue = make_issue(1132, labels=["manual"])
    tools, calls = build_tools(Fakes(issues=[issue], view=issue))
    assert main(["claim", "1132", "--worktree", WT], tools) == 3
    assert not any(c[:2] == ["issue", "comment"] for c in calls.gh)
    assert "manual" in capsys.readouterr().out


def test_claim_takes_every_issue_it_can_and_still_exits_3_on_a_refusal():
    good, bad = make_issue(1132), make_issue(1140, labels=["manual"])
    tools, calls = build_tools(Fakes(issues=[good, bad], view={1132: good, 1140: bad}))
    assert main(["claim", "1132", "1140", "--worktree", WT], tools) == 3
    assert any(c[:3] == ["issue", "comment", "1132"] for c in calls.gh)
    assert not any(c[:3] == ["issue", "comment", "1140"] for c in calls.gh)


def test_claim_refuses_a_closed_issue():
    issue = make_issue(1132, state="CLOSED")
    tools, calls = build_tools(Fakes(issues=[issue], view=issue))
    assert main(["claim", "1132", "--worktree", WT], tools) == 3
    assert not any(c[:2] == ["issue", "comment"] for c in calls.gh)


def test_dry_run_writes_nothing():
    issue = make_issue(1132)
    tools, calls = build_tools(Fakes(issues=[issue], view=issue))
    assert main(["claim", "1132", "--worktree", WT, "--dry-run"], tools) == 0
    assert calls.gh == []


def test_release_removes_the_label():
    issue = make_issue(
        1132, labels=["claimed"], comments=[claim_comment(WT, None, "t")]
    )
    tools, calls = build_tools(Fakes(issues=[issue], view=issue))
    assert main(["release", "1132", "--worktree", WT], tools) == 0
    assert ["issue", "edit", "1132", "--remove-label", "claimed"] in calls.gh


def test_claims_prints_one_row_per_claimed_issue(capsys, monkeypatch):
    import dev.findings as findings

    monkeypatch.setattr(findings, "_worktree_facts", _stale_facts)
    held = make_issue(1132, comments=[claim_comment(WT, None, "t")])
    tools, _ = build_tools(Fakes(issues=[held, make_issue(1140)]))
    assert main(["claims"], tools) == 0
    out = capsys.readouterr().out
    assert "1132" in out
    assert "1140" not in out


def test_reap_releases_a_stale_claim_and_leaves_a_live_one(monkeypatch):
    """The pair.

    A reap that releases everything and a reap that releases nothing are indistinguishable
    from a fixture where every claim is stale, which is what an earlier draft of this test
    had.
    """
    import dev.findings as findings
    from dev.prune_worktrees import Worktree

    live_wt = "worktree-issue-1140"
    tree = Worktree(
        path="/w/1140", head="abc", branch=live_wt, locked=False, lock_reason=""
    )
    monkeypatch.setattr(
        findings,
        "_worktree_facts",
        lambda: ([tree], lambda _p: True, lambda _t: False, True),
    )
    stale = make_issue(1132, comments=[claim_comment(WT, None, "t")])
    live = make_issue(1140, comments=[claim_comment(live_wt, None, "t")])
    tools, calls = build_tools(Fakes(issues=[stale, live]))
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
        Fakes(issues=[before], view={1132: [before, after_rival_won]})
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


def test_reap_dry_run_writes_nothing_and_says_so(monkeypatch, capsys):
    import dev.findings as findings
    from dev.prune_worktrees import Worktree

    live_wt = "worktree-issue-1140"
    tree = Worktree(
        path="/w/1140", head="abc", branch=live_wt, locked=False, lock_reason=""
    )
    monkeypatch.setattr(
        findings,
        "_worktree_facts",
        lambda: ([tree], lambda _p: True, lambda _t: False, True),
    )
    stale = make_issue(1132, comments=[claim_comment(WT, None, "t")])
    tools, calls = build_tools(Fakes(issues=[stale]))
    assert main(["reap", "--dry-run"], tools) == 0
    assert calls.gh == []
    assert "would" in capsys.readouterr().out


def test_reap_refuses_to_release_anything_when_git_fails(monkeypatch):
    """A transient git failure must not read as "every worktree is gone".

    `_worktree_facts` returns `ok=False` on a git failure, not merely an empty worktree
    list — without that distinction `reap` cannot tell a real git error from a register
    where nothing is claimed, and would release every live claim in it.
    """
    import dev.findings as findings

    monkeypatch.setattr(
        findings,
        "_worktree_facts",
        lambda: ([], lambda _p: False, lambda _t: False, False),
    )
    stale = make_issue(1132, comments=[claim_comment(WT, None, "t")])
    tools, calls = build_tools(Fakes(issues=[stale]))
    assert main(["reap"], tools) != 0
    assert not any(c[:2] == ["issue", "edit"] for c in calls.gh)


def test_reap_releases_when_the_worktree_list_is_genuinely_empty(monkeypatch):
    """The other half of the pair above: `ok=True` with zero worktrees still reaps.

    Distinguishes "git failed" from "git succeeded and found nothing" — a fixture that
    conflated the two would pass on a reap that never fires as readily as one that always
    does.
    """
    import dev.findings as findings

    monkeypatch.setattr(
        findings,
        "_worktree_facts",
        lambda: ([], lambda _p: False, lambda _t: False, True),
    )
    stale = make_issue(1132, comments=[claim_comment(WT, None, "t")])
    tools, calls = build_tools(Fakes(issues=[stale]))
    assert main(["reap"], tools) == 0
    assert ["issue", "edit", "1132", "--remove-label", "claimed"] in calls.gh
