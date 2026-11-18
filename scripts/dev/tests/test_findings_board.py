"""`board.sync_board` against a fake `gh api graphql`: the read, the plan and the mutations.

`FakeGh` answers the aliased read from `issues`, `board` and `issue_fields`, and records
each mutation's query text, so a test asserts which writes went out without restating the
GraphQL.
"""

import json
import subprocess

from _findings_fakes import operator_comment

from dev.findings_lib.board import (
    DONE,
    IN_PROGRESS,
    MUTATION_PAUSE_S,
    PROJECT_TITLE,
    TODO,
    sync_board,
    wanted_fields,
)
from dev.findings_lib.issue_model import claim_comment

BOARD_ID = "PVT_board"
STATUS_FIELD = {
    "id": "IF_status",
    "name": "Status",
    "options": [
        {"id": "o_todo", "name": TODO},
        {"id": "o_prog", "name": IN_PROGRESS},
        {"id": "o_done", "name": DONE},
    ],
}


def _board(*, fields=("Severity", "Not before")) -> dict:
    known = {
        "Severity": {
            "id": "F_sev",
            "name": "Severity",
            "options": [{"id": f"o_{s}", "name": s} for s in ("high", "medium", "low")],
        },
        "Not before": {"id": "F_nb", "name": "Not before"},
    }
    return {
        "id": BOARD_ID,
        "title": PROJECT_TITLE,
        "fields": {"nodes": [known[f] for f in fields]},
    }


def _issue(
    number, *, labels=(), state="OPEN", prs=(), held=None, status=None, comments=()
) -> dict:
    items = []
    if held is not None:
        values = [
            {"date": v, "field": {"name": k}}
            if k == "Not before"
            else {"name": v, "field": {"name": k}}
            for k, v in held.items()
        ]
        items.append(
            {
                "id": f"item{number}",
                "project": {"id": BOARD_ID},
                "fieldValues": {"nodes": values},
            }
        )
    field_values = [{"name": status, "field": {"name": "Status"}}] if status else []
    return {
        "id": f"I_{number}",
        "number": number,
        "state": state,
        "labels": {"nodes": [{"name": n} for n in labels]},
        "comments": {"nodes": [operator_comment(c) for c in comments]},
        "closedByPullRequestsReferences": {"nodes": [{"state": s} for s in prs]},
        "issueFieldValues": {"nodes": field_values},
        "projectItems": {"nodes": items},
    }


def _claimed(worktree="worktree-a"):
    return [claim_comment(worktree, None, "2026-11-18T10:00Z")]


class FakeGh:
    """A `gh` that answers the board read and records every mutation."""

    def __init__(self, issues, board=None, *, issue_fields=(STATUS_FIELD,), fail=None):
        self.issues = {i["number"]: i for i in issues}
        self.board = _board() if board is None else board
        self.issue_fields = list(issue_fields)
        self.fail = fail
        self.mutations: list[str] = []

    def __call__(self, *argv, **_):
        query = argv[argv.index("-f") + 1].removeprefix("query=")
        if self.fail:
            raise subprocess.CalledProcessError(1, "gh", stderr=self.fail)
        if query.startswith("mutation"):
            self.mutations.append(query)
            data = {
                line.split(":", 1)[0]: {"item": {"id": "new_" + line.split(":", 1)[0]}}
                for line in query.splitlines()
                if line.startswith("a")
            }
            return subprocess.CompletedProcess(argv, 0, json.dumps({"data": data}), "")
        repository = {
            "owner": {"projectsV2": {"nodes": [self.board]}},
            "issueFields": {"nodes": self.issue_fields},
        }
        for n, issue in self.issues.items():
            if f"i{n}:" in query:
                repository[f"i{n}"] = issue
        for word in query.split():
            if word.startswith("i") and word.endswith(":") and word[1:-1].isdigit():
                repository.setdefault(word[:-1], None)
        body = json.dumps({"data": {"repository": repository}})
        return subprocess.CompletedProcess(argv, 0, body, "")


def test_status_follows_the_claim_a_closing_pr_and_the_close():
    assert wanted_fields("OPEN", {"claude"}, 0, claimed=False)["Status"] == TODO
    assert wanted_fields("OPEN", set(), 0, claimed=True)["Status"] == IN_PROGRESS
    assert wanted_fields("OPEN", set(), 1, claimed=False)["Status"] == IN_PROGRESS
    assert wanted_fields("CLOSED", set(), 0, claimed=True)["Status"] == DONE


