#!/usr/bin/env python3
"""Tests for the session-worktree pruner CLI: the orphan scan, the branch sweep and dispatch.

The removal decision and the git calls that act on it are `lib.worktrees`, tested in
`scripts/lib/tests/test_worktrees.py`. What is tested here is what the CLI adds over them:
the orphan-directory and orphan-branch scans, and `prune_all`/`main`, which act.

Run: uv run pytest scripts/dev/tests/test_prune_worktrees.py
"""

import shutil
import sys
from pathlib import Path

from lib.git_testing import (
    commit,
    git,
    git_out,
    init_repo,
    scrub_process_git_env,
    scrubbed_env,
)
from lib.proc_testing import run
from lib.worktrees import REMOVABLE, Worktree
from prune_worktrees import (
    delete_branch,
    find_orphan_dirs,
    landed_orphan_branches,
    main,
    orphan_branches,
    prune_all,
    reap_claims,
    sweep_branches,
)

PRUNER = Path(__file__).resolve().parents[1] / "prune_worktrees.py"


def _tree(branch="worktree-x", locked=False):
    return Worktree(path="/w", head="abc", branch=branch, locked=locked)


def _init_scratch_repo(path: Path) -> None:
    init_repo(path)
    commit(path, "init", **{"a.txt": "one\n"})


def test_orphan_dirs_are_those_git_does_not_track(tmp_path):
    tracked = tmp_path / "live"
    tracked.mkdir()
    stray = tmp_path / "left-behind"
    stray.mkdir()
    (tmp_path / "notes.txt").write_text("")

    assert find_orphan_dirs(str(tmp_path), {str(tracked)}) == [str(stray)]


def test_missing_worktrees_dir_is_not_an_error():
    assert find_orphan_dirs("/nonexistent/worktrees", set()) == []


# --- the --brief / --prune dispatch ---------------------------------------------
#
# These drive main() rather than brief(), because the bug was in the dispatch: `if
# args.brief: return brief()` returned before the prune block, so brief() itself was
# innocent and a test calling brief(prune=True) would have passed against the broken build.


def _main_recording_removals(monkeypatch, tmp_path, argv, removable=2):
    """Run main(argv) over a fabricated survey, returning every path it asked to remove.

    `remove` itself is `lib.worktrees`' and is tested against real git there; what is under
    test here is whether the dispatch reaches it. The checkout is `tmp_path`, a real
    directory: `prune_all` ends with `repair_object_store`, and the branch sweep's bulk
    ancestry read runs real git too. Both only need a cwd that exists, and every git failure
    there is check=False.
    """
    removed = []
    repo = str(tmp_path)

    def fake_remove(repo, tree):
        removed.append(tree.path)
        return True, ""

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
    monkeypatch.setattr("prune_worktrees.git_stdout", lambda *a, **k: "")
    monkeypatch.setattr("prune_worktrees.remove", fake_remove)
    return main(argv), removed, trees


def test_prune_with_brief_removes_every_removable_worktree(monkeypatch, tmp_path):
    # the accepting half: --brief shortens the report, it does not cancel --prune
    rc, removed, trees = _main_recording_removals(
        monkeypatch, tmp_path, ["--prune", "--brief"]
    )
    assert rc == 0
    assert removed == [t.path for t in trees]


def test_brief_without_prune_removes_nothing(monkeypatch, tmp_path):
    # the rejecting half: --brief alone is the SessionStart banner and must stay read-only
    rc, removed, _ = _main_recording_removals(monkeypatch, tmp_path, ["--brief"])
    assert rc == 0
    assert removed == []


def test_prune_with_brief_does_not_tell_the_caller_to_re_run_prune(
    capsys, monkeypatch, tmp_path
):
    # the hint that made the no-op read as a report; it must not survive an actual prune
    _main_recording_removals(monkeypatch, tmp_path, ["--prune", "--brief"])
    out = capsys.readouterr().out
    assert "--prune" not in out
    assert out.count(f"removed {tmp_path}/.claude/worktrees/") == 2


def test_prune_without_brief_still_removes_every_removable_worktree(
    monkeypatch, tmp_path
):
    # the long report is the path that always worked; it must keep working after the refactor
    _, removed, trees = _main_recording_removals(monkeypatch, tmp_path, ["--prune"])
    assert removed == [t.path for t in trees]


def test_prune_all_reports_a_removal_git_refused(capsys, tmp_path):
    prune_all(
        str(tmp_path),
        [_tree()],
        advise=lambda root: [f"  blocked under {root}"],
        remover=lambda repo, tree: (False, "is dirty"),
    )
    out = capsys.readouterr().out
    assert "could not remove /w: is dirty\n  blocked under /w\n" in out


