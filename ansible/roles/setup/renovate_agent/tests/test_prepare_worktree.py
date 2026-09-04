"""prepare_worktree() must lock the run tree so a concurrent session's SessionStart pruner
(scripts/dev/prune_worktrees.py --prune) can't delete it mid-run, and must be able to unlock
its own previous tree so the next tick can still recreate it. See issue #1069.

Run: uv run pytest ansible/roles/setup/renovate_agent/tests/test_prepare_worktree.py
"""

import os
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "files"))
import pytest
import renovate_agent

# Mirrors scripts/dev/prune_worktrees.py's LOCK_OWNER — the lock reason must parse the same way
# session_is_alive() parses it, or a live run's lock reads as "unrecognized format" there too
# (which happens to also be treated as alive, but for the wrong reason: never rely on that).
LOCK_OWNER = re.compile(r"\(pid (\d+) start (\d+)\)")


class _RecordingRun:
    """Stands in for renovate_agent.run(): records every argv, returns success."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, argv, cwd=None, timeout=120):
        self.calls.append(argv)
        return 0, ""

    def subcommands(self) -> list[str]:
        """The `git worktree <subcommand>` word of each recorded call, in order."""
        out = []
        for argv in self.calls:
            if "worktree" in argv:
                out.append(argv[argv.index("worktree") + 1])
        return out


class TestLocksTheNewTree:
    def test_prepare_worktree_locks_after_add_with_a_parseable_reason(
        self, tmp_path, monkeypatch
    ) -> None:
        recorder = _RecordingRun()
        monkeypatch.setattr(renovate_agent, "run", recorder)
        path = str(tmp_path / "renovate-auto")

        renovate_agent.prepare_worktree(str(tmp_path), path, "worktree-renovate-auto")

        assert recorder.subcommands() == ["prune", "add", "lock"]
        lock_call = recorder.calls[-1]
        assert lock_call[:5] == ["git", "-C", str(tmp_path), "worktree", "lock"]
        reason = lock_call[lock_call.index("--reason") + 1]
        match = LOCK_OWNER.search(reason)
        assert match, f"reason {reason!r} does not match LOCK_OWNER"
        assert int(match.group(1)) == os.getpid()

    def test_a_failed_lock_raises_naming_the_git_error(
        self, tmp_path, monkeypatch
    ) -> None:
        def failing_run(argv, cwd=None, timeout=120):
            if "lock" in argv:
                return 1, "fatal: unable to lock"
            return 0, ""

        monkeypatch.setattr(renovate_agent, "run", failing_run)
        path = str(tmp_path / "renovate-auto")

        with pytest.raises(RuntimeError, match="git worktree lock failed"):
            renovate_agent.prepare_worktree(
                str(tmp_path), path, "worktree-renovate-auto"
            )


class TestUnlocksBeforeRemovingItsOwnTree:
    def test_prepare_worktree_unlocks_before_removing_an_existing_tree(
        self, tmp_path, monkeypatch
    ) -> None:
        recorder = _RecordingRun()
        monkeypatch.setattr(renovate_agent, "run", recorder)
        path = tmp_path / "renovate-auto"
        path.mkdir()

        renovate_agent.prepare_worktree(
            str(tmp_path), str(path), "worktree-renovate-auto"
        )

        subs = recorder.subcommands()
        # git worktree remove refuses a locked tree outright, and one --force does not
        # override a lock (git requires it twice) — the unlock must precede the remove.
        assert subs.index("unlock") < subs.index("remove")
