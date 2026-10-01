"""`clean_one` against real git — a scratch repo and a real worktree, no `dirty` override.

Its siblings in test_fanout_clean.py drive `clean_one` through its seams, which cannot see what
`is_dirty` really raises on a tree git refuses to read. These build the tree and let the real
`prune_worktrees.is_dirty` and `remove` run against it.

Run: uv run pytest scripts/dev/tests/test_fanout_clean_real_git.py
"""

import shutil
import subprocess
from pathlib import Path

import pytest

from fanout_lib.clean import clean_one
from lib.git_testing import git, init_repo, scrub_process_git_env
from prune_worktrees import Worktree, parse_worktree_list, remove


def _init_scratch_repo(path: Path) -> None:
    init_repo(path, initial_commit="init")


def _worktree_list(repo: Path) -> str:
    return git(repo, "worktree", "list", "--porcelain").stdout


def _branches(repo: Path) -> str:
    return git(repo, "branch", "--list").stdout


def _scrub_git_env(monkeypatch) -> None:
    """Strip every inherited `GIT_*` var for the rest of this test's process.

    `remove()` and `clean_one`'s own `git branch -D` call take no environment argument, so
    this is the only way to keep their subprocess calls off the real repository: under
    `prek`'s own `pytest` hook, `GIT_DIR` is set in the environment, and `-C <scratch repo>`
    does not override an inherited `GIT_DIR`.
    """
    scrub_process_git_env(monkeypatch)


def test_a_missing_worktree_directory_is_deregistered_and_its_merged_branch_dropped(
    tmp_path, monkeypatch
):
    """A worktree `rm -rf`'d by hand rather than through the normal
    removal path stays registered (`prunable`, per git's own porcelain label). `clean_one`
    must deregister it instead of crashing in the real `is_dirty` on a cwd that no longer
    exists — this passes no `dirty` override, so the real default proves that.
    """
    _scrub_git_env(monkeypatch)
    repo = tmp_path / "repo"
    _init_scratch_repo(repo)
    wt = tmp_path / "wt"
    git(repo, "worktree", "add", "-q", "-b", "gone-branch", str(wt))
    shutil.rmtree(wt)
    porcelain = _worktree_list(repo)
    assert "prunable" in porcelain  # the scenario this test exists to cover
    tree = next(t for t in parse_worktree_list(porcelain) if t.path == str(wt))

    state, why = clean_one(str(repo), tree, ask=lambda *a, **k: True, remover=remove)

    assert state == "removed" and "already gone" in why
    assert str(wt) not in _worktree_list(repo)
    assert "gone-branch" not in _branches(repo)


def test_a_present_merged_worktree_is_removed_and_its_branch_dropped(
    tmp_path, monkeypatch
):
    """Removing a landed tree must not leave its `worktree-fanout-<batch>` branch behind
    for the operator. Real git throughout, and no `dirty` override, so the tree really is
    present and clean.
    """
    _scrub_git_env(monkeypatch)
    repo = tmp_path / "repo"
    _init_scratch_repo(repo)
    wt = tmp_path / "wt"
    git(repo, "worktree", "add", "-q", "-b", "done-branch", str(wt))
    tree = next(
        t for t in parse_worktree_list(_worktree_list(repo)) if t.path == str(wt)
    )

    state, why = clean_one(str(repo), tree, ask=lambda *a, **k: True, remover=remove)

    assert (state, why) == ("removed", "")
    assert not wt.exists()
    assert "done-branch" not in _branches(repo)


def test_a_missing_worktree_directory_keeps_its_unmerged_branch(tmp_path, monkeypatch):
    _scrub_git_env(monkeypatch)
    repo = tmp_path / "repo"
    _init_scratch_repo(repo)
    wt = tmp_path / "wt"
    git(repo, "worktree", "add", "-q", "-b", "gone-branch", str(wt))
    shutil.rmtree(wt)
    tree = next(
        t for t in parse_worktree_list(_worktree_list(repo)) if t.path == str(wt)
    )

    state, why = clean_one(str(repo), tree, ask=lambda *a, **k: False, remover=remove)

    assert state == "removed" and "already gone" in why
    assert "gone-branch" in _branches(repo)


def test_a_failed_branch_delete_on_a_missing_tree_is_reported_as_kept(
    tmp_path, monkeypatch
):
    """Ruling 23: `removed: … (already gone)` must not be printed when the branch stays.

    Real git refuses the delete here — the registered tree is renamed onto a branch that
    does not exist — so this exercises the same `git branch -D` the code runs, not a stub.
    """
    _scrub_git_env(monkeypatch)
    repo = tmp_path / "repo"
    _init_scratch_repo(repo)
    wt = tmp_path / "wt"
    git(repo, "worktree", "add", "-q", "-b", "gone-branch", str(wt))
    shutil.rmtree(wt)
    registered = next(
        t for t in parse_worktree_list(_worktree_list(repo)) if t.path == str(wt)
    )
    tree = Worktree(
        path=registered.path,
        head=registered.head,
        branch="no-such-branch",
        locked=registered.locked,
        lock_reason=registered.lock_reason,
    )

    state, why = clean_one(str(repo), tree, ask=lambda *a, **k: True, remover=remove)

    assert state == "kept"
    assert "branch no-such-branch not deleted" in why and "not found" in why
    # The registration really is gone; it is only the branch claim that was wrong.
    assert str(wt) not in _worktree_list(repo)


def test_a_worktree_whose_submodule_gitdir_dangles_is_kept_and_names_the_git_error(
    tmp_path, monkeypatch
):
    """`git status` fatals in a worktree whose submodule `.git` file points nowhere.

    `is_dirty` raises `CalledProcessError` there, and the traceback reached the operator as
    a truncated `clean failed (exit 1)` line saying nothing about the cause. Real git and
    the real `is_dirty` throughout — a `dirty` fake raising on cue would still pass if
    `git_dirty` were later given `check=False`, which is the dangerous change.
    """
    _scrub_git_env(monkeypatch)
    upstream = tmp_path / "sub-upstream"
    _init_scratch_repo(upstream)
    repo = tmp_path / "repo"
    _init_scratch_repo(repo)
    wt = tmp_path / "wt"
    git(repo, "worktree", "add", "-q", "-b", "broken-sub", str(wt))
    git(
        wt,
        "-c",
        "protocol.file.allow=always",
        "submodule",
        "add",
        "-q",
        str(upstream),
        "sub",
    )
    git(wt, "commit", "-q", "-m", "add sub", "--no-gpg-sign")
    (wt / "sub" / ".git").write_text("gitdir: ../../nowhere/modules/sub\n")
    # The scenario this test exists to cover: git itself cannot read the tree.
    with pytest.raises(subprocess.CalledProcessError):
        git(wt, "status", "--porcelain")
    tree = next(
        t for t in parse_worktree_list(_worktree_list(repo)) if t.path == str(wt)
    )

    state, why = clean_one(str(repo), tree, ask=lambda *a, **k: True, remover=remove)

    assert state == "kept"
    assert "git status failed" in why and "not a git repository" in why
    # Unreadable is not clean: the tree and its branch both survive.
    assert wt.exists()
    assert "broken-sub" in _branches(repo)
