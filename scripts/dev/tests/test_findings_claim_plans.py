"""The gh argv a claim or a release plans, and the two cases each refuses."""

from datetime import date

import pytest


from _findings_fakes import operator_comment

from dev.findings_lib.issue_model import claim_comment
from dev.findings_lib.milestone import description_for
from dev.findings_lib.plans import (
    ClaimRefused,
    plan_claim,
    plan_milestone_add,
    plan_release,
    plan_release_held,
)

WT = "worktree-issue-1132"
TODAY = date(2026, 9, 11)
FOCUS = "Opt-in milestones"
SESSION_MILESTONE = {"title": FOCUS, "description": description_for(WT)}
PROJECT = "Backup hardening"
LEAD = "worktree-backup-lead"
PROJECT_MILESTONE = {
    "title": PROJECT,
    "description": description_for(LEAD, project=True),
}


def _issue(number=1132, labels=(), comments=(), milestone=None):
    """One issue, always carrying `claude` — `plan_claim` refuses one without it."""
    return {
        "number": number,
        "state": "OPEN",
        "labels": [{"name": n} for n in ("claude", *labels)],
        "comments": [operator_comment(b) for b in comments],
        "milestone": milestone,
    }


def test_claiming_an_unclaimed_issue_only_comments():
    plans = plan_claim(_issue(), worktree=WT, session=None, when="t", today=TODAY)
    assert len(plans) == 1
    assert plans[0][:3] == ["issue", "comment", "1132"]
    assert f"Claim: `{WT}`" in plans[0][4]


def test_claiming_for_a_branch_with_a_milestone_files_the_issue_under_it():
    plans = plan_claim(
        _issue(), worktree=WT, session=None, when="t", today=TODAY, milestone=FOCUS
    )
    assert plans[1] == ["issue", "edit", "1132", "--milestone", FOCUS]


def test_claiming_a_closed_issue_is_refused():
    closed = _issue()
    closed["state"] = "CLOSED"
    with pytest.raises(ClaimRefused) as exc:
        plan_claim(closed, worktree=WT, session=None, when="t", today=TODAY)
    assert "closed" in exc.value.reason


def test_claiming_an_issue_deferred_to_tomorrow_is_refused_and_names_the_way_out():
    with pytest.raises(ClaimRefused) as exc:
        plan_claim(
            _issue(labels=["not-before:2026-09-12"]),
            worktree=WT,
            session=None,
            when="t",
            today=TODAY,
        )
    assert "deferred until 2026-09-12" in exc.value.reason
    assert "defer 1132 --clear" in exc.value.reason


def test_claiming_an_issue_on_its_not_before_date_is_allowed():
    plans = plan_claim(
        _issue(labels=["not-before:2026-09-11"]),
        worktree=WT,
        session=None,
        when="t",
        today=TODAY,
    )
    assert plans[0][:3] == ["issue", "comment", "1132"]


def test_claiming_a_manual_issue_is_refused():
    with pytest.raises(ClaimRefused) as exc:
        plan_claim(
            _issue(labels=["manual"]), worktree=WT, session=None, when="t", today=TODAY
        )
    assert "manual" in exc.value.reason


def test_claiming_an_issue_another_worktree_holds_is_refused():
    issue = _issue(comments=[claim_comment("worktree-issue-9999", None, "t")])
    with pytest.raises(ClaimRefused) as exc:
        plan_claim(issue, worktree=WT, session=None, when="t", today=TODAY)
    assert "worktree-issue-9999" in exc.value.reason


def test_claiming_an_issue_outside_the_register_is_refused():
    """`_load_issue` has no label filter, so `claim 3 --worktree X` reached Renovate's
    Dependency Dashboard. `load_issues` DOES filter `--label claude`, so `claims`, `reap` and
    `next` were all blind to the claim and only a hand-typed `release` could clear it.
    """
    outsider = {"number": 3, "state": "OPEN", "labels": [], "comments": []}
    with pytest.raises(ClaimRefused) as exc:
        plan_claim(outsider, worktree=WT, session=None, when="t", today=TODAY)
    assert "claude" in exc.value.reason


