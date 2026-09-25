#!/usr/bin/env python3
"""Tests for the session-worktree pruner: the removal decision, and what it does with it.

The readers -- the porcelain parser, the lock-liveness check, the cherry and merge-tree
verdicts -- live in the deployed `claude_worktree` module (#2133) and are tested in the
dotfiles repo beside it; `test_claude_worktree_import.py` covers the bootstrap. What is
tested here is what stays in this repo: `classify` takes the facts the git calls produce,
`is_merged` gates the readers on exit status, and `remove`/`prune_all`/`main` act.

Run: uv run pytest scripts/dev/tests/test_prune_worktrees.py
"""

import os
import subprocess
import sys
from pathlib import Path

from prune_worktrees import (
    KEEP,
    REMOVABLE,
    Worktree,
    _worktree_facts,
    classify,
    delete_branch,
    find_orphan_dirs,
    landed_orphan_branches,
    main,
    orphan_branches,
    prune_all,
    remove,
    sweep_branches,
)

PRUNER = Path(__file__).resolve().parents[1] / "prune_worktrees.py"


def test_orphan_dirs_are_those_git_does_not_track(tmp_path):
    tracked = tmp_path / "live"
    tracked.mkdir()
    stray = tmp_path / "left-behind"
    stray.mkdir()
    (tmp_path / "notes.txt").write_text("")

    assert find_orphan_dirs(str(tmp_path), {str(tracked)}) == [str(stray)]


def test_missing_worktrees_dir_is_not_an_error():
    assert find_orphan_dirs("/nonexistent/worktrees", set()) == []


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


# bug survived CI — every test above exercises classify()/is_merged()/session_is_alive()/
# parse_worktree_list, none of them the actual `git worktree remove` call.


def test_remove_unlocks_before_removing_a_locked_tree(monkeypatch):
    calls = []

    def fake_git(*args, **kwargs):
        argv = ["git", *args]
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr("prune_worktrees.git", fake_git)
    ok, err = remove("/repo", _tree(locked=True))
    assert ok is True and err == ""
    assert calls == [
        ["git", "worktree", "unlock", "/w"],
        ["git", "worktree", "remove", "/w"],
    ]


def test_remove_skips_unlock_for_a_never_locked_tree(monkeypatch):
    calls = []

    def fake_git(*args, **kwargs):
        argv = ["git", *args]
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr("prune_worktrees.git", fake_git)
    remove("/repo", _tree(locked=False))
    assert calls == [["git", "worktree", "remove", "/w"]]


def test_remove_never_passes_force(monkeypatch):
    calls = []

    def fake_git(*args, **kwargs):
        argv = ["git", *args]
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr("prune_worktrees.git", fake_git)
    remove("/repo", _tree(locked=True))
    # --force (or -f -f) is the escape hatch git's own docs suggest for a locked tree; using
    # it here would remove the safety net that makes auto-unlock acceptable — a REMOVABLE
    # tree is only guaranteed merged AND clean, not that git agrees it's safe to delete.
    assert not any("-f" in c or "--force" in c for c in calls)


