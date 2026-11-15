"""`milestone.ensure` and `milestone.close_idle` against a fake `gh api`."""

import subprocess

import pytest

from dev.findings_lib.milestone import DESCRIPTION, close_idle, ensure, pr_title

REPO = "o/r"
WT = "worktree-a"


def _fake(open_=(), closed=(), create_error=None):
    writes = []

    def gh_json(*argv, **_kw):
        state = argv[1].split("state=")[1].split("&")[0]
        return [list(open_ if state == "open" else closed)]

    def gh(*argv, **_kw):
        writes.append(list(argv))
        if create_error and "POST" in argv:
            raise create_error

    return gh_json, gh, writes


def _m(title, number=1, description=DESCRIPTION, open_issues=0):
    return {
        "title": title,
        "number": number,
        "description": description,
        "open_issues": open_issues,
    }


def test_ensure_writes_nothing_when_the_milestone_is_open():
    gh_json, gh, writes = _fake(open_=[_m(WT)])
    ensure(WT, REPO, dry_run=False, gh_json=gh_json, gh=gh)
    assert writes == []


def test_ensure_creates_a_missing_milestone():
    gh_json, gh, writes = _fake()
    ensure(WT, REPO, dry_run=False, gh_json=gh_json, gh=gh)
    assert writes == [
        [
            "api",
            "-X",
            "POST",
            "repos/o/r/milestones",
            "-f",
            f"title={WT}",
            "-f",
            f"description={DESCRIPTION}",
        ]
    ]


def test_ensure_reopens_a_closed_session_milestone():
    refused = subprocess.CalledProcessError(1, "gh")
    gh_json, gh, writes = _fake(closed=[_m(WT, number=7)], create_error=refused)
    ensure(WT, REPO, dry_run=False, gh_json=gh_json, gh=gh)
    assert writes[-1] == [
        "api",
        "-X",
        "PATCH",
        "repos/o/r/milestones/7",
        "-f",
        "state=open",
    ]


def test_ensure_refuses_an_operator_milestone_of_the_same_title():
    gh_json, gh, writes = _fake(open_=[_m(WT, description="release 1.0")])
    with pytest.raises(ValueError, match="not a session milestone"):
        ensure(WT, REPO, dry_run=False, gh_json=gh_json, gh=gh)
    assert writes == []


def test_close_idle_closes_only_an_owned_empty_unheld_milestone():
    gh_json, gh, writes = _fake(
        open_=[
            _m("worktree-gone", number=1),
            _m("worktree-live", number=2),
            _m("worktree-busy", number=3, open_issues=1),
            _m("worktree-theirs", number=4, description="operator"),
        ]
    )
    closed = close_idle(
        REPO,
        lambda branch: branch == "worktree-live",
        dry_run=False,
        gh_json=gh_json,
        gh=gh,
    )
    assert closed == ["worktree-gone"]
    assert writes == [
        ["api", "-X", "PATCH", "repos/o/r/milestones/1", "-f", "state=closed"]
    ]


def test_a_batch_pr_joins_the_milestone_of_the_issue_it_closes():
    pr = {
        "headRefName": "worktree-fanout-batch-3",
        "closingIssuesReferences": [{"number": 5}],
    }
    orchestrator = {"title": "worktree-fanout-8741", "description": DESCRIPTION}
    assert pr_title(pr, lambda n: orchestrator) == "worktree-fanout-8741"


def test_a_pr_closing_no_session_issue_joins_its_head_branch_milestone():
    pr = {"headRefName": WT, "closingIssuesReferences": [{"number": 5}]}
    assert pr_title(pr, lambda n: None) == WT


def test_a_pr_from_a_non_session_branch_gets_no_milestone():
    pr = {"headRefName": "renovate/uv-0.9.x", "closingIssuesReferences": []}
    assert pr_title(pr, lambda n: None) is None