def _reaps_after(tmp_path, removed_ok):
    reaped = []
    prune_all(
        str(tmp_path),
        [_tree()],
        advise=lambda root: [],
        remover=lambda repo, tree: (removed_ok, "" if removed_ok else "is dirty"),
        reaper=lambda repo: reaped.append(repo) or ["released #1 (worktree-x)"],
    )
    return reaped


def test_prune_all_reaps_claims_after_a_removal(capsys, tmp_path):
    # A removed tree's claims stay on the register until `reap` runs (#3928).
    assert _reaps_after(tmp_path, removed_ok=True) == [str(tmp_path)]
    assert "released #1 (worktree-x)" in capsys.readouterr().out


def test_prune_all_does_not_reap_when_nothing_was_removed(tmp_path):
    assert _reaps_after(tmp_path, removed_ok=False) == []


def test_the_default_reaper_skips_a_checkout_with_no_findings_script(tmp_path):
    # The main-path tests run in scratch checkouts, so this is also what keeps them off gh.
    [line] = reap_claims(str(tmp_path))
    assert line.startswith("claims not reaped: no ")


# --- the orphan-branch sweep ----------------------------------------------------
#
# Real git throughout, for the reason `lib.worktrees`' on-disk removal tests give: what is
# claimed is that git ACCEPTS the sequence. `git branch -d` refusing a rebase-landed branch is the
# whole reason the dotfiles hook swept so few, and a mock would happily accept it.


def _branch_names(repo: Path) -> list[str]:
    return git_out(repo, "branch", "--format=%(refname:short)").split()


def _repo_with_branches(tmp_path: Path) -> Path:
    """A scratch repo carrying one branch of each class the sweep has to tell apart."""
    repo = tmp_path / "repo"
    _init_scratch_repo(repo)
    git(
        repo, "branch", "worktree-ancestry"
    )  # tip IS master: `git branch -d` accepts it
    git(repo, "branch", "feature-landed")  # merged, but not a session branch
    git(repo, "checkout", "-q", "-b", "worktree-open")
    (repo / "b.txt").write_text("two\n")
    git(repo, "add", "b.txt")
    git(repo, "commit", "-q", "-m", "open work", "--no-gpg-sign")
    git(repo, "checkout", "-q", "master")
    git(repo, "worktree", "add", "-q", "-b", "worktree-live", str(tmp_path / "live"))
    git(repo, "update-ref", "refs/remotes/origin/master", "master")
    return repo


def test_the_sweep_sees_only_session_branches_no_worktree_holds(tmp_path, monkeypatch):
    scrub_process_git_env(monkeypatch)
    repo = _repo_with_branches(tmp_path)

    # feature-landed is excluded by the prefix though it is merged; worktree-live is excluded
    # because a worktree holds it; master is excluded on both counts.
    assert sorted(orphan_branches(str(repo))) == ["worktree-ancestry", "worktree-open"]


def test_an_unmerged_session_branch_is_never_swept(tmp_path, monkeypatch):
    # The RED half. worktree-open carries a commit master does not have, so no local layer
    # settles it and it must survive a sweep that deletes its neighbour.
    scrub_process_git_env(monkeypatch)
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
    scrub_process_git_env(monkeypatch)
    repo = _repo_with_branches(tmp_path)
    git(repo, "checkout", "-q", "-b", "worktree-rebased")
    (repo / "c.txt").write_text("three\n")
    git(repo, "add", "c.txt")
    git(repo, "commit", "-q", "-m", "rebased work", "--no-gpg-sign")
    git(repo, "checkout", "-q", "master")
    # -x forces a new SHA: a same-second cherry-pick otherwise reproduces the original's.
    git(repo, "cherry-pick", "-x", "worktree-rebased")
    git(repo, "update-ref", "refs/remotes/origin/master", "master")

    assert "worktree-rebased" in landed_orphan_branches(str(repo), deep=True)
    ok, err = delete_branch(str(repo), "worktree-rebased")

    assert ok, err
    assert "worktree-rebased" not in _branch_names(repo)


def test_the_shallow_sweep_stops_at_the_bulk_ancestry_layer(tmp_path, monkeypatch):
    # The banner's budget is the constraint (`session-health.py` kills it at 5s), so deep=False
    # settles what one `git branch --merged` settles and nothing more. Asserted by what it
    # MISSES: a rebase-landed branch is exactly the case only the per-branch layers reach, so
    # deep=False returning it would mean it paid for them.
    scrub_process_git_env(monkeypatch)
    repo = _repo_with_branches(tmp_path)
    git(repo, "checkout", "-q", "-b", "worktree-rebased")
    (repo / "c.txt").write_text("three\n")
    git(repo, "add", "c.txt")
    git(repo, "commit", "-q", "-m", "rebased work", "--no-gpg-sign")
    git(repo, "checkout", "-q", "master")
    # -x forces a new SHA: a same-second cherry-pick otherwise reproduces the original's.
    git(repo, "cherry-pick", "-x", "worktree-rebased")
    git(repo, "update-ref", "refs/remotes/origin/master", "master")

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
    scrub_process_git_env(monkeypatch)
    repo = tmp_path / "repo"
    _init_scratch_repo(repo)
    git(repo, "worktree", "add", "-q", "-b", "worktree-done", str(tmp_path / "done"))
    git(repo, "update-ref", "refs/remotes/origin/master", "master")

    done = run([sys.executable, str(PRUNER), "--prune"], cwd=repo, env=scrubbed_env())

    assert done.returncode == 0, done.stderr
    assert not (tmp_path / "done").exists()
    assert "worktree-done" not in _branch_names(repo)


