"""Tests for the forge layer of `lib.worktrees.is_merged`: a tip inside a merged PR's head.

`land.sh` merges master into a batch branch before it merges the PR, so the merged head is the
local tip plus a merge commit (#6365). `place clean` and the weekly sweep both judge by
`is_merged`, so both see such a tree as landed (#8529).

Run: uv run pytest scripts/lib/tests/test_worktrees_forge_landed.py
"""

from types import SimpleNamespace

from lib.git_testing import commit, git, init_repo
from lib.proc_testing import fake_bin, path_with
from lib.worktrees import contained_in_merged_pr, is_merged

TIP = "a" * 40
MERGED_HEAD = "f" * 40


def _contained(merged_heads: str, compare: str) -> bool:
    """`contained_in_merged_pr` with a gh whose `pr list` prints `merged_heads` and `api` `compare`."""

    def fake_gh(*args, **_kwargs):
        out = compare if args[0] == "api" else merged_heads
        return SimpleNamespace(returncode=0, stdout=out)

    return contained_in_merged_pr("/repo", TIP, "worktree-fanout-b", fake_gh)


def test_a_tip_inside_the_merged_pr_head_has_landed():
    assert _contained(MERGED_HEAD + "\n", "ahead\n")


def test_a_diverged_tip_has_not_landed():
    """Local commits the merged head lacks are work nothing proves landed."""
    assert not _contained(MERGED_HEAD + "\n", "diverged\n")


def test_a_branch_with_no_merged_pr_has_not_landed():
    assert not _contained("", "ahead\n")


def test_is_merged_counts_a_branch_land_sh_extended_before_the_merge(
    tmp_path, monkeypatch
):
    """The sweep's own call: no local layer settles the branch, and the forge's exact match fails."""
    repo = init_repo(tmp_path / "repo")
    commit(repo, "init", **{"a.txt": "one\n"})
    git(repo, "checkout", "-q", "-b", "worktree-fanout-b")
    head = commit(repo, "batch work", **{"b.txt": "two\n"})
    git(repo, "checkout", "-q", "master")
    stubs = fake_bin(
        tmp_path / "bin",
        gh=f'case "$1" in\n  pr) echo {MERGED_HEAD} ;;\n  api) echo ahead ;;\nesac\n',
    )
    monkeypatch.setenv("PATH", path_with(stubs))
    monkeypatch.delenv("GH_BIN", raising=False)
    assert is_merged(str(repo), head, "worktree-fanout-b", base="master")
