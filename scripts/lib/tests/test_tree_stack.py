"""`stack` gives the tree a squash merge of each head makes, with no checkout.

Run: uv run pytest scripts/lib/tests/test_tree_stack.py
"""

import subprocess

import pytest

from lib import tree_stack
from lib.git_testing import commit, git, git_out, init_repo


def _branch(repo, base: str, name: str, **files: str) -> str:
    """A branch off `base` with one commit changing `files`; its head."""
    git(repo, "checkout", "-q", "-b", name, base)
    return commit(repo, name, **files)


def _repo(tmp_path):
    repo = init_repo(tmp_path / "repo")
    base = commit(repo, "base", **{"a.txt": "a\n", "b.txt": "b\n"})
    return repo, base


def test_each_stacked_tree_is_the_squash_of_that_head_onto_the_ones_before(tmp_path):
    repo, base = _repo(tmp_path)
    one = _branch(repo, base, "one", **{"a.txt": "one\n"})
    two = _branch(repo, base, "two", **{"b.txt": "two\n"})

    stacked = tree_stack.stack(repo, base, [one, two], ["One (#1)", "Two (#2)"])

    # The oracle is git's own squash merge of each head onto the branch, in turn.
    git(repo, "checkout", "-q", "-b", "squashed", base)
    squashed = []
    for head in (one, two):
        git(repo, "merge", "--squash", "-q", head)
        git(repo, "commit", "-q", "-m", "squash", "--no-gpg-sign")
        squashed.append(git_out(repo, "rev-parse", "HEAD^{tree}"))
    assert [tree for _, tree in stacked] == squashed
    # One commit a head, each with the one before it as its only parent.
    tip = stacked[-1][0]
    assert git_out(repo, "log", "--format=%s %p", f"{base}..{tip}").splitlines() == [
        f"Two (#2) {stacked[0][0][:7]}",
        f"One (#1) {base[:7]}",
    ]


def test_a_head_that_conflicts_with_one_before_it_is_named_by_its_place(tmp_path):
    repo, base = _repo(tmp_path)
    one = _branch(repo, base, "one", **{"a.txt": "one\n"})
    two = _branch(repo, base, "two", **{"b.txt": "two\n"})
    other = _branch(repo, base, "other", **{"a.txt": "other\n"})

    assert len(tree_stack.stack(repo, base, [one, two])) == 2
    with pytest.raises(tree_stack.Conflict) as conflict:
        tree_stack.stack(repo, base, [one, two, other])
    assert conflict.value.index == 2


def test_a_commit_git_cannot_read_is_an_error_and_not_a_conflict(tmp_path):
    repo, base = _repo(tmp_path)
    with pytest.raises(subprocess.CalledProcessError):
        tree_stack.stack(repo, base, ["f" * 40])
