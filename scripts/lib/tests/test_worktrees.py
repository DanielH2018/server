"""Tests for `lib.worktrees`: the removal decision and the git calls that act on it.

The readers -- the porcelain parser, the lock-liveness check, the cherry and merge-tree
verdicts -- live in the deployed `claude_worktree` package and are tested in the dotfiles
repo beside it; `test_claude_worktree_import.py` covers the bootstrap. What is tested here is
the policy this repo keeps: `classify` takes the facts the git calls produce, `remove` acts on
the verdict, and `worktree_facts` separates a failed read from an empty one. The CLI that
drives them is `scripts/dev/tests/test_prune_worktrees.py`'s.

Run: uv run pytest scripts/lib/tests/test_worktrees.py
"""

import os
import shutil
import subprocess
from contextlib import contextmanager
from pathlib import Path

from lib.git_testing import commit, git, init_repo, scrub_process_git_env
from lib.worktrees import (
    KEEP,
    REMOVABLE,
    Worktree,
    classify,
    primary_checkout,
    remove,
    worktree_facts,
)


LIVE_LOCK = "claude session mine (pid {pid} start {start})"


def _live_lock_reason():
    """A lock reason naming this process, which is by definition still running."""
    pid = os.getpid()
    with open(f"/proc/{pid}/stat") as handle:
        start = handle.read().rpartition(")")[2].split()[19]
    return LIVE_LOCK.format(pid=pid, start=start)


def _tree(branch="worktree-x", locked=False):
    return Worktree(path="/w", head="abc", branch=branch, locked=locked)


def test_merged_clean_unlocked_is_removable():
    verdict, _ = classify(_tree(), merged=True, dirty=False)
    assert verdict == REMOVABLE


def test_a_live_lock_wins_over_a_merged_branch():
    # a session may still be working past its own merge; the lock is how it says so
    tree = _tree(locked=True)
    tree.lock_reason = _live_lock_reason()
    verdict, reason = classify(tree, merged=True, dirty=False)
    assert verdict == KEEP
    assert "in use" in reason


def test_a_stale_lock_does_not_keep_a_merged_worktree():
    # Claude Code never releases the lock when a session ends, so obeying every lock
    # would keep every abandoned worktree forever
    tree = _tree(locked=True)
    tree.lock_reason = "claude session gone (pid 1 start 999999999)"
    verdict, _ = classify(tree, merged=True, dirty=False)
    assert verdict == REMOVABLE


def test_a_stale_lock_still_does_not_override_uncommitted_work():
    tree = _tree(locked=True)
    tree.lock_reason = "claude session gone (pid 1 start 999999999)"
    verdict, reason = classify(tree, merged=True, dirty=True)
    assert verdict == KEEP
    assert "uncommitted" in reason


def test_uncommitted_changes_are_never_removed():
    verdict, reason = classify(_tree(), merged=True, dirty=True)
    assert verdict == KEEP
    assert "uncommitted" in reason


def test_unmerged_branch_is_kept():
    verdict, reason = classify(_tree(), merged=False, dirty=False)
    assert verdict == KEEP
    assert "not merged" in reason


def test_detached_head_is_kept_rather_than_guessed_about():
    verdict, reason = classify(_tree(branch=None), merged=True, dirty=False)
    assert verdict == KEEP
    assert "detached" in reason


def _init_scratch_repo(path: Path) -> None:
    init_repo(path)
    commit(path, "init", **{"a.txt": "one\n"})


def test_remove_actually_deletes_a_locked_worktree_on_disk(tmp_path, monkeypatch):
    # Real git calls rather than mocks: the mocked tests above only prove call order, not
    # that git accepts the sequence. Without the unlock, `git worktree remove` on a locked
    # tree fails outright ("cannot remove a locked working tree") and the caller would
    # report success while removing nothing — this is the bug itself, reproduced.
    #
    # Scrub GIT_* for remove()'s own git calls: it takes no environment argument, so this
    # is the only way to keep them off the real repository. git exports GIT_DIR and
    # GIT_INDEX_FILE into hook processes and `git -C <path>` does NOT override them, so
    # under the prek pre-commit hook an unscrubbed remove() unlocks and deletes worktrees
    # of the live repo. The fixture's own calls go through lib.git_testing, which scrubs
    # for itself.
    scrub_process_git_env(monkeypatch)

    repo = tmp_path / "repo"
    _init_scratch_repo(repo)
    wt = tmp_path / "wt"
    git(repo, "worktree", "add", "-q", "-b", "feature", str(wt))
    git(repo, "worktree", "lock", str(wt), "--reason", "test lock")

    ok, err = remove(
        str(repo), Worktree(path=str(wt), head="x", branch="feature", locked=True)
    )

    assert ok, err
    assert not wt.exists()


