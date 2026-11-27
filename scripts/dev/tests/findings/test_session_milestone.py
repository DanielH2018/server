"""`session_milestone.main` against a fake gh."""

import subprocess

from dev.findings_lib.milestone import description_for
from dev.session_milestone import main
from lib.json_types import JsonValue

FOCUS = {
    "title": "Opt-in milestones",
    "number": 3,
    "state": "open",
    "description": description_for("worktree-a"),
}


def _fake(pr, milestones=()):
    writes = []

    def gh_json(*argv, **_kw) -> JsonValue:
        if argv[0] == "api":
            return [[m for m in milestones if f"state={m['state']}" in argv[1]]]
        return pr

    def gh(*argv, **_kw) -> subprocess.CompletedProcess[str]:
        writes.append(list(argv))
        return subprocess.CompletedProcess(list(argv), 0, "", "")

    return gh_json, gh, writes


def test_a_pr_that_already_has_a_milestone_keeps_it():
    pr = {
        "headRefName": "worktree-a",
        "milestone": {"title": "Release 1.0"},
        "closingIssuesReferences": [],
    }
    gh_json, gh, writes = _fake(pr, [FOCUS])
    assert main(["attach-pr", "12"], gh_json=gh_json, gh=gh) == 0
    assert writes == []


def test_a_pr_is_filed_under_its_branch_milestone():
    pr = {"headRefName": "worktree-a", "milestone": None, "closingIssuesReferences": []}
    gh_json, gh, writes = _fake(pr, [FOCUS])
    assert main(["attach-pr", "12", "--repo", "o/r"], gh_json=gh_json, gh=gh) == 0
    assert writes == [
        ["pr", "edit", "12", "--repo", "o/r", "--milestone", "Opt-in milestones"]
    ]


def test_a_pr_from_a_branch_with_no_milestone_gets_none():
    pr = {"headRefName": "worktree-b", "milestone": None, "closingIssuesReferences": []}
    gh_json, gh, writes = _fake(pr, [FOCUS])
    assert main(["attach-pr", "12"], gh_json=gh_json, gh=gh) == 0
    assert writes == []
