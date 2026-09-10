"""clean removes only a done batch whose PR merged, by prune_worktrees' content check — spec §4.

Run: uv run pytest scripts/dev/tests/test_fanout_clean.py
"""

import re

import pytest

from fanout_lib.clean import clean_one, remote_clean_command
from fanout_lib.manifest import Batch, Manifest, save
from fanout_place import main
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


def test_the_unlock_runs_before_remove_and_a_kept_tree_is_relocked():
    """Ruling 13: classify reads a `fanout-<batch>` lock reason as an alive session and
    never even looks at merged/dirty, so clean_one has to unlock before classify sees the
    tree — and put the lock back when the verdict stays kept, so an unrelated prune keeps
    leaving a still-running or still-unmerged batch alone.
    """
    tree = _tree(locked=True, lock_reason="fanout-b")
    calls = []

    def unlocker(repo, path):
        calls.append(("unlock", path))

    def locker(repo, path, reason):
        calls.append(("lock", path, reason))

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

    calls.clear()
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
    assert calls == [("unlock", tree.path), ("lock", tree.path, "fanout-b")]


def test_the_remote_command_resets_the_unit_then_runs_the_worktrees_own_copy_of_the_script():
    cmd = remote_clean_command(B)
    assert cmd.startswith(
        "systemctl --user reset-failed fanout-b 2>/dev/null; cd /home/ubuntu/server && "
    )
    assert (
        f"uv run python {B.worktree}/scripts/dev/fanout_place.py clean-one {B.worktree} {B.branch}"
        in cmd
    )


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
    assert not (tmp_path / f"{manifest.run_id}.json").exists()


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
