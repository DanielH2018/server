"""`findings_lib/milestone.py` against a fake `gh api`."""

import pytest

from dev.findings_lib.milestone import (
    attach_target,
    close_idle,
    close_if_done,
    description_for,
    pr_branch,
    start,
)

REPO = "o/r"
WT = "worktree-a"


def _m(title, branch=WT, number=1, state="open", open_issues=0, description=None):
    return {
        "title": title,
        "number": number,
        "state": state,
        "description": description or description_for(branch),
        "open_issues": open_issues,
    }


def _fake(milestones=()):
    writes = []

    def gh_json(*argv, **_kw):
        path = argv[1]
        if "?state=" in path:
            state = path.split("state=")[1].split("&")[0]
            return [[m for m in milestones if m["state"] == state]]
        number = int(path.rsplit("/", 1)[1])
        return next(m for m in milestones if m["number"] == number)

    def gh(*argv, **_kw):
        writes.append(list(argv))

    return gh_json, gh, writes


def _patch(number, state):
    return [
        "api",
        "-X",
        "PATCH",
        f"repos/o/r/milestones/{number}",
        "-f",
        f"state={state}",
    ]


def test_start_creates_the_milestone_under_its_title_with_the_branch_in_the_description():
    gh_json, gh, writes = _fake()
    start("Opt-in milestones", WT, REPO, dry_run=False, gh_json=gh_json, gh=gh)
    assert writes == [
        [
            "api",
            "-X",
            "POST",
            "repos/o/r/milestones",
            "-f",
            "title=Opt-in milestones",
            "-f",
            f"description={description_for(WT)}",
        ]
    ]


@pytest.mark.parametrize("state", ["open", "closed"])
@pytest.mark.parametrize("project", [True, False], ids=["to-project", "to-session"])
def test_start_refuses_to_change_a_milestone_form(state, project):
    held = _m(
        "Backups", state=state, description=description_for(WT, project=not project)
    )
    gh_json, gh, writes = _fake([held])
    with pytest.raises(ValueError, match="start the other form"):
        start(
            "Backups", WT, REPO, dry_run=False, project=project, gh_json=gh_json, gh=gh
        )
    assert writes == []


def test_start_twice_with_the_same_title_writes_nothing():
    gh_json, gh, writes = _fake([_m("Opt-in milestones")])
    start("Opt-in milestones", WT, REPO, dry_run=False, gh_json=gh_json, gh=gh)
    assert writes == []


def test_start_reopens_the_branch_closed_milestone_of_that_title():
    gh_json, gh, writes = _fake([_m("Opt-in milestones", number=4, state="closed")])
    start("Opt-in milestones", WT, REPO, dry_run=False, gh_json=gh_json, gh=gh)
    assert writes == [_patch(4, "open")]


def test_start_refuses_a_second_open_milestone_on_the_branch():
    gh_json, gh, writes = _fake([_m("First focus")])
    with pytest.raises(
        ValueError, match="already has the open milestone `First focus`"
    ):
        start("Second focus", WT, REPO, dry_run=False, gh_json=gh_json, gh=gh)
    assert writes == []


def test_start_refuses_a_title_another_branch_owns():
    gh_json, gh, writes = _fake([_m("Opt-in milestones", branch="worktree-b")])
    with pytest.raises(ValueError, match="for `worktree-b`"):
        start("Opt-in milestones", WT, REPO, dry_run=False, gh_json=gh_json, gh=gh)
    assert writes == []


def test_attach_finds_nothing_for_a_branch_that_never_started_one():
    gh_json, gh, writes = _fake([_m("Other work", branch="worktree-b")])
    assert attach_target(WT, REPO, dry_run=False, gh_json=gh_json, gh=gh) is None
    assert writes == []


def test_attach_reopens_the_branch_closed_milestone():
    gh_json, gh, writes = _fake([_m("Opt-in milestones", number=4, state="closed")])
    title = attach_target(WT, REPO, dry_run=False, gh_json=gh_json, gh=gh)
    assert title == "Opt-in milestones"
    assert writes == [_patch(4, "open")]


def test_close_if_done_closes_an_empty_session_milestone_and_keeps_a_busy_one():
    gh_json, gh, writes = _fake(
        [_m("Done", number=1), _m("Busy", branch="worktree-b", number=2, open_issues=1)]
    )
    assert close_if_done(1, REPO, dry_run=False, gh_json=gh_json, gh=gh)
    assert not close_if_done(2, REPO, dry_run=False, gh_json=gh_json, gh=gh)
    assert writes == [_patch(1, "closed")]


def test_close_if_done_leaves_an_operator_milestone_alone():
    gh_json, gh, writes = _fake([_m("Release 1.0", description="operator's")])
    assert not close_if_done(1, REPO, dry_run=False, gh_json=gh_json, gh=gh)
    assert writes == []


def test_close_idle_closes_only_an_owned_empty_unheld_milestone():
    gh_json, gh, writes = _fake(
        [
            _m("Gone", branch="worktree-gone", number=1),
            _m("Live", branch="worktree-live", number=2),
            _m("Busy", branch="worktree-busy", number=3, open_issues=1),
            _m("Theirs", number=4, description="operator"),
        ]
    )
    closed = close_idle(
        REPO,
        lambda branch: branch == "worktree-live",
        dry_run=False,
        gh_json=gh_json,
        gh=gh,
    )
    assert closed == ["Gone"]
    assert writes == [_patch(1, "closed")]


def test_a_batch_pr_follows_the_issue_it_closes_to_the_orchestrator_branch():
    pr = {
        "headRefName": "worktree-fanout-batch-3",
        "closingIssuesReferences": [{"number": 5}],
    }
    orchestrator = _m("Fan-out", branch="worktree-fanout-8741")
    assert pr_branch(pr, lambda n: orchestrator) == "worktree-fanout-8741"


def test_a_pr_closing_no_session_issue_uses_its_head_branch():
    pr = {"headRefName": WT, "closingIssuesReferences": [{"number": 5}]}
    assert pr_branch(pr, lambda n: None) == WT


def test_a_batch_pr_follows_its_issue_into_a_project_milestone():
    pr = {
        "headRefName": "worktree-fanout-batch-3",
        "closingIssuesReferences": [{"number": 5}],
    }
    project = _m("Backups", description=description_for("worktree-lead", project=True))
    assert pr_branch(pr, lambda n: project) == "worktree-lead"
