"""What `next` offers, and the four reasons it withholds an issue."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from _findings_fakes import Fakes, build_tools, facts, make_issue, operator_comment

from dev.findings import main
from dev.findings_lib.issue_model import claim_comment, pickable, pr_refs

WT = "worktree-issue-1132"


def _issue(number, labels=(), comments=()):
    return {
        "number": number,
        "title": f"finding {number}",
        "state": "OPEN",
        "body": "",
        "labels": [{"name": n} for n in ("claude", *labels)],
        "comments": [operator_comment(b) for b in comments],
        "createdAt": "2026-09-01T00:00:00Z",
        "url": "",
    }


def test_an_ordinary_open_issue_is_pickable():
    assert [
        r["number"] for r in pickable([_issue(1)], live_claims=set(), pr_refs=set())
    ] == [1]


def test_a_manual_issue_is_not_pickable():
    assert (
        pickable([_issue(1, labels=["manual"])], live_claims=set(), pr_refs=set()) == []
    )


def test_a_live_claimed_issue_is_not_pickable():
    issue = _issue(1, comments=[claim_comment(WT, None, "t")])
    assert pickable([issue], live_claims={1}, pr_refs=set()) == []


def test_a_stale_claimed_issue_is_pickable():
    issue = _issue(1, comments=[claim_comment(WT, None, "t")])
    assert [
        r["number"] for r in pickable([issue], live_claims=set(), pr_refs=set())
    ] == [1]


def test_an_issue_with_an_open_pr_is_not_pickable():
    assert pickable([_issue(1)], live_claims=set(), pr_refs={1}) == []


def test_pickable_orders_high_severity_first():
    low = _issue(1, labels=["severity/low"])
    high = _issue(2, labels=["severity/high"])
    rows = pickable([low, high], live_claims=set(), pr_refs=set())
    assert [r["number"] for r in rows] == [2, 1]


def test_pr_refs_finds_every_closing_keyword():
    assert pr_refs(["Closes #1", "fixes #2", "Resolved #3"]) == {1, 2, 3}


def test_pr_refs_ignores_a_bare_issue_mention():
    assert pr_refs(["see #4", "closes issue 5"]) == set()


# --- main(["next", ...]): the handler's own plumbing, not just pickable() -----------------


def test_next_via_main_lists_an_ordinary_open_issue(capsys):
    """Exercises the plumbing `pickable()` alone can't: parser wiring, dispatch, JSON."""
    issue = make_issue(1132)
    tools, _ = build_tools(Fakes(issues=[issue], worktree_facts=facts()))
    assert main(["next", "--json"], tools) == 0
    rows = json.loads(capsys.readouterr().out)
    assert [r["number"] for r in rows] == [1132]


def test_next_text_render_marks_a_stale_claim_and_names_reap(capsys):
    """`next`'s whole text render was uncovered — every other test passes `--json` (#1275).

    One test for the three things it prints: the row, the marker naming who holds a stale
    claim, and the note pointing at `reap`. `next` offering a stale-claimed issue is the
    behaviour `pickable` pins; this pins that the operator is TOLD, which is the half that
    turned a correct offer into an unexplained exit 3 from `claim`.
    """
    stale_wt = "worktree-gone"
    free = make_issue(1140, title="nobody holds this")
    held = make_issue(
        1132, title="stale claim on this", comments=[claim_comment(stale_wt, None, "t")]
    )
    tools, _ = build_tools(Fakes(issues=[free, held], worktree_facts=facts()))
    assert main(["next"], tools) == 0
    out = capsys.readouterr().out
    assert "#1140" in out and "#1132" in out
    assert f"[stale claim by `{stale_wt}`]" in out
    assert "reap" in out


def test_next_text_render_marks_nothing_when_no_claim_is_stale(capsys):
    """The rejecting half: no marker and no `reap` note when every offered issue is free."""
    tools, _ = build_tools(Fakes(issues=[make_issue(1140)], worktree_facts=facts()))
    assert main(["next"], tools) == 0
    out = capsys.readouterr().out
    assert "#1140" in out
    assert "stale claim" not in out
    assert "reap" not in out


def test_next_says_so_when_nothing_is_pickable(capsys):
    tools, _ = build_tools(
        Fakes(issues=[make_issue(1140, labels=["manual"])], worktree_facts=facts())
    )
    assert main(["next"], tools) == 0
    assert "nothing to pick up" in capsys.readouterr().out


def test_next_withholds_a_live_claim_when_the_git_read_fails(capsys):
    """The safety-critical branch: a git-read failure must not read as "no claims are live".

    Pins the withhold end to end through `main`, not just through `pickable`.
    """
    held = make_issue(1132, comments=[claim_comment(WT, None, "t")])
    tools, _ = build_tools(Fakes(issues=[held], worktree_facts=facts(ok=False)))
    assert main(["next", "--json"], tools) == 0
    rows = json.loads(capsys.readouterr().out)
    assert rows == []


def test_next_withholds_an_issue_an_open_pr_already_closes(capsys):
    """#1283: the open-PR filter was proven only through `pickable` with a hand-fed set.

    No test drove `main(["next"])` with `Fakes(prs=[...])`, so `open_pr_refs` could return an
    empty set and the whole suite stayed green. The `prs` field was added to the shared fake
    for exactly this and then went unused.
    """
    spoken_for = make_issue(1132, title="a PR already says it closes this")
    free = make_issue(1140, title="nobody has this")
    tools, _ = build_tools(
        Fakes(
            issues=[spoken_for, free],
            prs=[{"body": "Closes #1132"}],
            worktree_facts=facts(),
        )
    )
    assert main(["next", "--json"], tools) == 0
    rows = json.loads(capsys.readouterr().out)
    assert [r["number"] for r in rows] == [1140]


def test_next_offers_an_issue_no_open_pr_mentions(capsys):
    """The rejecting half of the pair above: an empty PR list withholds nothing."""
    tools, _ = build_tools(
        Fakes(issues=[make_issue(1132)], prs=[], worktree_facts=facts())
    )
    assert main(["next", "--json"], tools) == 0
    rows = json.loads(capsys.readouterr().out)
    assert [r["number"] for r in rows] == [1132]


def test_next_limit_bounds_the_list(capsys):
    """#1283: `--limit` had no test at all, so the slice could be deleted outright."""
    tools, _ = build_tools(
        Fakes(issues=[make_issue(1132), make_issue(1140)], worktree_facts=facts())
    )
    assert main(["next", "--limit", "1", "--json"], tools) == 0
    assert len(json.loads(capsys.readouterr().out)) == 1


def test_next_with_no_limit_returns_every_pickable_issue(capsys):
    """The accepting half, and the operator-requested change it guards.

    `--limit` defaulted to 10. An orchestrator read `next --json`, got 10 rows and took them
    for the whole free set while 12 more sat invisible. A view blind to real state that does
    not announce it is the same failure class as the four paths in #1277, so the default is
    now unbounded and `--limit N` is the opt-in bound. Eleven issues, so a reintroduced
    default of 10 fails here rather than passing by coincidence.
    """
    issues = [make_issue(1100 + n) for n in range(11)]
    tools, _ = build_tools(Fakes(issues=issues, worktree_facts=facts()))
    assert main(["next", "--json"], tools) == 0
    assert len(json.loads(capsys.readouterr().out)) == 11
