"""clean removes only a done batch whose PR merged, by lib.worktrees' content check — spec §4.

Run: uv run pytest scripts/dev/tests/test_fanout_clean.py
"""

import dataclasses
import json
import re
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from dev.fanout_lib import clean as clean_mod
from dev.fanout_lib.clean import PARKED_KEPT, clean_one, remote_clean_command
from dev.fanout_lib.manifest import (
    Batch,
    Manifest,
    path as manifest_path,
    retired_path,
    save,
)
from dev.fanout_lib.target import SERVER_CHECKOUT, Target
from dev.fanout_lib.transport import Tools
from dev.fanout_lib.place import cmd_clean_one, main
from lib.git_testing import commit, git, init_repo, scrubbed_env
from lib.proc_testing import fake_bin, path_with, run
from lib.worktree_testing import ignore_foreign_slice_holds_env
from lib.worktrees import Worktree
from _fanout_fakes import as_operator, fake_tools, ok

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
        brancher=lambda repo, branch: (True, ""),
    )
    assert state == "removed" and removed == [B.worktree]


def test_a_removed_tree_whose_branch_refuses_to_delete_is_reported_as_kept():
    state, why = clean_one(
        "/r",
        _tree(),
        ask=lambda *a, **k: True,
        dirty=lambda p: False,
        remover=lambda r, t: (True, ""),
        unlocker=_noop_unlocker,
        locker=_noop_locker,
        brancher=lambda repo, branch: (False, "error: branch is checked out"),
    )
    assert state == "kept"
    assert f"branch {B.branch} not deleted: error: branch is checked out" in why


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


@pytest.mark.parametrize(
    ("merged", "verdict"), [(False, ("kept", PARKED_KEPT)), (True, ("removed", ""))]
)
def test_a_parked_batch_is_kept_until_its_pr_has_merged(tmp_path, merged, verdict):
    """Its claims expire with the worktree, while the open PR still holds the edits."""
    (tmp_path / ".fanout").mkdir()
    report = {"type": "result", "result": "parked: refused before merge (a hold)\nPR"}
    (tmp_path / ".fanout" / "report.json").write_text(json.dumps(report) + "\n")
    got = clean_one(
        "/r",
        dataclasses.replace(_tree(), path=str(tmp_path)),
        ask=lambda *a, **k: merged,
        dirty=lambda p: False,
        remover=lambda r, t: (True, ""),
        unlocker=_noop_unlocker,
        locker=_noop_locker,
        brancher=lambda r, b: (True, ""),
    )
    assert got == verdict


def test_a_dirty_tree_is_kept_even_when_merged():
    # classify's own reason text is "uncommitted changes", not "dirty" — read from
    # lib.worktrees.classify rather than the
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
        brancher=lambda r, b: (True, ""),
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


def test_the_remote_command_resets_fetches_then_runs_a_snapshot_of_master():
    cmd = as_operator(remote_clean_command(B))
    # The active-unit refusal is the outermost test; everything else is its else.
    assert cmd.startswith(
        "if systemctl --user is-active --quiet fanout-b; then "
        f'echo "kept: {B.worktree} — unit fanout-b still active; stop it first"; '
        "else "
        "systemctl --user reset-failed fanout-b 2>/dev/null; "
        "git -C /home/ubuntu/server fetch --quiet origin master && "
    )
    # The interpreter leg is the else of the tree-exists test (Ruling 30). It runs from the
    # primary checkout's cwd, and the script it runs is the snapshot's.
    assert (
        'else snap=$(mktemp -d) && { mkdir -p "$snap"/server && '
        'git -C /home/ubuntu/server archive -o "$snap"/server.tar origin/master scripts && '
        'tar -xf "$snap"/server.tar -C "$snap"/server && rm "$snap"/server.tar && '
        "cd /home/ubuntu/server && RUN_HOOK_PROJECT_DIR=/home/ubuntu/server "
        'PYTHONPATH="$snap"/server/scripts '
        "uv run --no-project --no-python-downloads --python 3.15.2 python "
        f'"$snap"/server/scripts/dev/fanout.py place clean-one {B.worktree} {B.branch}; '
        'rc=$?; rm -rf "$snap"; (exit $rc); }; ' in cmd
    )
    assert "python /home/ubuntu/server/scripts" not in cmd
    assert f"{B.worktree}/scripts/dev" not in cmd


STALE_SCRIPT = (
    'import sys\nprint("kept:", sys.argv[-2], "judged by a stale fanout.py")\n'
)
SCRIPTS = Path(clean_mod.__file__).resolve().parents[2]


