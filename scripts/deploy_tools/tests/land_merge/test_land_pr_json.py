"""The PR JSON the lander reads: each field narrowed to its type where `gh_json` returns it.

Run: uv run pytest scripts/deploy_tools/tests/land_merge/test_land_pr_json.py
"""

import json
import shutil
import subprocess

import pytest

from deploy_tools.land_lib.pr_json import (
    parse_file,
    parse_review,
    parse_view,
    rest_filter,
)


def test_a_view_keeps_the_fields_the_lander_reads():
    view = parse_view(
        {
            "author": {"login": "app/renovate", "is_bot": True},
            "files": [{"path": "a.yml", "additions": 1}],
            "changedFiles": 1,
            "isCrossRepository": False,
            "mergeCommit": None,
            "body": None,
            "commits": [
                {"oid": "a1", "messageHeadline": "Drop it", "messageBody": None}
            ],
        }
    )
    assert view == {
        "author": {"login": "app/renovate"},
        "files": [{"path": "a.yml"}],
        "changedFiles": 1,
        "isCrossRepository": False,
        "mergeCommit": None,
        "body": "",
        "commits": [{"messageHeadline": "Drop it", "messageBody": ""}],
    }


def test_a_view_field_no_parser_reads_is_refused():
    with pytest.raises(KeyError, match="url"):
        parse_view({"state": "OPEN", "url": "https://example.invalid/pr/1"})


@pytest.mark.parametrize(
    "obj",
    [
        {"headRefOid": 7},
        {"isCrossRepository": "false"},
        {"changedFiles": True},
        {"files": [{"path": None}, "a.yml"]},
        {"mergeCommit": "abc"},
        {"author": ["octocat"]},
        {"author": {"login": 5}},
        {"mergeCommit": ["abc"]},
        {"commits": [5]},
    ],
)
def test_a_view_field_of_the_wrong_type_is_refused(obj):
    """The error names the field, so `Landing.view`'s refusal can be traced to it."""
    with pytest.raises(ValueError, match=f"^{next(iter(obj))}"):
        parse_view(obj)


def test_a_rename_keeps_its_old_path_status_and_blob():
    entry = {"filename": "b", "previous_filename": "a", "status": "renamed", "sha": "e"}
    assert parse_file({**entry, "patch": "@@"}) == entry


def test_a_file_entry_without_a_status_is_refused():
    """A guessed status could skip an import-shape check, so none is guessed."""
    with pytest.raises(ValueError, match=r"^status"):
        parse_file({"filename": "b"})


def test_a_review_by_a_deleted_account_has_no_user():
    review = parse_review({"user": None, "state": "APPROVED", "commit_id": "abc"})
    assert review == {
        "user": None,
        "state": "APPROVED",
        "submitted_at": "",
        "commit_id": "abc",
    }


def _pull(**fields):
    """A REST pull object: open, ready and mergeable unless `fields` says otherwise."""
    pull = {"state": "open", "merged": False, "draft": False, "mergeable": True}
    return {**pull, "head": {"sha": "abc", "ref": "a-branch"}, **fields}


@pytest.mark.parametrize(
    ("fields", "pull", "view"),
    [
        ("state,mergeable", _pull(), {"state": "OPEN", "mergeable": "MERGEABLE"}),
        ("mergeable", _pull(mergeable=None), {"mergeable": "UNKNOWN"}),
        ("mergeable", _pull(mergeable=False), {"mergeable": "CONFLICTING"}),
        ("state", _pull(state="closed"), {"state": "CLOSED"}),
        ("state", _pull(state="closed", merged=True), {"state": "MERGED"}),
        ("isDraft,headRefOid", _pull(), {"isDraft": False, "headRefOid": "abc"}),
        ("isDraft", _pull(draft=True), {"isDraft": True}),
    ],
)
def test_the_rest_filter_answers_as_gh_pr_view_does(fields, pull, view):
    jq, asked = shutil.which("jq"), rest_filter(fields)
    assert jq, "this test runs the filter with jq; gh applies it with its own"
    assert asked
    answer = subprocess.run(
        [jq, "-c", asked],
        input=json.dumps(pull),
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    assert parse_view(json.loads(answer.stdout)) == view


def test_a_field_the_rest_pull_object_lacks_has_no_rest_filter():
    assert rest_filter("state,reviewDecision") is None
