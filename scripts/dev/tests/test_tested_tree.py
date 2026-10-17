"""tested_tree answers `proven` only for a green status that carries the pushed tree.

Run: uv run pytest scripts/dev/tests/test_tested_tree.py
"""

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


def _status(tree=TREE, state="success", context=tested_tree.CONTEXT):
    return [{"context": context, "state": state, "description": tree}]


def _prove(pulls, statuses, tree=TREE, sha=SQUASH):
    return tested_tree.tree_is_proven(_api(pulls, statuses), REPO_NAME, sha, tree)[0]


def test_equal_trees_are_proven():
    assert _prove(_pr(), _status()) is True


def test_unequal_trees_are_not_proven():
    assert _prove(_pr(), _status(tree=OTHER_TREE)) is False


def test_a_failed_or_foreign_status_is_not_proven():
    assert _prove(_pr(), _status(state="failure")) is False
    assert _prove(_pr(), _status(context="ci/other")) is False


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


def test_main_writes_the_answer_and_exits_zero_when_the_lookup_fails(
    tmp_path, monkeypatch, capsys
):
    output = tmp_path / "out"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    argv = ["prove", "--repo", REPO_NAME, "--sha", SQUASH, "--tree", TREE]

    assert tested_tree.main(argv, _api(OSError("down"), [])) == 0
    assert output.read_text() == "proven=false\n"
    assert "proven=false" in capsys.readouterr().out
