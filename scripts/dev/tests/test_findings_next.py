"""What `next` offers, and the four reasons it withholds an issue."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from _findings_fakes import Fakes, build_tools, make_issue

from dev.findings import main, pickable
from dev.findings_model import claim_comment, pr_refs

WT = "worktree-issue-1132"


def _issue(number, labels=(), comments=()):
    return {
        "number": number,
        "title": f"finding {number}",
        "state": "OPEN",
        "body": "",
        "labels": [{"name": n} for n in ("claude", *labels)],
        "comments": [{"body": b} for b in comments],
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
    tools, _ = build_tools(Fakes(issues=[issue]))
    assert main(["next", "--json"], tools) == 0
    rows = json.loads(capsys.readouterr().out)
    assert [r["number"] for r in rows] == [1132]


def test_next_withholds_a_live_claim_when_the_git_read_fails(monkeypatch, capsys):
    """The safety-critical branch: a git-read failure must not read as "no claims are live".

    Pins the withhold end to end through `main`, not just through `pickable`.
    """
    import dev.findings as findings

    monkeypatch.setattr(
        findings,
        "_worktree_facts",
        lambda: ([], lambda _p: False, lambda _t: False, False),
    )
    held = make_issue(1132, comments=[claim_comment(WT, None, "t")])
    tools, _ = build_tools(Fakes(issues=[held]))
    assert main(["next", "--json"], tools) == 0
    rows = json.loads(capsys.readouterr().out)
    assert rows == []