def test_remove_deletes_a_never_locked_worktree_on_disk(tmp_path, monkeypatch):
    # The unlocked half of the pair above: no lock to release, and the tree still goes.
    scrub_process_git_env(monkeypatch)
    repo = tmp_path / "repo"
    _init_scratch_repo(repo)
    wt = tmp_path / "wt"
    git(repo, "worktree", "add", "-q", "-b", "feature", str(wt))

    ok, err = remove(
        str(repo), Worktree(path=str(wt), head="x", branch="feature", locked=False)
    )

    assert ok, err
    assert not wt.exists()


def test_remove_never_forces_past_an_untracked_file(tmp_path, monkeypatch):
    # --force is the escape hatch git's own docs suggest for a locked tree; using it would
    # remove the safety net that makes auto-unlock acceptable. A REMOVABLE tree is only
    # guaranteed merged AND clean as of the read, so git's own refusal on a file that
    # appeared since is the backstop -- and this is the one input where it must fire.
    scrub_process_git_env(monkeypatch)
    repo = tmp_path / "repo"
    _init_scratch_repo(repo)
    wt = tmp_path / "wt"
    git(repo, "worktree", "add", "-q", "-b", "feature", str(wt))
    git(repo, "worktree", "lock", str(wt), "--reason", "test lock")
    (wt / "scratch.txt").write_text("unlanded\n")

    ok, err = remove(
        str(repo), Worktree(path=str(wt), head="x", branch="feature", locked=True)
    )

    assert not ok
    assert "untracked" in err or "modified" in err, err
    assert (wt / "scratch.txt").exists()


@contextmanager
def _sleeper(cwd: Path, project_dir: Path | None = None):
    """A live process with `cwd`, and `CLAUDE_PROJECT_DIR` when given, killed on exit."""
    env = dict(os.environ)
    env.pop("CLAUDE_PROJECT_DIR", None)
    if project_dir is not None:
        env["CLAUDE_PROJECT_DIR"] = str(project_dir)
    proc = subprocess.Popen(["sleep", "60"], cwd=cwd, env=env)
    try:
        yield proc
    finally:
        proc.kill()
        proc.wait()


def _locked_paths(repo: Path) -> set[str]:
    trees = git(repo, "worktree", "list", "--porcelain").stdout.split("\n\n")
    return {
        t.splitlines()[0].removeprefix("worktree ") for t in trees if "\nlocked" in t
    }


def test_remove_refuses_a_tree_a_live_process_has_as_cwd_is_flagged(
    tmp_path, monkeypatch
):
    # #3910: a session deleted out from under itself loses every repo hook. The lock here
    # names no live owner, so classify() would call the tree REMOVABLE; the process is what
    # still uses it.
    scrub_process_git_env(monkeypatch)
    repo = tmp_path / "repo"
    _init_scratch_repo(repo)
    wt = tmp_path / "wt"
    git(repo, "worktree", "add", "-q", "-b", "feature", str(wt))
    git(repo, "worktree", "lock", str(wt), "--reason", "test lock")

    with _sleeper(cwd=wt) as proc:
        ok, err = remove(
            str(repo), Worktree(path=str(wt), head="x", branch="feature", locked=True)
        )

    assert not ok
    assert f"pid {proc.pid}" in err, err
    assert wt.exists()
    # The refusal comes before the unlock, so the prune path leaves the lock as it was.
    assert str(wt) in _locked_paths(repo)


def test_remove_refuses_a_tree_a_live_session_names_as_project_dir_is_flagged(
    tmp_path, monkeypatch
):
    scrub_process_git_env(monkeypatch)
    repo = tmp_path / "repo"
    _init_scratch_repo(repo)
    wt = tmp_path / "wt"
    git(repo, "worktree", "add", "-q", "-b", "feature", str(wt))

    with _sleeper(cwd=tmp_path, project_dir=wt) as proc:
        ok, err = remove(
            str(repo), Worktree(path=str(wt), head="x", branch="feature", locked=False)
        )

    assert not ok
    assert f"pid {proc.pid}" in err and "CLAUDE_PROJECT_DIR" in err, err
    assert wt.exists()