def _git(repo: Path, *args: str) -> None:
    """Run git in `repo` with every inherited GIT_* variable removed.

    The identity goes in the environment rather than into `git config`, because a
    `git config user.email` call resolves GIT_DIR before it resolves `-C` — under a
    pre-commit hook that writes the test identity into the real repository, which is
    how t@t.com came to author 170 real commits (2026-08-17). Same pattern as
    scripts/deploy_tools/tests/test_deploy_staleness.py.
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["GIT_AUTHOR_NAME"] = env["GIT_COMMITTER_NAME"] = "t"
    env["GIT_AUTHOR_EMAIL"] = env["GIT_COMMITTER_EMAIL"] = "t@example.invalid"
    subprocess.run(["git", *args], cwd=repo, env=env, check=True)


def _init_scratch_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q", "--initial-branch=master")
    (path / "a.txt").write_text("one\n")
    _git(path, "add", "a.txt")
    _git(path, "commit", "-q", "-m", "init", "--no-gpg-sign")


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
    # of the live repo. The fixture's own calls go through _git, which scrubs for itself.
    for var in [name for name in os.environ if name.startswith("GIT_")]:
        monkeypatch.delenv(var, raising=False)

    repo = tmp_path / "repo"
    _init_scratch_repo(repo)
    wt = tmp_path / "wt"
    _git(repo, "worktree", "add", "-q", "-b", "feature", str(wt))
    _git(repo, "worktree", "lock", str(wt), "--reason", "test lock")

    ok, err = remove(
        str(repo), Worktree(path=str(wt), head="x", branch="feature", locked=True)
    )

    assert ok, err
    assert not wt.exists()


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


# pr_head_says_merged moved into claude_worktree with the lookup that calls it (dotfiles
# #629); its tests live in that package's tests/test_readers.py.


# --- the --brief / --prune dispatch (#1190) ---------------------------------------------
#
# These drive main() rather than brief(), because the bug was in the dispatch: `if
# args.brief: return brief()` returned before the prune block, so brief() itself was
# innocent and a test calling brief(prune=True) would have passed against the broken build.


def _main_recording_git(monkeypatch, tmp_path, argv, removable=2):
    """Run main(argv) over a fabricated survey, returning every argv `lib.git.git` saw.

    The checkout is `tmp_path`, a real directory: `prune_all` ends with
    `repair_object_store`, which runs git through lib.git's own binding (out of the stub's
    reach) and only needs a cwd that exists -- every git failure there is check=False.
    """
    calls = []
    repo = str(tmp_path)

    def fake_git(*args, **kwargs):
        argv = ["git", *args]
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    trees = [
        Worktree(
            path=f"{repo}/.claude/worktrees/w{i}",
            head="abc",
            branch=f"b{i}",
            locked=False,
        )
        for i in range(removable)
    ]
    monkeypatch.setattr("prune_worktrees.primary_checkout", lambda: repo)
    monkeypatch.setattr(
        "prune_worktrees.survey", lambda repo: [(REMOVABLE, t, "merged") for t in trees]
    )
    monkeypatch.setattr("prune_worktrees.find_orphan_dirs", lambda *a, **k: [])
    monkeypatch.setattr("prune_worktrees.parse_worktree_list", lambda porcelain: [])
    monkeypatch.setattr("prune_worktrees._git", lambda *a, **k: "")
    monkeypatch.setattr("prune_worktrees.git", fake_git)
    return main(argv), calls, trees


def test_prune_with_brief_removes_every_removable_worktree(monkeypatch, tmp_path):
    # the accepting half: --brief shortens the report, it does not cancel --prune
    rc, calls, trees = _main_recording_git(
        monkeypatch, tmp_path, ["--prune", "--brief"]
    )
    assert rc == 0
    removals = [c for c in calls if c[:3] == ["git", "worktree", "remove"]]
    assert [c[3] for c in removals] == [t.path for t in trees]


def test_brief_without_prune_removes_nothing(monkeypatch, tmp_path):
    # the rejecting half: --brief alone is the SessionStart banner and must stay read-only
    rc, calls, _ = _main_recording_git(monkeypatch, tmp_path, ["--brief"])
    assert rc == 0
    assert not any(c[:3] == ["git", "worktree", "remove"] for c in calls)


def test_prune_with_brief_does_not_tell_the_caller_to_re_run_prune(
    capsys, monkeypatch, tmp_path
):
    # the hint that made the no-op read as a report; it must not survive an actual prune
    _main_recording_git(monkeypatch, tmp_path, ["--prune", "--brief"])
    out = capsys.readouterr().out
    assert "--prune" not in out
    assert out.count(f"removed {tmp_path}/.claude/worktrees/") == 2


def test_prune_without_brief_still_removes_every_removable_worktree(
    monkeypatch, tmp_path
):
    # the long report is the path that always worked; it must keep working after the refactor
    _, calls, trees = _main_recording_git(monkeypatch, tmp_path, ["--prune"])
    removals = [c for c in calls if c[:3] == ["git", "worktree", "remove"]]
    assert [c[3] for c in removals] == [t.path for t in trees]


def test_prune_all_reports_a_removal_git_refused(capsys, monkeypatch, tmp_path):
    def fake_git(*args, **kwargs):
        return subprocess.CompletedProcess(
            ["git", *args], 1, stdout="", stderr="is dirty\n"
        )

    monkeypatch.setattr("prune_worktrees.git", fake_git)
    prune_all(str(tmp_path), [_tree()], advise=lambda root: [f"  blocked under {root}"])
    out = capsys.readouterr().out
    assert "could not remove /w: is dirty\n  blocked under /w\n" in out


def test_worktree_facts_ok_is_false_when_the_git_call_fails(monkeypatch):
    """`reap` refuses on `ok=False` rather than release the whole register.

    A failing `git worktree list` must read as "the read failed", not as "there are no
    worktrees" — the two produce the same empty stdout under `check=False`.
    """
    monkeypatch.setattr("prune_worktrees.primary_checkout", lambda: "/repo")
    monkeypatch.setattr(
        "prune_worktrees.git",
        lambda *a, **k: subprocess.CompletedProcess(a, 1, stdout="", stderr="fatal\n"),
    )
    trees, _dirty, _merged, ok = _worktree_facts()
    assert ok is False
    assert trees == []


def test_worktree_facts_ok_is_true_when_git_succeeds_with_no_worktrees(monkeypatch):
    """The other half of the pair above: a real empty list still reads `ok=True`."""
    monkeypatch.setattr("prune_worktrees.primary_checkout", lambda: "/repo")
    monkeypatch.setattr(
        "prune_worktrees.git",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout="", stderr=""),
    )
    trees, _dirty, _merged, ok = _worktree_facts()
    assert ok is True
    assert trees == []


# --- the orphan-branch sweep (#2430) ----------------------------------------------------
#
# Real git throughout, for the reason the on-disk removal test above gives: what is claimed
# is that git ACCEPTS the sequence. `git branch -d` refusing a rebase-landed branch is the
# whole reason the dotfiles hook swept so few, and a mock would happily accept it.


def _scrub_git_env(monkeypatch) -> None:
    """Drop every inherited GIT_* variable, as the on-disk removal test does and why."""
    for var in [name for name in os.environ if name.startswith("GIT_")]:
        monkeypatch.delenv(var, raising=False)


def _branch_names(repo: Path) -> list[str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    out = subprocess.run(
        ["git", "branch", "--format=%(refname:short)"],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout.split()


def _repo_with_branches(tmp_path: Path) -> Path:
    """A scratch repo carrying one branch of each class the sweep has to tell apart."""
    repo = tmp_path / "repo"
    _init_scratch_repo(repo)
    _git(
        repo, "branch", "worktree-ancestry"
    )  # tip IS master: `git branch -d` accepts it
    _git(repo, "branch", "feature-landed")  # merged, but not a session branch
    _git(repo, "checkout", "-q", "-b", "worktree-open")
    (repo / "b.txt").write_text("two\n")
    _git(repo, "add", "b.txt")
    _git(repo, "commit", "-q", "-m", "open work", "--no-gpg-sign")
    _git(repo, "checkout", "-q", "master")
    _git(repo, "worktree", "add", "-q", "-b", "worktree-live", str(tmp_path / "live"))
    _git(repo, "update-ref", "refs/remotes/origin/master", "master")
    return repo


def test_the_sweep_sees_only_session_branches_no_worktree_holds(tmp_path, monkeypatch):
    _scrub_git_env(monkeypatch)
    repo = _repo_with_branches(tmp_path)

    # feature-landed is excluded by the prefix though it is merged; worktree-live is excluded
    # because a worktree holds it; master is excluded on both counts.
    assert sorted(orphan_branches(str(repo))) == ["worktree-ancestry", "worktree-open"]


def test_an_unmerged_session_branch_is_never_swept(tmp_path, monkeypatch):
    # The RED half. worktree-open carries a commit master does not have, so no local layer
    # settles it and it must survive a sweep that deletes its neighbour.
    _scrub_git_env(monkeypatch)
    repo = _repo_with_branches(tmp_path)

    landed = landed_orphan_branches(str(repo), deep=True)

    assert landed == ["worktree-ancestry"]
    sweep_branches(str(repo), landed)
    assert "worktree-open" in _branch_names(repo)
    assert "worktree-ancestry" not in _branch_names(repo)


def test_a_rebase_landed_branch_is_deleted_though_git_branch_d_refuses_it(
    tmp_path, monkeypatch
):
    # The case the dotfiles hook could not sweep: the commit's content is on master under a
    # different sha, so the tip is not an ancestor and `-d` says "not fully merged".
    _scrub_git_env(monkeypatch)
    repo = _repo_with_branches(tmp_path)
    _git(repo, "checkout", "-q", "-b", "worktree-rebased")
    (repo / "c.txt").write_text("three\n")
    _git(repo, "add", "c.txt")
    _git(repo, "commit", "-q", "-m", "rebased work", "--no-gpg-sign")
    _git(repo, "checkout", "-q", "master")
    # -x forces a new SHA: a same-second cherry-pick otherwise reproduces the original's.
    _git(repo, "cherry-pick", "-x", "worktree-rebased")
    _git(repo, "update-ref", "refs/remotes/origin/master", "master")

    assert "worktree-rebased" in landed_orphan_branches(str(repo), deep=True)
    ok, err = delete_branch(str(repo), "worktree-rebased")

    assert ok, err
    assert "worktree-rebased" not in _branch_names(repo)


def test_the_shallow_sweep_stops_at_the_bulk_ancestry_layer(tmp_path, monkeypatch):
    # The banner's budget is the constraint (`session-health.py` kills it at 5s), so deep=False
    # settles what one `git branch --merged` settles and nothing more. Asserted by what it
    # MISSES: a rebase-landed branch is exactly the case only the per-branch layers reach, so
    # deep=False returning it would mean it paid for them.
    _scrub_git_env(monkeypatch)
    repo = _repo_with_branches(tmp_path)
    _git(repo, "checkout", "-q", "-b", "worktree-rebased")
    (repo / "c.txt").write_text("three\n")
    _git(repo, "add", "c.txt")
    _git(repo, "commit", "-q", "-m", "rebased work", "--no-gpg-sign")
    _git(repo, "checkout", "-q", "master")
    # -x forces a new SHA: a same-second cherry-pick otherwise reproduces the original's.
    _git(repo, "cherry-pick", "-x", "worktree-rebased")
    _git(repo, "update-ref", "refs/remotes/origin/master", "master")

    assert landed_orphan_branches(str(repo), deep=False) == ["worktree-ancestry"]
    assert "worktree-rebased" in landed_orphan_branches(str(repo), deep=True)


def test_one_prune_removes_a_worktree_and_deletes_the_branch_it_freed(
    tmp_path, monkeypatch
):
    # One pass converges. A branch is only orphan once its worktree is gone, so a list read
    # before the removals names none of the branches those removals free — and the sweep
    # would leave its own leavings for the next run, forever.
    #
    # A subprocess, so main() resolves the checkout itself from cwd: the alternative is
    # patching primary_checkout, and the monkeypatch ratchet in ansible/tests/repo/ only ever
    # falls. CLAUDE_WORKTREE_HOME is inherited, which is how the stand-in reaches it in CI.
    _scrub_git_env(monkeypatch)
    repo = tmp_path / "repo"
    _init_scratch_repo(repo)
    _git(repo, "worktree", "add", "-q", "-b", "worktree-done", str(tmp_path / "done"))
    _git(repo, "update-ref", "refs/remotes/origin/master", "master")

    run = subprocess.run(
        [sys.executable, str(PRUNER), "--prune"],
        cwd=repo,
        env={k: v for k, v in os.environ.items() if not k.startswith("GIT_")},
        capture_output=True,
        text=True,
    )

    assert run.returncode == 0, run.stderr
    assert not (tmp_path / "done").exists()
    assert "worktree-done" not in _branch_names(repo)
