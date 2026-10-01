"""clean removes only a done batch whose PR merged, by prune_worktrees' content check — spec §4.

Run: uv run pytest scripts/dev/tests/test_fanout_clean.py
"""

import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from fanout_lib.clean import clean_one, remote_clean_command
from fanout_lib.manifest import Batch, Manifest, path as manifest_path, save
from fanout_lib.transport import Tools
from fanout_place import cmd_clean_one, main
from prune_worktrees import Worktree
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


def test_the_remote_command_resets_fetches_then_runs_the_worktrees_own_copy_of_the_script():
    cmd = remote_clean_command(B)
    # The active-unit refusal is the outermost test; everything else is its else.
    assert cmd.startswith(
        "if systemctl --user is-active --quiet fanout-b; then "
        f'echo "kept: {B.worktree} — unit fanout-b still active; stop it first"; '
        "else "
        "systemctl --user reset-failed fanout-b 2>/dev/null; "
        "git -C /home/ubuntu/server fetch --quiet origin master && "
    )
    # The interpreter leg is the else of the tree-exists test (Ruling 30), and runs from
    # the primary checkout's cwd against the worktree's own copy of the script.
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
        brancher=lambda r, b: (calls.append(("branch -D", b)), (True, ""))[1],
    )
    assert code == 0
    assert calls == [("unlock", str(wt)), ("branch -D", "worktree-fanout-b")]


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