def test_check_names_the_layer_that_settled_a_rebase_landed_branch(
    tmp_path, monkeypatch
):
    # Not ancestry: the cherry-pick lands the content under a new sha, so only the patch-id
    # layer settles it, and naming that layer is what --check adds over a bare yes/no.
    scrub_process_git_env(monkeypatch)
    repo = _repo_with_branches(tmp_path)
    git(repo, "checkout", "-q", "-b", "worktree-rebased")
    (repo / "c.txt").write_text("three\n")
    git(repo, "add", "c.txt")
    git(repo, "commit", "-q", "-m", "rebased work", "--no-gpg-sign")
    git(repo, "checkout", "-q", "master")
    # -x forces a new SHA: a same-second cherry-pick otherwise reproduces the original's.
    git(repo, "cherry-pick", "-x", "worktree-rebased")
    git(repo, "update-ref", "refs/remotes/origin/master", "master")

    done = run(
        [sys.executable, str(PRUNER), "--check", "worktree-rebased"],
        cwd=repo,
        env=scrubbed_env(),
    )

    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "worktree-rebased: landed (patch-id)"


def test_check_exits_1_for_a_branch_holding_unlanded_work(tmp_path, monkeypatch):
    # The RED half. GH_BIN=false keeps the forge layer offline: it fails, and a failed forge
    # lookup is no verdict, which must read as unlanded.
    scrub_process_git_env(monkeypatch)
    repo = _repo_with_branches(tmp_path)

    done = run(
        [sys.executable, str(PRUNER), "--check", "worktree-open"],
        cwd=repo,
        env=scrubbed_env(GH_BIN="false"),
    )

    assert done.returncode == 1, done.stderr
    assert done.stdout.strip() == "worktree-open: unlanded"


# --- a registered worktree whose directory is gone (#4191) ---------------------------
#
# `git worktree list` keeps the registration, marked prunable, until `git worktree prune`
# runs. survey() asked is_dirty() about it, which ran git with a cwd that does not exist, and
# the FileNotFoundError took the whole report and every --prune down with it.


def _repo_with_a_vanished_worktree(tmp_path: Path) -> Path:
    """A scratch repo holding one merged live worktree and one registered at a deleted path."""
    repo = tmp_path / "repo"
    _init_scratch_repo(repo)
    git(repo, "worktree", "add", "-q", "-b", "worktree-done", str(tmp_path / "done"))
    git(repo, "worktree", "add", "-q", "--detach", str(tmp_path / "gone"))
    shutil.rmtree(tmp_path / "gone")
    git(repo, "update-ref", "refs/remotes/origin/master", "master")
    return repo


def test_report_survives_a_worktree_registered_at_a_deleted_path(tmp_path, monkeypatch):
    scrub_process_git_env(monkeypatch)
    repo = _repo_with_a_vanished_worktree(tmp_path)

    done = run([sys.executable, str(PRUNER)], cwd=repo, env=scrubbed_env())

    assert "FileNotFoundError" not in done.stderr
    assert done.returncode == 0, done.stderr


def test_report_still_names_the_live_worktrees_beside_a_vanished_one(
    tmp_path, monkeypatch
):
    # One stale registration must not hide the rest: the merged live tree is still reported.
    scrub_process_git_env(monkeypatch)
    repo = _repo_with_a_vanished_worktree(tmp_path)

    done = run([sys.executable, str(PRUNER)], cwd=repo, env=scrubbed_env())

    assert f"[{REMOVABLE:9}] {tmp_path / 'done'}" in done.stdout, done.stderr


def test_the_weekly_prune_removes_merged_worktrees_beside_a_vanished_one(
    tmp_path, monkeypatch
):
    # `--prune --brief` is the weekly cron's invocation; the stale registration blocked it.
    scrub_process_git_env(monkeypatch)
    repo = _repo_with_a_vanished_worktree(tmp_path)

    done = run(
        [sys.executable, str(PRUNER), "--prune", "--brief"],
        cwd=repo,
        env=scrubbed_env(),
    )

    assert done.returncode == 0, done.stderr
    assert not (tmp_path / "done").exists()
