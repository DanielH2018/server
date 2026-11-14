"""`board.sync_board` against a fake `gh api graphql`: the read, the plan and the mutations.

`FakeGh` answers the aliased read from `issues` and `board`, and records each mutation's
query text, so a test asserts which writes went out without restating the GraphQL.
"""

import json
import subprocess

from dev.findings_lib.board import (
    DONE,
    IN_PROGRESS,
    PROJECT_TITLE,
    TODO,
    sync_board,
    wanted_fields,
)

BOARD_ID = "PVT_board"


def _board(*, fields=("Status", "Severity", "Not before")) -> dict:
    known = {
        "Status": {
            "id": "F_status",
            "name": "Status",
            "options": [
                {"id": "o_todo", "name": TODO},
                {"id": "o_prog", "name": IN_PROGRESS},
                {"id": "o_done", "name": DONE},
            ],
        },
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


def _issue(number, *, labels=(), state="OPEN", prs=(), held=None) -> dict:
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
    return {
        "id": f"I_{number}",
        "number": number,
        "state": state,
        "labels": {"nodes": [{"name": n} for n in labels]},
        "closedByPullRequestsReferences": {"nodes": [{"state": s} for s in prs]},
        "projectItems": {"nodes": items},
    }


class FakeGh:
    """A `gh` that answers the board read and records every mutation."""

    def __init__(self, issues, board=None, *, fail=None):
        self.issues = {i["number"]: i for i in issues}
        self.board = _board() if board is None else board
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
        repository = {"owner": {"projectsV2": {"nodes": [self.board]}}}
        for n, issue in self.issues.items():
            if f"i{n}:" in query:
                repository[f"i{n}"] = issue
        for word in query.split():
            if word.startswith("i") and word.endswith(":") and word[1:-1].isdigit():
                repository.setdefault(word[:-1], None)
        body = json.dumps({"data": {"repository": repository}})
        return subprocess.CompletedProcess(argv, 0, body, "")


def test_status_follows_the_claim_a_closing_pr_and_the_close():
    assert wanted_fields("OPEN", {"claude"}, 0)["Status"] == TODO
    assert wanted_fields("OPEN", {"claimed"}, 0)["Status"] == IN_PROGRESS
    assert wanted_fields("OPEN", set(), 1)["Status"] == IN_PROGRESS
    assert wanted_fields("CLOSED", {"claimed"}, 0)["Status"] == DONE


def test_severity_and_not_before_come_from_their_labels():
    fields = wanted_fields("OPEN", {"severity/high", "not-before:2026-12-01"}, 0)
    assert fields["Severity"] == "high"
    assert fields["Not before"] == "2026-12-01"
    assert wanted_fields("OPEN", set(), 0)["Severity"] is None


def test_a_claimed_issue_on_the_board_moves_to_in_progress():
    gh = FakeGh(
        [
            _issue(
                7,
                labels=["claimed", "severity/low"],
                held={"Status": TODO, "Severity": "low"},
            )
        ]
    )

    outcomes = sync_board([7], None, gh=gh)

    assert outcomes == {7: ("updated", "Status Todo -> In Progress")}
    assert len(gh.mutations) == 1
    assert '"o_prog"' in gh.mutations[0] and '"item7"' in gh.mutations[0]


def test_an_issue_missing_from_the_board_is_added_then_written():
    gh = FakeGh([_issue(9, labels=["severity/medium"])])

    outcomes = sync_board([9], None, gh=gh)

    assert outcomes[9] == (
        "updated",
        "added; Status empty -> Todo; Severity empty -> medium",
    )
    assert "addProjectV2ItemById" in gh.mutations[0]
    assert '"new_a9"' in gh.mutations[1]


def test_a_cleared_label_clears_the_field():
    gh = FakeGh([_issue(4, held={"Status": TODO, "Not before": "2026-11-01"})])

    assert sync_board([4], None, gh=gh)[4][1] == "Not before 2026-11-01 -> empty"
    assert "clearProjectV2ItemFieldValue" in gh.mutations[0]


def test_an_agreeing_issue_writes_nothing():
    gh = FakeGh([_issue(5, held={"Status": TODO})])

    assert sync_board([5], None, gh=gh) == {5: ("unchanged", "")}
    assert gh.mutations == []


def test_a_dry_run_plans_but_writes_nothing():
    gh = FakeGh([_issue(7, labels=["claimed"], held={"Status": TODO})])

    assert sync_board([7], None, dry_run=True, gh=gh)[7][0] == "updated"
    assert gh.mutations == []


def test_a_board_without_a_field_fails_the_issue_and_names_the_field():
    gh = FakeGh(
        [_issue(7, held={"Status": TODO})], _board(fields=("Status", "Severity"))
    )

    assert sync_board([7], None, gh=gh) == {
        7: ("failed", 'board has no "Not before" field')
    }
    assert gh.mutations == []


def test_a_number_that_is_not_an_issue_fails_alone():
    gh = FakeGh([_issue(5, held={"Status": TODO})])

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