def test_a_process_in_a_sibling_tree_does_not_block_removal_is_clean(
    tmp_path, monkeypatch
):
    # `wt2` shares `wt`'s string prefix, so a startswith match would refuse here. The
    # sleeper's project dir is the repo, the parent of every tree, as the primary
    # checkout's session's is.
    scrub_process_git_env(monkeypatch)
    repo = tmp_path / "repo"
    _init_scratch_repo(repo)
    wt, wt2 = tmp_path / "wt", tmp_path / "wt2"
    git(repo, "worktree", "add", "-q", "-b", "feature", str(wt))
    git(repo, "worktree", "add", "-q", "-b", "other", str(wt2))

    with _sleeper(cwd=wt2, project_dir=repo):
        ok, err = remove(
            str(repo), Worktree(path=str(wt), head="x", branch="feature", locked=False)
        )

    assert ok, err
    assert not wt.exists()


def test_remove_still_deregisters_a_deleted_tree_a_session_names_is_clean(
    tmp_path, monkeypatch
):
    # #3887's state: the directory is already gone and a live session still points at it.
    # Deregistering cannot hurt that session further, and fan-out's `_clean_missing_tree`
    # reaches this through the same remover.
    scrub_process_git_env(monkeypatch)
    repo = tmp_path / "repo"
    _init_scratch_repo(repo)
    wt = tmp_path / "wt"
    git(repo, "worktree", "add", "-q", "-b", "feature", str(wt))
    shutil.rmtree(wt)

    with _sleeper(cwd=tmp_path, project_dir=wt):
        ok, err = remove(
            str(repo), Worktree(path=str(wt), head="x", branch="feature", locked=False)
        )

    assert ok, err
    assert str(wt) not in git(repo, "worktree", "list", "--porcelain").stdout


def test_removable_reason_names_the_dead_lock_owner_not_unlocked():
    # "unlocked" would be misleading here: at classify() time the tree is still locked —
    # only remove() unlocks it later. The reason string must say what's actually true.
    tree = _tree(locked=True)
    tree.lock_reason = "claude session gone (pid 1 start 999999999)"
    verdict, reason = classify(tree, merged=True, dirty=False)
    assert verdict == REMOVABLE
    assert "lock owner is dead" in reason


def test_removable_reason_for_a_never_locked_tree_says_unlocked():
    verdict, reason = classify(_tree(locked=False), merged=True, dirty=False)
    assert verdict == REMOVABLE
    assert reason.endswith("unlocked")


def test_worktree_facts_ok_is_false_when_the_git_call_fails(monkeypatch):
    """`reap` refuses on `ok=False` rather than release the whole register.

    A failing `git worktree list` must read as "the read failed", not as "there are no
    worktrees" — the two produce the same empty stdout under `check=False`.
    """
    monkeypatch.setattr("lib.worktrees.primary_checkout", lambda: "/repo")
    monkeypatch.setattr(
        "lib.worktrees.git",
        lambda *a, **k: subprocess.CompletedProcess(a, 1, stdout="", stderr="fatal\n"),
    )
    trees, _dirty, _merged, ok = worktree_facts()
    assert ok is False
    assert trees == []


def test_worktree_facts_ok_is_true_when_git_succeeds_with_no_worktrees(monkeypatch):
    """The other half of the pair above: a real empty list still reads `ok=True`."""
    monkeypatch.setattr("lib.worktrees.primary_checkout", lambda: "/repo")
    monkeypatch.setattr(
        "lib.worktrees.git",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout="", stderr=""),
    )
    trees, _dirty, _merged, ok = worktree_facts()
    assert ok is True
    assert trees == []


def test_primary_checkout_from_a_linked_worktree_is_the_primary_is_clean(tmp_path):
    repo = tmp_path / "repo"
    _init_scratch_repo(repo)
    wt = tmp_path / "wt"
    git(repo, "worktree", "add", "-q", "-b", "feature", str(wt))
    assert primary_checkout(str(wt)) == str(repo.resolve())


def test_primary_checkout_of_a_separated_git_dir_is_flagged_as_none(tmp_path):
    # The common dir's parent is the directory holding the git dir, which is not a checkout:
    # the orphan scan would read `<that>/.claude/worktrees/` and `worktree_facts` would list
    # worktrees from a directory git does not consider a repo.
    checkout = tmp_path / "checkout"
    git(
        tmp_path,
        "init",
        "-q",
        f"--separate-git-dir={tmp_path / 'store'}",
        str(checkout),
    )
    assert primary_checkout(str(checkout)) is None
