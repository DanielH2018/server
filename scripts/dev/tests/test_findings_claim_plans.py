"""The gh argv a claim or a release plans, and the two cases each refuses."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from dev.findings_model import claim_comment
from dev.findings_plans import ClaimRefused, plan_claim, plan_release

WT = "worktree-issue-1132"


def _issue(number=1132, labels=(), comments=()):
    return {
        "number": number,
        "state": "OPEN",
        "labels": [{"name": n} for n in labels],
        "comments": [{"body": b} for b in comments],
    }


def test_claiming_an_unclaimed_issue_comments_and_labels():
    plans = plan_claim(_issue(), worktree=WT, session=None, when="t")
    assert plans[0][:3] == ["issue", "comment", "1132"]
    assert f"Claim: `{WT}`" in plans[0][4]
    assert plans[1] == ["issue", "edit", "1132", "--add-label", "claimed"]


def test_claiming_a_closed_issue_is_refused():
    closed = _issue()
    closed["state"] = "CLOSED"
    with pytest.raises(ClaimRefused) as exc:
        plan_claim(closed, worktree=WT, session=None, when="t")
    assert "closed" in exc.value.reason


def test_claiming_a_manual_issue_is_refused():
    with pytest.raises(ClaimRefused) as exc:
        plan_claim(_issue(labels=["manual"]), worktree=WT, session=None, when="t")
    assert "manual" in exc.value.reason


def test_claiming_an_issue_another_worktree_holds_is_refused():
    issue = _issue(comments=[claim_comment("worktree-issue-9999", None, "t")])
    with pytest.raises(ClaimRefused) as exc:
        plan_claim(issue, worktree=WT, session=None, when="t")
    assert "worktree-issue-9999" in exc.value.reason


def test_reclaiming_an_issue_you_already_hold_is_a_no_op():
    issue = _issue(comments=[claim_comment(WT, None, "t")])
    assert plan_claim(issue, worktree=WT, session=None, when="t") == []


def test_releasing_a_claim_you_hold_comments_and_removes_the_label():
    issue = _issue(labels=["claimed"], comments=[claim_comment(WT, None, "t")])
    plans = plan_release(issue, worktree=WT, when="t", reason="landed in #1270")
    assert f"Released: `{WT}`" in plans[0][4]
    assert plans[1] == ["issue", "edit", "1132", "--remove-label", "claimed"]


def test_releasing_a_claim_you_do_not_hold_is_refused():
    issue = _issue(comments=[claim_comment("worktree-issue-9999", None, "t")])
    with pytest.raises(ClaimRefused) as exc:
        plan_release(issue, worktree=WT, when="t", reason=None)
    assert "worktree-issue-9999" in exc.value.reason


def test_releasing_an_unclaimed_issue_is_refused():
    with pytest.raises(ClaimRefused) as exc:
        plan_release(_issue(), worktree=WT, when="t", reason=None)
    assert "not claimed" in exc.value.reason
