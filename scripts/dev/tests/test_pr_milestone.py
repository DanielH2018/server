"""`pr_milestone.main` against a fake gh."""

from dev.findings_lib.milestone import DESCRIPTION
from dev.pr_milestone import main


def _fake(pr, open_milestones=()):
    writes = []

    def gh_json(*argv, **_kw):
        if argv[0] == "api":
            return [list(open_milestones)]
        return pr

    def gh(*argv, **_kw):
        writes.append(list(argv))

    return gh_json, gh, writes


def test_a_pr_that_already_has_a_milestone_keeps_it():
    pr = {
        "headRefName": "worktree-a",
        "milestone": {"title": "release 1.0"},
        "closingIssuesReferences": [],
    }
    gh_json, gh, writes = _fake(pr)
    assert main(["12"], gh_json=gh_json, gh=gh) == 0
    assert writes == []


def test_a_session_pr_is_added_to_its_milestone():
    pr = {"headRefName": "worktree-a", "milestone": None, "closingIssuesReferences": []}
    open_ = [{"title": "worktree-a", "number": 3, "description": DESCRIPTION}]
    gh_json, gh, writes = _fake(pr, open_)
    assert main(["12", "--repo", "o/r"], gh_json=gh_json, gh=gh) == 0
    assert writes == [
        ["pr", "edit", "12", "--repo", "o/r", "--milestone", "worktree-a"]
    ]