def _scratch_with_a_stale_primary(tmp_path):
    """A primary checkout behind `origin/master`, and a locked batch worktree cut from it.

    `origin/master` holds this suite's `scripts/`. The primary's work tree and the batch
    worktree both hold the commit before it, whose `fanout.py` keeps every tree. The batch
    branch holds a change master lacks, so no local layer of `is_merged` calls it landed.
    """
    origin = init_repo(tmp_path / "origin.git", bare=True)
    primary = init_repo(tmp_path / "primary")
    git(primary, "remote", "add", "origin", str(origin))
    commit(primary, "stale", **{"scripts/dev/fanout.py": STALE_SCRIPT})
    worktree = tmp_path / "w"
    git(primary, "worktree", "add", "-q", "-b", B.branch, str(worktree))
    commit(worktree, "work", **{"feature.txt": "unlanded\n"})
    git(primary, "worktree", "lock", "--reason", B.unit, str(worktree))
    shutil.rmtree(primary / "scripts")
    shutil.copytree(
        SCRIPTS,
        primary / "scripts",
        ignore=shutil.ignore_patterns("__pycache__", "tests", "*.json"),
    )
    commit(primary, "current")
    git(primary, "push", "-q", "origin", "master")
    git(primary, "reset", "-q", "--hard", "HEAD~1")
    return primary, worktree


def _clean_for_real(tmp_path, primary, worktree, gh):
    """Run `place clean` over one batch, executing the chain it builds under bash.

    `gh` is the stub forge. `uv` drops its flags and runs this suite's interpreter, and
    `systemctl` reports the unit inactive. The slice rule is off in that bash's `clean-one`;
    see `lib.worktree_testing`.

    Returns:
        `(manifest, exit status)`.
    """
    stub_bin = fake_bin(
        tmp_path / "bin",
        systemctl='#!/bin/sh\ncase "$2" in is-active) exit 3 ;; esac\nexit 0\n',
        gh=gh,
        uv=(
            '#!/bin/sh\nwhile [ "$1" != python ]; do shift; done; shift\n'
            f'exec {sys.executable} "$@"\n'
        ),
    )
    (tmp_path / "tmp").mkdir()
    env = scrubbed_env(
        TMPDIR=str(tmp_path / "tmp"), **ignore_foreign_slice_holds_env(tmp_path)
    )
    env["PATH"] = path_with(stub_bin, env=env)

    def run_here(_host, command, timeout, stdin=None):
        command = command.replace(SERVER_CHECKOUT, str(primary))
        assert SERVER_CHECKOUT not in command  # never aimed at the shared checkout
        return run(["bash", "-c", command], env=env, timeout=timeout)

    batch = dataclasses.replace(B, worktree=str(worktree))
    manifest = _manifest(tmp_path / "runs", [batch])
    tools = dataclasses.replace(fake_tools()[0], run=run_here)
    code = main(
        ["clean", manifest.run_id, "--manifest-root", str(tmp_path / "runs")], tools
    )
    return manifest, code


def test_a_landed_batch_is_removed_by_masters_clean_one_though_the_primary_checkout_is_stale(
    tmp_path, capsys
):
    """daniel-server's case: no job fast-forwards its primary checkout.

    `gh` answers a merged head that is not the local tip and a compare of `ahead`, which is a
    squash-merged batch `land.sh` merged master into.
    """
    primary, worktree = _scratch_with_a_stale_primary(tmp_path)
    gh = f'#!/bin/sh\ncase "$1" in\n  pr) echo {"f" * 40} ;;\n  api) echo ahead ;;\nesac\n'

    manifest, code = _clean_for_real(tmp_path, primary, worktree, gh)

    out = capsys.readouterr().out
    assert code == 0, out
    assert f"b on daniel-server: removed: {worktree}" in out
    assert not worktree.exists()
    assert B.branch not in git(primary, "branch", "--list").stdout
    assert not manifest_path(manifest.run_id, tmp_path / "runs").exists()
    assert list((tmp_path / "tmp").iterdir()) == []


def test_an_unmerged_batch_is_kept_by_masters_clean_one(tmp_path, capsys):
    primary, worktree = _scratch_with_a_stale_primary(tmp_path)

    manifest, code = _clean_for_real(tmp_path, primary, worktree, "#!/bin/sh\n")

    out = capsys.readouterr().out
    assert code == 0, out
    assert f"b on daniel-server: kept: {worktree} {B.branch} not merged" in out
    assert (worktree / "feature.txt").exists()
    assert B.branch in git(primary, "branch", "--list").stdout
    assert manifest_path(manifest.run_id, tmp_path / "runs").exists()


def _porcelain(*paths: str, locked_reason: str | None = None) -> str:
    lock_line = f"locked {locked_reason}\n" if locked_reason else ""
    return "".join(
        f"worktree {p}\nHEAD abc\nbranch refs/heads/worktree-fanout-b\n{lock_line}\n"
        for p in paths
    )


def test_cmd_clean_one_reaches_clean_one_for_a_registered_tree(capsys):
    args = SimpleNamespace(worktree=B.worktree, repo="DanielH2018/server")
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


def test_cmd_clean_one_default_ask_merges_into_the_targets_base():
    """Every other test here injects `ask`, so none built the default one. A worktree path
    once rebound the name holding the Target, and every real `clean` died on
    `'PosixPath' object has no attribute 'base'` with this file green.
    """
    from dev.fanout_lib.target import resolve

    seen = {}

    def fake_clean_one(checkout, tree, *, ask, **_):
        seen["base"] = ask.keywords["base"]
        return "kept", "(fake)"

    args = SimpleNamespace(worktree=B.worktree, repo="DanielH2018/server")
    code = cmd_clean_one(
        args,
        Tools(),
        list_worktrees=lambda: _porcelain(B.worktree),
        clean=fake_clean_one,
    )
    assert code == 0
    assert seen["base"] == resolve("DanielH2018/server").base


