#!/usr/bin/env python3
"""Tests for the session-worktree pruner's parsing and removal decision.

The git calls are kept out of both functions under test, so these run without a fixture
repo: `parse_worktree_list` takes porcelain text and `classify` takes the two facts the
git calls produce.

Run: uv run pytest scripts/test_prune_worktrees.py
"""

from __future__ import annotations

import os
import subprocess

from prune_worktrees import (
    KEEP,
    REMOVABLE,
    Worktree,
    cherry_says_merged,
    classify,
    find_orphan_dirs,
    parse_worktree_list,
    remove,
    session_is_alive,
)


def test_a_lock_held_by_a_running_process_reads_as_alive():
    assert session_is_alive(_live_lock_reason()) is True


def test_a_lock_naming_a_dead_process_reads_as_dead():
    # pid 1 exists but its start time will not match this fabricated one, which is the
    # pid-reuse case the start field is there to catch
    assert session_is_alive("claude session old (pid 1 start 999999999)") is False


def test_an_unparseable_lock_reason_is_treated_as_live():
    # someone else's lock in an unknown format; guessing wrong would destroy work
    assert session_is_alive("locked by hand while debugging") is True


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


PORCELAIN = """worktree /home/ubuntu/server
HEAD 15f277c8aa
branch refs/heads/master

worktree /home/ubuntu/server/.claude/worktrees/merged-work
HEAD 2ea6f0881b
branch refs/heads/worktree-merged-work
locked

worktree /home/ubuntu/server/.claude/worktrees/live-work
HEAD 88c45837cc
branch refs/heads/worktree-live-work

worktree /home/ubuntu/server/.claude/worktrees/detached
HEAD 4576e3160a
detached
"""


def test_parses_every_worktree_including_the_last():
    trees = parse_worktree_list(PORCELAIN)
    assert [t.path.split("/")[-1] for t in trees] == [
        "server",
        "merged-work",
        "live-work",
        "detached",
    ]


def test_strips_the_refs_heads_prefix_from_branches():
    trees = parse_worktree_list(PORCELAIN)
    assert trees[1].branch == "worktree-merged-work"


def test_detached_worktree_has_no_branch():
    trees = parse_worktree_list(PORCELAIN)
    assert trees[3].branch is None
    assert trees[3].head == "4576e3160a"


def test_reads_the_lock_marker():
    trees = parse_worktree_list(PORCELAIN)
    assert trees[1].locked is True
    assert trees[2].locked is False


def test_locked_with_a_reason_still_counts_as_locked():
    trees = parse_worktree_list(
        "worktree /w\nHEAD abc\nbranch refs/heads/b\nlocked in use by session 3\n"
    )
    assert trees[0].locked is True


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


def test_rebase_merged_branch_reads_as_merged():
    # The case that made this script keep merged worktrees forever: `gh pr merge --rebase`
    # replays the commit onto master as a new object, so it is not an ancestor — but its
    # patch is upstream, which is what `-` means.
    assert cherry_says_merged("- edf6dd1ec3ff0d5bfa201364a3fdf3ab5e072736") is True


def test_a_branch_with_unlanded_work_is_not_merged():
    assert cherry_says_merged("+ 4f5515ed1c0e4a2b8d3f9a7c6b5e4d3c2b1a0f9e") is False


def test_a_partly_landed_branch_is_not_merged():
    # One commit upstream, one not. Removing this worktree would lose the `+` commit.
    assert cherry_says_merged("- aaaaaaa\n+ bbbbbbb") is False


def test_nothing_ahead_of_upstream_reads_as_merged():
    assert cherry_says_merged("") is True


def test_blank_lines_do_not_change_the_verdict():
    assert cherry_says_merged("\n- aaaaaaa\n\n") is True


# --- remove(): the removal loop never had a test, which is why the unlock-before-remove
# bug survived CI — every test above exercises classify()/is_merged()/session_is_alive()/
# parse_worktree_list, none of them the actual `git worktree remove` call.


def test_remove_unlocks_before_removing_a_locked_tree(monkeypatch):
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr("prune_worktrees.subprocess.run", fake_run)
    ok, err = remove("/repo", _tree(locked=True))
    assert ok is True and err == ""
    assert calls == [
        ["git", "worktree", "unlock", "/w"],
        ["git", "worktree", "remove", "/w"],
    ]


def test_remove_skips_unlock_for_a_never_locked_tree(monkeypatch):
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr("prune_worktrees.subprocess.run", fake_run)
    remove("/repo", _tree(locked=False))
    assert calls == [["git", "worktree", "remove", "/w"]]


def test_remove_never_passes_force(monkeypatch):
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr("prune_worktrees.subprocess.run", fake_run)
    remove("/repo", _tree(locked=True))
    # --force (or -f -f) is the escape hatch git's own docs suggest for a locked tree; using
    # it here would remove the safety net that makes auto-unlock acceptable — a REMOVABLE
    # tree is only guaranteed merged AND clean, not that git agrees it's safe to delete.
    assert not any("-f" in c or "--force" in c for c in calls)


def _init_scratch_repo(path):
    subprocess.run(
        ["git", "init", "-q", "--initial-branch=master", str(path)], check=True
    )
    subprocess.run(
        ["git", "-C", str(path), "config", "user.email", "t@t.com"], check=True
    )
    subprocess.run(["git", "-C", str(path), "config", "user.name", "t"], check=True)
    (path / "a.txt").write_text("one\n")
    subprocess.run(["git", "-C", str(path), "add", "a.txt"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-q", "-m", "init"], check=True)


def test_remove_actually_deletes_a_locked_worktree_on_disk(tmp_path, monkeypatch):
    # Real git calls rather than mocks: the mocked tests above only prove call order, not
    # that git accepts the sequence. Without the unlock, `git worktree remove` on a locked
    # tree fails outright ("cannot remove a locked working tree") and the caller would
    # report success while removing nothing — this is the bug itself, reproduced.
    #
    # Scrub GIT_* first. git exports GIT_DIR and GIT_INDEX_FILE into hook processes, and
    # `git -C <path>` does NOT override them, so under the prek pre-commit hook every git
    # call below — including remove()'s own — targets the real repository rather than the
    # scratch one. Observed: it tried to commit into the live worktree and only an existing
    # index.lock stopped it.
    for var in [name for name in os.environ if name.startswith("GIT_")]:
        monkeypatch.delenv(var, raising=False)

    repo = tmp_path / "repo"
    _init_scratch_repo(repo)
    wt = tmp_path / "wt"
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", "-q", "-b", "feature", str(wt)],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "lock", str(wt), "--reason", "test lock"],
        check=True,
    )

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
