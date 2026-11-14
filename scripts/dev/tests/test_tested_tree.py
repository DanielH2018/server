"""tested_tree answers `proven` only for a green status this repository's CI recorded.

A push must carry the recorded tree. A pull-request head needs a record for any tree.

Run: uv run pytest scripts/dev/tests/test_tested_tree.py
"""

import pytest

import tested_tree

REPO_NAME = "o/r"
SQUASH = "a" * 40
HEAD = "b" * 40
TREE = "c" * 40
OTHER_TREE = "d" * 40


def _api(pulls, statuses):
    """A `get` that serves the two endpoints; an Exception value is raised."""

    def get(path):
        body = pulls if path.endswith("/pulls") else statuses
        if isinstance(body, Exception):
            raise body
        return body

    return get


def _pr(merge_commit_sha=SQUASH):
    return [{"merge_commit_sha": merge_commit_sha, "head": {"sha": HEAD}}]


RUN_URL = f"https://github.com/{REPO_NAME}/actions/runs/123456"


def _status(
    tree=TREE,
    state="success",
    context=tested_tree.CONTEXT,
    login=tested_tree.CREATOR,
    target_url=RUN_URL,
):
    return [
        {
            "context": context,
            "state": state,
            "description": tree,
            "creator": {"login": login},
            "target_url": target_url,
        }
    ]


def _prove(pulls, statuses, tree=TREE, sha=SQUASH):
    return tested_tree.tree_is_proven(_api(pulls, statuses), REPO_NAME, sha, tree)[0]


def test_equal_trees_are_proven():
    assert _prove(_pr(), _status()) is True


def test_unequal_trees_are_not_proven():
    assert _prove(_pr(), _status(tree=OTHER_TREE)) is False


def test_a_failed_or_foreign_status_is_not_proven():
    assert _prove(_pr(), _status(state="failure")) is False
    assert _prove(_pr(), _status(context="ci/other")) is False


def test_a_status_from_another_creator_is_not_proven():
    assert _prove(_pr(), _status(login="fake-operator")) is False
    status = _status()
    del status[0]["creator"]
    assert _prove(_pr(), status) is False


def test_a_status_not_pointing_at_this_repositorys_runs_is_not_proven():
    for url in (
        None,
        "",
        "https://example.invalid/o/r/actions/runs/1",
        "https://github.com/fake-owner/r/actions/runs/1",
        f"https://github.com/{REPO_NAME}/actions/runs/",
        f"https://github.com/{REPO_NAME}/actions/runs/1/attempts/2",
        f"https://github.com/{REPO_NAME}x/actions/runs/1",
    ):
        assert _prove(_pr(), _status(target_url=url)) is False, url


def test_a_pr_that_merely_contains_the_commit_is_not_proven():
    assert _prove(_pr(merge_commit_sha="e" * 40), _status()) is False


def test_a_commit_with_no_pr_is_not_proven():
    assert _prove([], _status()) is False


def test_an_unreadable_api_is_not_proven():
    assert _prove(OSError("boom"), _status()) is False
    assert _prove(_pr(), ValueError("not json")) is False
    assert _prove({"message": "Not Found"}, _status()) is False


def test_an_unreadable_tree_is_not_proven():
    """An empty or malformed id must not equal an empty status description."""
    assert _prove(_pr(), _status(tree=""), tree="") is False
    assert _prove(_pr(), _status(tree="nope"), tree="nope") is False
    assert _prove(_pr(), _status(), sha="") is False


def _prove_head(statuses, head=HEAD):
    return tested_tree.head_is_proven(_api([], statuses), REPO_NAME, head)[0]


def test_a_head_with_a_green_record_for_any_tree_is_proven():
    # A head carries one status per context and run, newest first.
    newer = _status(context="ci/other") + _status(state="failure")
    assert _prove_head(newer + _status(tree=OTHER_TREE)) is True


@pytest.mark.parametrize(
    "statuses",
    [
        [],
        _status(login="fake-operator"),
        _status(target_url="https://github.com/fake-owner/r/actions/runs/1"),
        _status(tree="a batch"),
        OSError("down"),
    ],
    ids=["none", "creator", "run-url", "no-tree", "unreadable"],
)
def test_a_head_without_a_green_record_from_this_repositorys_ci_is_not_proven(statuses):
    assert _prove_head(statuses) is False


def test_main_answers_the_pull_request_question_from_the_head_alone(
    tmp_path, monkeypatch
):
    output = tmp_path / "out"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    argv = ["prove-head", "--repo", REPO_NAME, "--head", HEAD]

    assert tested_tree.main(argv, _api(OSError("no pulls read"), _status())) == 0
    assert output.read_text() == "proven=true\n"


def test_main_writes_the_answer_and_exits_zero_when_the_lookup_fails(
    tmp_path, monkeypatch, capsys
):
    output = tmp_path / "out"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    argv = ["prove", "--repo", REPO_NAME, "--sha", SQUASH, "--tree", TREE]

    assert tested_tree.main(argv, _api(OSError("down"), [])) == 0
    assert output.read_text() == "proven=false\n"
    assert "proven=false" in capsys.readouterr().out
