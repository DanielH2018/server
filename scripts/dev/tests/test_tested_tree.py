"""tested_tree answers `proven` only for a green status this repository's CI recorded.

A push must carry the recorded tree. A pull-request head needs a record for any tree.

Run: uv run pytest scripts/dev/tests/test_tested_tree.py
"""

import json

import pytest

import tested_tree
from lib.git_testing import commit, git, git_out, init_repo

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


@pytest.mark.parametrize(
    ("body", "members"),
    [
        ("Landing batch.\n\nMembers: #7 #8 #9\n", [7, 8, 9]),
        ("Members: #7 #8\r\nmore\r\n", [7, 8]),
        ("Members: #1 #2 #3 #4", [1, 2, 3, 4]),
        ("Members: #1 #2 #3 #4 #5", []),
        ("Members: 7 8", []),
        ("see Members: #7", []),
        ("Members: #7 #8 and more", []),
        ("Members: #7\nMembers: #8", []),
        ("Members: #7 #7", []),
    ],
    ids=[
        *["named", "crlf", "four", "five", "bare", "inline", "trailing"],
        *["two-lines", "repeated"],
    ],
)
def test_a_batch_names_its_members_on_one_line_or_names_none(body, members):
    assert tested_tree.batch_members(body) == members


def _batch(tmp_path, second: dict[str, str] | None = None):
    """A checkout whose origin serves two member heads; (checkout, base, heads, trees).

    The checkout holds master alone, as a pull-request run's does, so the heads reach it only
    through the fetch. The trees are what git's own squash merge of each head makes, in turn.
    `second` is what the second member changes; the first changes `a.txt`.
    """
    origin = init_repo(tmp_path / "origin")
    git(origin, "config", "uploadpack.allowAnySHA1InWant", "true")
    base = commit(origin, "base", **{"a.txt": "a\n", "b.txt": "b\n"})
    heads = []
    for name, files in (
        ("one", {"a.txt": "one\n"}),
        ("two", second or {"b.txt": "two\n"}),
    ):
        git(origin, "checkout", "-q", "-b", name, base)
        heads.append(commit(origin, name, **files))
    git(origin, "checkout", "-q", "-b", "squashed", base)
    trees = []
    for head in heads:
        if git(origin, "merge", "--squash", "-q", head, check=False).returncode:
            break
        git(origin, "commit", "-q", "-m", "squash", "--no-gpg-sign")
        trees.append(git_out(origin, "rev-parse", "HEAD^{tree}"))
    url = f"file://{origin}"
    git(tmp_path, "clone", "-q", "--single-branch", "-b", "master", url, "checkout")
    return tmp_path / "checkout", base, heads, trees


def _pull(head, state="open", head_repo=REPO_NAME):
    return {"state": state, "head": {"sha": head, "repo": {"full_name": head_repo}}}


def _record(checkout, base, pulls, tested):
    """`record_batch` for members #1 and #2; (what it says, the statuses it posted)."""
    posted = []

    def get(path):
        answer = pulls[int(path.rsplit("/", 1)[1]) - 1]
        if isinstance(answer, Exception):
            raise answer
        return answer

    told = tested_tree.record_batch(
        get,
        lambda path, body: posted.append((path, body)),
        REPO_NAME,
        [1, 2],
        base,
        tested,
        RUN_URL,
        cwd=checkout,
    )
    return told, posted


def _statuses(heads, trees):
    return [
        (
            f"repos/{REPO_NAME}/statuses/{head}",
            {
                "state": "success",
                "context": tested_tree.CONTEXT,
                "description": tree,
                "target_url": RUN_URL,
            },
        )
        for head, tree in zip(heads, trees, strict=True)
    ]


def test_a_batch_records_each_members_stacked_tree_on_its_head(tmp_path):
    checkout, base, heads, trees = _batch(tmp_path)
    told, posted = _record(checkout, base, [_pull(h) for h in heads], trees[-1])
    assert posted == _statuses(heads, trees)
    assert told == f"recorded #1 {heads[0][:8]}, #2 {heads[1][:8]}"
    # What it wrote is what the readers accept.
    assert _prove_head([{**posted[0][1], "creator": {"login": tested_tree.CREATOR}}])


@pytest.mark.parametrize(
    ("case", "why"),
    [
        ("another-tree", "and the run tested"),
        ("fork", "nothing: #2 is not an open pull request from a branch of this"),
        ("closed", "nothing: #1 is not an open pull request from a branch of this"),
        ("conflict", "nothing: #2 does not merge onto the run's base"),
        ("unreadable", "recording failed: OSError"),
        ("unfetchable", "recording failed: CalledProcessError"),
    ],
)
def test_a_batch_the_gate_refuses_or_cannot_rebuild_records_nothing(
    tmp_path, case, why
):
    second = {"a.txt": "two\n"} if case == "conflict" else None
    checkout, base, heads, trees = _batch(tmp_path, second)
    pulls = [_pull(h) for h in heads]
    tested = trees[-1]
    if case == "another-tree":
        # The run tested the first member alone, so the batch is not the stack it names.
        tested = trees[0]
    elif case == "fork":
        pulls[1] = _pull(heads[1], head_repo="fork/r")
    elif case == "closed":
        pulls[0] = _pull(heads[0], state="closed")
    elif case == "unreadable":
        pulls[1] = OSError("down")
    elif case == "unfetchable":
        pulls[1] = _pull("f" * 40)
    told, posted = _record(checkout, base, pulls, tested)
    assert posted == []
    assert why in told


def test_main_records_the_batch_its_body_names_and_exits_zero(
    tmp_path, monkeypatch, capsys
):
    checkout, base, heads, trees = _batch(tmp_path)
    monkeypatch.chdir(checkout)
    monkeypatch.setenv(tested_tree.BODY_ENV, "A batch.\n\nMembers: #1 #2\n")
    posted = []
    argv = ["record-batch", "--repo", REPO_NAME, "--base", base]
    argv += ["--tested-tree", trees[-1], "--run-url", RUN_URL]

    def get(path):
        return _pull(heads[int(path.rsplit("/", 1)[1]) - 1])

    assert tested_tree.main(argv, get, lambda *call: posted.append(call)) == 0
    assert posted == _statuses(heads, trees)
    assert "recorded #1" in capsys.readouterr().out


@pytest.mark.parametrize("body", [None, {"state": "success"}])
def test_a_request_with_a_body_is_a_post_of_that_body_as_json(body):
    request = tested_tree._request("the-token", "repos/o/r/statuses/abc", body)
    assert request.full_url == "https://api.github.com/repos/o/r/statuses/abc"
    assert request.get_header("Authorization") == "Bearer the-token"
    assert request.get_method() == ("GET" if body is None else "POST")
    sent = request.data
    assert (json.loads(sent) if isinstance(sent, bytes) else sent) == body