def test_reclaiming_an_issue_you_already_hold_is_a_no_op():
    issue = _issue(comments=[claim_comment(WT, None, "t")], milestone=SESSION_MILESTONE)
    assert (
        plan_claim(
            issue, worktree=WT, session=None, when="t", today=TODAY, milestone=FOCUS
        )
        == []
    )


def test_reclaiming_an_issue_missing_from_its_session_milestone_repairs_it():
    issue = _issue(comments=[claim_comment(WT, None, "t")])
    assert plan_claim(
        issue, worktree=WT, session=None, when="t", today=TODAY, milestone=FOCUS
    ) == [["issue", "edit", "1132", "--milestone", FOCUS]]


def test_releasing_takes_the_issue_out_of_its_session_milestone():
    issue = _issue(comments=[claim_comment(WT, None, "t")], milestone=SESSION_MILESTONE)
    plans = plan_release(issue, worktree=WT, when="t", reason=None)
    assert plans[1] == ["issue", "edit", "1132", "--remove-milestone"]


def test_releasing_keeps_the_issue_in_a_project_milestone():
    issue = _issue(
        comments=[claim_comment(LEAD, None, "t")], milestone=PROJECT_MILESTONE
    )
    plans = plan_release(issue, worktree=LEAD, when="t", reason="batch failed")
    assert [p[:2] for p in plans] == [["issue", "comment"]]


def test_claiming_from_another_branch_project_leaves_the_issue_in_it():
    plans = plan_claim(
        _issue(milestone=PROJECT_MILESTONE),
        worktree=WT,
        session=None,
        when="t",
        today=TODAY,
        milestone=FOCUS,
    )
    assert [p[:2] for p in plans] == [["issue", "comment"]]


def test_claiming_from_your_own_project_backlog_only_comments():
    plans = plan_claim(
        _issue(milestone=PROJECT_MILESTONE),
        worktree=LEAD,
        session=None,
        when="t",
        today=TODAY,
        milestone=PROJECT,
    )
    assert [p[:2] for p in plans] == [["issue", "comment"]]


@pytest.mark.parametrize(
    ("issue", "reason"),
    [
        ({**_issue(), "state": "CLOSED"}, "closed"),
        ({**_issue(), "labels": []}, "not in the register"),
        (_issue(comments=[claim_comment(WT, None, "t")]), f"claimed by `{WT}`"),
    ],
    ids=["closed", "outside-the-register", "claimed"],
)
def test_a_backlog_refuses_an_issue_nobody_could_pick_up_from_it(issue, reason):
    with pytest.raises(ClaimRefused, match=reason):
        plan_milestone_add(issue, PROJECT_MILESTONE)


def test_adding_an_issue_already_in_the_backlog_plans_nothing():
    assert (
        plan_milestone_add(_issue(milestone=PROJECT_MILESTONE), PROJECT_MILESTONE) == []
    )


def test_releasing_leaves_an_operator_milestone_alone():
    issue = _issue(
        comments=[claim_comment(WT, None, "t")],
        milestone={"title": WT, "description": "the operator's own"},
    )
    plans = plan_release(issue, worktree=WT, when="t", reason=None)
    assert len(plans) == 1


def test_closing_keeps_the_issue_in_its_session_milestone():
    issue = _issue(comments=[claim_comment(WT, None, "t")], milestone=SESSION_MILESTONE)
    plans = plan_release_held(issue, when="t", reason="closed", detach=False)
    assert len(plans) == 1 and plans[0][:3] == ["issue", "comment", "1132"]


def test_releasing_a_claim_you_do_not_hold_is_refused():
    issue = _issue(comments=[claim_comment("worktree-issue-9999", None, "t")])
    with pytest.raises(ClaimRefused) as exc:
        plan_release(issue, worktree=WT, when="t", reason=None)
    assert "worktree-issue-9999" in exc.value.reason


def test_releasing_an_unclaimed_issue_is_refused():
    with pytest.raises(ClaimRefused) as exc:
        plan_release(_issue(), worktree=WT, when="t", reason=None)
    assert "not claimed" in exc.value.reason