def test_severity_and_not_before_come_from_their_labels():
    labels = {"severity/high", "not-before:2026-12-01"}
    fields = wanted_fields("OPEN", labels, 0, claimed=False)
    assert fields["Severity"] == "high"
    assert fields["Not before"] == "2026-12-01"
    assert wanted_fields("OPEN", set(), 0, claimed=False)["Severity"] is None


def test_a_claim_comment_sets_the_issues_status_field_to_in_progress():
    gh = FakeGh(
        [
            _issue(
                7,
                labels=["severity/low"],
                held={"Severity": "low"},
                status=TODO,
                comments=_claimed(),
            )
        ]
    )

    outcomes = sync_board([7], None, gh=gh)

    assert outcomes == {7: ("updated", "Status Todo -> In Progress")}
    assert len(gh.mutations) == 1
    assert "setIssueFieldValue" in gh.mutations[0]
    assert '"I_7"' in gh.mutations[0] and '"o_prog"' in gh.mutations[0]


def test_an_issue_missing_from_the_board_is_added_then_written_after_a_pause():
    gh = FakeGh([_issue(9, labels=["severity/medium"])])
    pauses = []

    outcomes = sync_board([9], None, gh=gh, sleep=pauses.append)

    assert outcomes[9] == (
        "updated",
        "added; Status empty -> Todo; Severity empty -> medium",
    )
    assert "addProjectV2ItemById" in gh.mutations[0]
    assert "setIssueFieldValue" in gh.mutations[1] and '"new_a9"' in gh.mutations[1]
    assert pauses == [MUTATION_PAUSE_S]


def test_a_cleared_label_clears_the_board_field():
    gh = FakeGh([_issue(4, held={"Not before": "2026-11-01"}, status=TODO)])

    assert sync_board([4], None, gh=gh)[4][1] == "Not before 2026-11-01 -> empty"
    assert "clearProjectV2ItemFieldValue" in gh.mutations[0]


def test_an_agreeing_issue_writes_nothing():
    gh = FakeGh([_issue(5, held={}, status=TODO)])

    assert sync_board([5], None, gh=gh) == {5: ("unchanged", "")}
    assert gh.mutations == []


def test_a_dry_run_plans_but_writes_nothing():
    gh = FakeGh([_issue(7, held={}, status=TODO, comments=_claimed())])

    assert sync_board([7], None, dry_run=True, gh=gh)[7][0] == "updated"
    assert gh.mutations == []


def test_a_board_without_a_field_fails_the_issue_and_names_the_field():
    gh = FakeGh([_issue(7, held={}, status=TODO)], _board(fields=("Severity",)))

    assert sync_board([7], None, gh=gh) == {
        7: ("failed", 'board has no "Not before" field')
    }
    assert gh.mutations == []


def test_an_account_without_the_status_issue_field_names_where_to_add_it():
    gh = FakeGh([_issue(7, held={})], issue_fields=())

    assert sync_board([7], None, gh=gh) == {
        7: (
            "failed",
            'no "Status" issue field; add it under Settings > Planning > Issue fields',
        )
    }
    assert gh.mutations == []


def test_a_number_that_is_not_an_issue_fails_alone():
    gh = FakeGh([_issue(5, held={}, status=TODO)])

    assert sync_board([5, 6], None, gh=gh) == {
        5: ("unchanged", ""),
        6: ("failed", "not an issue"),
    }


def test_a_gh_refusal_fails_every_issue_without_raising():
    gh = FakeGh(
        [], fail="gh: Your token has not been granted the required scopes\nmore"
    )

    assert sync_board([1, 2], None, gh=gh) == {
        1: ("failed", "gh: Your token has not been granted the required scopes"),
        2: ("failed", "gh: Your token has not been granted the required scopes"),
    }


def test_a_board_with_another_title_is_not_written():
    board = _board() | {"title": f"{PROJECT_TITLE} archive"}
    gh = FakeGh([_issue(7)], board)

    assert sync_board([7], None, gh=gh)[7] == (
        "failed",
        f'no Project titled "{PROJECT_TITLE}"',
    )
