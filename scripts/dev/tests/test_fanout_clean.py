"""clean removes only a done batch whose PR merged, by prune_worktrees' content check — spec §4.

Run: uv run pytest scripts/dev/tests/test_fanout_clean.py
"""

import os
import re
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from fanout_lib.clean import clean_one, remote_clean_command
from fanout_lib.manifest import Batch, Manifest, path as manifest_path, save
from fanout_lib.transport import Tools
from fanout_place import cmd_clean_one, main
from prune_worktrees import Worktree, parse_worktree_list, remove
from _fanout_fakes import fake_tools, ok

B = Batch(
    "b",
    "daniel-server",
    "/home/ubuntu/server/.claude/worktrees/fanout-b",
    "worktree-fanout-b",
    "fanout-b",
    [1],
    "t",
)


def _tree(*, locked: bool = False, lock_reason: str = "") -> Worktree:
    return Worktree(
        path=B.worktree,
        head="abc",
        branch=B.branch,
        locked=locked,
        lock_reason=lock_reason,
    )


def _noop_unlocker(repo, path):
    pass


def _noop_locker(repo, path, reason):
    pass


def _git(repo: Path, *args: str, capture: bool = False) -> subprocess.CompletedProcess:
    """Run git in `repo` with every inherited GIT_* variable removed.

    Same identity-in-env approach as test_prune_worktrees.py's own `_git` helper: a bare
    `git config user.email` resolves GIT_DIR before `-C`, which would write the test
    identity into the real repository under a pre-commit hook rather than this scratch
    one — `prek`'s own `pytest` hook runs with `GIT_DIR` set, and every read here needs the
    same scrubbing as every write, or `git -C <repo>` silently reads the real checkout
    instead (confirmed: `prek run --all-files` failed both missing-directory tests below on
    exactly this before the reads were routed through here too).
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["GIT_AUTHOR_NAME"] = env["GIT_COMMITTER_NAME"] = "t"
    env["GIT_AUTHOR_EMAIL"] = env["GIT_COMMITTER_EMAIL"] = "t@example.invalid"
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        env=env,
        check=True,
        capture_output=capture,
        text=capture,
    )


def _init_scratch_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q", "--initial-branch=master")
    _git(path, "commit", "-q", "-m", "init", "--allow-empty", "--no-gpg-sign")


def _worktree_list(repo: Path) -> str:
    return _git(repo, "worktree", "list", "--porcelain", capture=True).stdout


def _branches(repo: Path) -> str:
    return _git(repo, "branch", "--list", capture=True).stdout


def test_a_merged_clean_tree_is_removed():
    removed = []
    state, _why = clean_one(
        "/home/ubuntu/server",
        _tree(),
        ask=lambda repo, head, branch="": True,
        dirty=lambda path: False,
        remover=lambda repo, tree: (removed.append(tree.path), (True, ""))[1],
        unlocker=_noop_unlocker,
        locker=_noop_locker,
    )
    assert state == "removed" and removed == [B.worktree]


def test_an_unmerged_tree_is_kept_and_named():
    state, why = clean_one(
        "/r",
        _tree(),
        ask=lambda *a, **k: False,
        dirty=lambda p: False,
        remover=lambda r, t: (True, ""),
        unlocker=_noop_unlocker,
        locker=_noop_locker,
    )
    assert state == "kept" and "not merged" in why


def test_a_dirty_tree_is_kept_even_when_merged():
    # classify's own reason text is "uncommitted changes", not "dirty" — read from
    # prune_worktrees.classify (scripts/dev/prune_worktrees.py:145) rather than the
    # word the brief guessed at.
    state, why = clean_one(
        "/r",
        _tree(),
        ask=lambda *a, **k: True,
        dirty=lambda p: True,
        remover=lambda r, t: (True, ""),
        unlocker=_noop_unlocker,
        locker=_noop_locker,
    )
    assert state == "kept" and "uncommitted" in why


def test_the_lock_is_released_only_for_a_tree_about_to_be_removed():
    """Ruling 13 (amended): classify judges an in-memory unlocked copy, so it never needs
    the real lock touched to reach a verdict. The real `git worktree lock`/`unlock` fire
    only around an actual removal — never on a KEEP.
    """
    tree = _tree(locked=True, lock_reason="fanout-b")
    calls = []

    def unlocker(repo, path):
        calls.append(("unlock", path))

    def locker(repo, path, reason):
        calls.append(("lock", path, reason))

    # Red proof: an unmerged tree is kept without the lock ever being touched.
    state, why = clean_one(
        "/r",
        tree,
        ask=lambda *a, **k: False,
        dirty=lambda p: False,
        remover=lambda r, t: (True, ""),
        unlocker=unlocker,
        locker=locker,
    )
    assert state == "kept" and "not merged" in why
    assert calls == []

    # A removable tree unlocks before remove, and stays unlocked once removed.
    state, why = clean_one(
        "/r",
        tree,
        ask=lambda *a, **k: True,
        dirty=lambda p: False,
        remover=lambda r, t: (calls.append(("remove", t.path, t.locked)), (True, ""))[
            1
        ],
        unlocker=unlocker,
        locker=locker,
    )
    assert state == "removed"
    assert calls == [("unlock", tree.path), ("remove", tree.path, False)]

    # Red proof: a removal that fails puts the lock back with its original reason.
    calls.clear()
    state, why = clean_one(
        "/r",
        tree,
        ask=lambda *a, **k: True,
        dirty=lambda p: False,
        remover=lambda r, t: (False, "disk full"),
        unlocker=unlocker,
        locker=locker,
    )
    assert state == "kept" and "disk full" in why
    assert calls == [("unlock", tree.path), ("lock", tree.path, "fanout-b")]


def _scrub_git_env(monkeypatch) -> None:
    """Strip every inherited `GIT_*` var for the rest of this test's process.

    `remove()` and `clean_one`'s own `git branch -D` call take no environment argument, so
    this is the only way to keep their subprocess calls off the real repository: under
    `prek`'s own `pytest` hook, `GIT_DIR` is set in the environment, and `-C <scratch repo>`
    does not override an inherited `GIT_DIR`. Same approach as
    test_prune_worktrees.py's own `test_remove_actually_deletes_a_locked_worktree_on_disk`.
    """
    for var in [name for name in os.environ if name.startswith("GIT_")]:
        monkeypatch.delenv(var, raising=False)


def test_a_missing_worktree_directory_is_deregistered_and_its_merged_branch_dropped(
    tmp_path, monkeypatch
):
    """Re-review item 1: a worktree `rm -rf`'d by hand rather than through the normal
    removal path stays registered (`prunable`, per git's own porcelain label). `clean_one`
    must deregister it instead of crashing in the real `is_dirty` on a cwd that no longer
    exists — this passes no `dirty` override, so the real default proves that.
    """
    _scrub_git_env(monkeypatch)
    repo = tmp_path / "repo"
    _init_scratch_repo(repo)
    wt = tmp_path / "wt"
    _git(repo, "worktree", "add", "-q", "-b", "gone-branch", str(wt))
    shutil.rmtree(wt)
    porcelain = _worktree_list(repo)
    assert "prunable" in porcelain  # the scenario this test exists to cover
    tree = next(t for t in parse_worktree_list(porcelain) if t.path == str(wt))

    state, why = clean_one(str(repo), tree, ask=lambda *a, **k: True, remover=remove)

    assert state == "removed" and "already gone" in why
    assert str(wt) not in _worktree_list(repo)
    assert "gone-branch" not in _branches(repo)


def test_a_missing_worktree_directory_keeps_its_unmerged_branch(tmp_path, monkeypatch):
    _scrub_git_env(monkeypatch)
    repo = tmp_path / "repo"
    _init_scratch_repo(repo)
    wt = tmp_path / "wt"
    _git(repo, "worktree", "add", "-q", "-b", "gone-branch", str(wt))
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
    """Ruling 23: `removed: … (already gone)` used to be printed even when the branch stayed.

    Real git refuses the delete here — the registered tree is renamed onto a branch that
    does not exist — so this exercises the same `git branch -D` the code runs, not a stub.
    """
    _scrub_git_env(monkeypatch)
    repo = tmp_path / "repo"
    _init_scratch_repo(repo)
    wt = tmp_path / "wt"
    _git(repo, "worktree", "add", "-q", "-b", "gone-branch", str(wt))
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


def test_the_remote_command_resets_fetches_then_runs_the_worktrees_own_copy_of_the_script():
    cmd = remote_clean_command(B)
    assert cmd.startswith(
        "systemctl --user reset-failed fanout-b 2>/dev/null; "
        "git -C /home/ubuntu/server fetch --quiet origin master && "
    )
    # The interpreter leg is now the else of the tree-exists test (Ruling 30), and still
    # runs from the primary checkout's cwd against the worktree's own copy of the script.
    assert (
        "else cd /home/ubuntu/server && "
        "uv run --no-project --no-python-downloads --python 3.14.6 python "
        f"{B.worktree}/scripts/dev/fanout_place.py clean-one {B.worktree} {B.branch}"
        in cmd
    )


def _porcelain(*paths: str, locked_reason: str | None = None) -> str:
    lock_line = f"locked {locked_reason}\n" if locked_reason else ""
    return "".join(
        f"worktree {p}\nHEAD abc\nbranch refs/heads/worktree-fanout-b\n{lock_line}\n"
        for p in paths
    )


def test_cmd_clean_one_reaches_clean_one_for_a_registered_tree(capsys):
    args = SimpleNamespace(worktree=B.worktree)
    code = cmd_clean_one(
        args,
        Tools(),
        list_worktrees=lambda: _porcelain(B.worktree),
        ask=lambda *a, **k: False,
        dirty=lambda p: False,
    )
    assert code == 0
    # "kept ... not merged" only comes out of clean_one itself, proving the tree was found
    # and handed to it rather than short-circuited by the absent-tree branch.
    out = capsys.readouterr().out
    assert "kept:" in out and "not merged" in out


def test_cmd_clean_one_reports_removed_for_an_absent_tree(capsys):
    """Critical 1: an already-gone worktree is the goal state, not a failure — it must read
    `removed`, or a re-run of `clean` after a partial first pass can never delete the
    manifest (Ruling C).
    """
    args = SimpleNamespace(worktree=B.worktree)
    code = cmd_clean_one(args, Tools(), list_worktrees=lambda: "")
    assert code == 0
    out = capsys.readouterr().out
    assert out.startswith("removed:")
    assert "already gone" in out


def test_cmd_clean_one_matches_a_symlinked_path_spelling(tmp_path, capsys):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    args = SimpleNamespace(worktree=str(link))
    code = cmd_clean_one(
        args,
        Tools(),
        list_worktrees=lambda: _porcelain(str(real)),
        ask=lambda *a, **k: False,
        dirty=lambda p: False,
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "already gone" not in out
    assert "kept:" in out
    assert Path(link).resolve() == Path(real).resolve()


def test_cmd_clean_one_forwards_unlocker_and_locker_seams(tmp_path):
    """Re-review item 4's caveat: without its own `unlocker`/`locker` seams, a
    REMOVABLE-with-lock case run through `cmd_clean_one` (rather than through `clean_one`
    directly) would shell a real worktree unlock against the primary checkout. `wt` is a
    real directory here so `clean_one` takes its normal path, not the missing-tree one.
    """
    wt = tmp_path / "wt"
    wt.mkdir()
    calls = []
    args = SimpleNamespace(worktree=str(wt))
    code = cmd_clean_one(
        args,
        Tools(),
        list_worktrees=lambda: _porcelain(str(wt), locked_reason="fanout-b"),
        ask=lambda *a, **k: True,
        dirty=lambda p: False,
        remover=lambda r, t: (True, ""),
        unlocker=lambda r, p: calls.append(("unlock", p)),
        locker=lambda r, p, reason: calls.append(("lock", p, reason)),
    )
    assert code == 0
    assert calls == [("unlock", str(wt))]


def _manifest(tmp_path, batches):
    manifest = Manifest("20260101T000010Z", "o", batches)
    save(manifest, root=tmp_path)
    return manifest


def test_clean_deletes_the_manifest_once_every_batch_is_removed(tmp_path):
    b1 = Batch("1", "daniel-box", "/w1", "worktree-fanout-1", "fanout-1", [1], "t")
    b2 = Batch("2", "daniel-server", "/w2", "worktree-fanout-2", "fanout-2", [2], "t")
    manifest = _manifest(tmp_path, [b1, b2])
    tools, _run = fake_tools(
        answers={"daniel-box": ok("removed: /w1"), "daniel-server": ok("removed: /w2")}
    )
    code = main(["clean", manifest.run_id, "--manifest-root", str(tmp_path)], tools)
    assert code == 0
    assert not manifest_path(manifest.run_id, tmp_path).exists()


def test_clean_keeps_the_manifest_when_one_batch_is_not_removed(tmp_path, capsys):
    b1 = Batch("1", "daniel-box", "/w1", "worktree-fanout-1", "fanout-1", [1], "t")
    b2 = Batch("2", "daniel-server", "/w2", "worktree-fanout-2", "fanout-2", [2], "t")
    manifest = _manifest(tmp_path, [b1, b2])
    tools, _run = fake_tools(
        answers={
            "daniel-box": ok("removed: /w1"),
            "daniel-server": ok(
                "kept: /w2 worktree-fanout-2 not merged into origin/master"
            ),
        }
    )
    code = main(["clean", manifest.run_id, "--manifest-root", str(tmp_path)], tools)
    assert code == 0
    assert (tmp_path / f"{manifest.run_id}.json").exists()
    assert "kept 2" in capsys.readouterr().out


def test_help_lists_clean_and_gives_clean_one_no_help_bullet(capsys):
    with pytest.raises(SystemExit):
        main(["--help"])
    plain = re.sub(r"\x1b\[[0-9;]*m", "", capsys.readouterr().out)
    assert "{read,launch,status,stop,clean,clean-one}" in plain
    # No subcommand here gets a help bullet under "positional arguments" — clean-one stays
    # exactly as undocumented as its siblings, rather than standing out with its own line.
    assert "clean-one " not in plain.split("positional arguments:", 1)[1]