def test_cmd_clean_one_reports_removed_for_an_absent_tree(capsys):
    """Critical 1: an already-gone worktree is the goal state, not a failure — it must read
    `removed`, or a re-run of `clean` after a partial first pass can never delete the
    manifest (Ruling C).
    """
    args = SimpleNamespace(worktree=B.worktree, repo="DanielH2018/server")
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
    args = SimpleNamespace(worktree=str(link), repo="DanielH2018/server")
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
    args = SimpleNamespace(worktree=str(wt), repo="DanielH2018/server")
    code = cmd_clean_one(
        args,
        Tools(),
        list_worktrees=lambda: _porcelain(str(wt), locked_reason="fanout-b"),
        ask=lambda *a, **k: True,
        dirty=lambda p: False,
        remover=lambda r, t: (True, ""),
        unlocker=lambda r, p: calls.append(("unlock", p)),
        locker=lambda r, p, reason: calls.append(("lock", p, reason)),
        brancher=lambda r, b: (calls.append(("branch -D", b)), (True, ""))[1],
    )
    assert code == 0
    assert calls == [("unlock", str(wt)), ("branch -D", "worktree-fanout-b")]


def _manifest(tmp_path, batches):
    manifest = Manifest("20260101T000010Z", "o", batches)
    save(manifest, root=tmp_path)
    return manifest


def test_clean_retires_the_manifest_once_every_batch_is_removed(tmp_path):
    b1 = Batch("1", "daniel-box", "/w1", "worktree-fanout-1", "fanout-1", [1], "t")
    b2 = Batch("2", "daniel-server", "/w2", "worktree-fanout-2", "fanout-2", [2], "t")
    manifest = _manifest(tmp_path, [b1, b2])
    tools, _run = fake_tools(
        answers={"daniel-box": ok("removed: /w1"), "daniel-server": ok("removed: /w2")}
    )
    code = main(["clean", manifest.run_id, "--manifest-root", str(tmp_path)], tools)
    assert code == 0
    assert not manifest_path(manifest.run_id, tmp_path).exists()
    retired = json.loads(retired_path(manifest.run_id, tmp_path).read_text())
    assert all(b["removed_at"] for b in retired["batches"])


def test_clean_keeps_the_manifest_when_one_batch_is_not_removed(tmp_path, capsys):
    b1 = Batch("1", "daniel-box", "/w1", "worktree-fanout-1", "fanout-1", [1], "t")
    b2 = Batch("2", "daniel-server", "/w2", "worktree-fanout-2", "fanout-2", [2], "t")
    manifest = _manifest(tmp_path, [b1, b2])
    tools, _run = fake_tools(
        answers={
            "daniel-box": ok("removed: /w1"),
            "daniel-server": ok("kept: /w2 worktree-fanout-2 not merged"),
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
    assert (
        "{read,launch,claim,status,stop,clean,abandon,resume,runs,clean-one}" in plain
    )
    # No subcommand here gets a help bullet under "positional arguments" — clean-one stays
    # exactly as undocumented as its siblings, rather than standing out with its own line.
    assert "clean-one " not in plain.split("positional arguments:", 1)[1]


def test_a_dotfiles_clean_fetches_main_and_runs_this_repos_copy_with_repo():
    """Its worktree carries no copy of this script, and its default branch is `main`."""
    wt = "/home/ubuntu/.local/share/chezmoi/.claude/worktrees/fanout-763"
    dotfiles = Batch(
        "763",
        "daniel-box",
        wt,
        "worktree-fanout-763",
        "fanout-dotfiles-763",
        [763],
        "t",
        repo="DanielH2018/dotfiles",
    )
    target = Target(
        "DanielH2018/dotfiles", "/home/ubuntu/.local/share/chezmoi", "origin/main"
    )
    cmd = as_operator(remote_clean_command(dotfiles, target=target))
    assert (
        "git -C /home/ubuntu/.local/share/chezmoi fetch --quiet origin main && " in cmd
    )
    # The chain's own fetch is of dotfiles, so the leg fetches this repo's master itself.
    assert (
        "else snap=$(mktemp -d) && { "
        "git -C /home/ubuntu/server fetch --quiet origin master && "
        'mkdir -p "$snap"/server && git -C /home/ubuntu/server archive ' in cmd
    )
    assert (
        "cd /home/ubuntu/.local/share/chezmoi && RUN_HOOK_PROJECT_DIR=/home/ubuntu/server "
        'PYTHONPATH="$snap"/server/scripts '
        "uv run --no-project --no-python-downloads --python 3.15.2 python "
        f'"$snap"/server/scripts/dev/fanout.py place clean-one {wt} '
        "worktree-fanout-763 --repo DanielH2018/dotfiles; " in cmd
    )
    assert f"{wt}/scripts/dev" not in cmd
