"""`board.sync_board` against a fake `gh api graphql`: the read, the plan and the mutations.

`FakeGh` answers the aliased read from `issues` and `board`, and records each mutation's
query text, so a test asserts which writes went out without restating the GraphQL.
"""

import json
import subprocess

from _findings_fakes import operator_comment

from dev.findings_lib.issue_model import claim_comment
from dev.findings_lib.board import (
    DONE,
    IN_PROGRESS,
    MUTATION_PAUSE_S,
    PROJECT_TITLE,
    TODO,
    sync_board,
    wanted_status,
)

BOARD_ID = "PVT_board"
CLAIMED = (claim_comment("worktree-a", None, "2026-11-18T10:00Z"),)
STATUS_FIELD = {
    "id": "F_status",
    "name": "Status",
    "options": [
        {"id": "o_todo", "name": TODO},
        {"id": "o_prog", "name": IN_PROGRESS},
        {"id": "o_done", "name": DONE},
    ],
}


def _board(*, fields=(STATUS_FIELD,)) -> dict:
    return {"id": BOARD_ID, "title": PROJECT_TITLE, "fields": {"nodes": list(fields)}}


def _issue(number, *, state="OPEN", prs=(), status=None, comments=()) -> dict:
    """One issue as the read returns it; ``status`` puts it on the board with that Status."""
    items = []
    if status is not None:
        items.append(
            {
                "id": f"item{number}",
                "project": {"id": BOARD_ID},
                "fieldValues": {
                    "nodes": [{"name": status, "field": {"name": "Status"}}]
                },
            }
        )
    return {
        "id": f"I_{number}",
        "number": number,
        "state": state,
        "comments": {"nodes": [operator_comment(c) for c in comments]},
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
        repository: dict[str, object] = {
            "owner": {"projectsV2": {"nodes": [self.board]}}
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
    assert wanted_status("OPEN", 0, claimed=False) == TODO
    assert wanted_status("OPEN", 0, claimed=True) == IN_PROGRESS
    assert wanted_status("OPEN", 1, claimed=False) == IN_PROGRESS
    assert wanted_status("CLOSED", 0, claimed=True) == DONE


def test_a_claim_comment_moves_the_board_status_to_in_progress():
    gh = FakeGh([_issue(7, status=TODO, comments=CLAIMED)])

    assert sync_board([7], None, gh=gh) == {
        7: ("updated", "Status Todo -> In Progress")
    }
    [mutation] = gh.mutations
    assert '"o_prog"' in mutation and '"item7"' in mutation


def test_an_issue_missing_from_the_board_is_added_then_written_after_a_pause():
    gh = FakeGh([_issue(9)])
    pauses = []

    assert sync_board([9], None, gh=gh, sleep=pauses.append)[9] == (
        "updated",
        "added; Status empty -> Todo",
    )
    assert "addProjectV2ItemById" in gh.mutations[0]
    assert '"new_a9"' in gh.mutations[1]
    assert pauses == [MUTATION_PAUSE_S]


def test_an_agreeing_issue_writes_nothing():
    gh = FakeGh([_issue(5, status=TODO)])

    assert sync_board([5], None, gh=gh) == {5: ("unchanged", "")}
    assert gh.mutations == []


def test_a_dry_run_plans_but_writes_nothing():
    gh = FakeGh([_issue(7, status=TODO, comments=CLAIMED)])

    assert sync_board([7], None, dry_run=True, gh=gh)[7][0] == "updated"
    assert gh.mutations == []


def test_a_board_without_a_status_field_fails_the_issue_and_names_the_field():
    gh = FakeGh([_issue(7, status=TODO)], _board(fields=()))

    assert sync_board([7], None, gh=gh) == {
        7: ("failed", 'board has no "Status" field')
    }
    assert gh.mutations == []


def test_a_number_that_is_not_an_issue_fails_alone():
    gh = FakeGh([_issue(5, status=TODO)])

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
