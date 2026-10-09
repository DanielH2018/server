"""The PR JSON the lander reads: each field narrowed to its type where `gh_json` returns it.

Run: uv run pytest scripts/deploy_tools/tests/test_land_pr_json.py
"""

import pytest

from deploy_tools.land_lib.pr_json import parse_file, parse_review, parse_view


def test_a_view_keeps_the_fields_the_lander_reads():
    view = parse_view(
        {
            "author": {"login": "app/renovate", "is_bot": True},
            "files": [{"path": "a.yml", "additions": 1}],
            "changedFiles": 1,
            "isCrossRepository": False,
            "mergeCommit": None,
            "body": None,
        }
    )
    assert view == {
        "author": {"login": "app/renovate"},
        "files": [{"path": "a.yml"}],
        "changedFiles": 1,
        "isCrossRepository": False,
        "mergeCommit": None,
        "body": "",
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
    ],
)
def test_a_view_field_of_the_wrong_type_is_refused(obj):
    """The error names the field, so `Landing.view`'s refusal can be traced to it."""
    with pytest.raises(ValueError, match=f"^{next(iter(obj))}"):
        parse_view(obj)


def test_a_rename_keeps_its_old_path():
    assert parse_file({"filename": "b", "previous_filename": "a", "status": "x"}) == {
        "filename": "b",
        "previous_filename": "a",
    }


def test_a_review_by_a_deleted_account_has_no_user():
    review = parse_review({"user": None, "state": "APPROVED", "commit_id": "abc"})
    assert review == {
        "user": None,
        "state": "APPROVED",
        "submitted_at": "",
        "commit_id": "abc",
    }
