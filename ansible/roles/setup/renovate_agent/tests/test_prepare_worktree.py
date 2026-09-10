"""prepare_worktree() must lock the run tree so a concurrent session's SessionStart pruner
(scripts/dev/prune_worktrees.py --prune) can't delete it mid-run, and must be able to unlock
its own previous tree so the next tick can still recreate it. See issue #1069.

It must also reclaim a directory git has no worktree record of, which is what a killed session
leaves behind and what stopped the agent for two days from 2026-09-08 (issue #1477).

Every boundary comes through `renovate_agent.AgentTools`, so nothing here patches a module
attribute — see that class's docstring and `ansible/tests/repo/monkeypatch_allowlist.txt`.

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
    """Stands in for renovate_agent.run(): records every argv, returns success.

    `registered` is the path list `git worktree list --porcelain` answers with, because
    prepare_worktree asks that question before deciding whether an existing directory is
    git's to remove or debris to reclaim.
    """

    def __init__(self, registered: list[str] | None = None) -> None:
        self.calls: list[list[str]] = []
        self.registered = registered or []

    def __call__(self, argv, cwd=None, timeout=120):
        self.calls.append(argv)
        if "list" in argv and "--porcelain" in argv:
            return 0, "".join(f"worktree {p}\nHEAD abc\n\n" for p in self.registered)
        return 0, ""

    def subcommands(self) -> list[str]:
        """The `git worktree <subcommand>` word of each recorded call, in order."""
        out = []
        for argv in self.calls:
            if "worktree" in argv:
                out.append(argv[argv.index("worktree") + 1])
        return out


def _tools(run, **kwargs) -> renovate_agent.AgentTools:
    return renovate_agent.AgentTools(run=run, **kwargs)


class TestLocksTheNewTree:
    def test_prepare_worktree_locks_after_add_with_a_parseable_reason(
        self, tmp_path
    ) -> None:
        recorder = _RecordingRun()
        path = str(tmp_path / "renovate-auto")

        renovate_agent.prepare_worktree(
            str(tmp_path), path, "worktree-renovate-auto", _tools(recorder)
        )

        assert recorder.subcommands() == ["prune", "add", "lock"]
        lock_call = recorder.calls[-1]
        assert lock_call[:5] == ["git", "-C", str(tmp_path), "worktree", "lock"]
        reason = lock_call[lock_call.index("--reason") + 1]
        match = LOCK_OWNER.search(reason)
        assert match, f"reason {reason!r} does not match LOCK_OWNER"
        assert int(match.group(1)) == os.getpid()

    def test_a_failed_lock_raises_naming_the_git_error(self, tmp_path) -> None:
        def failing_run(argv, cwd=None, timeout=120):
            if "lock" in argv:
                return 1, "fatal: unable to lock"
            return 0, ""

        path = str(tmp_path / "renovate-auto")

        with pytest.raises(RuntimeError, match="git worktree lock failed"):
            renovate_agent.prepare_worktree(
                str(tmp_path), path, "worktree-renovate-auto", _tools(failing_run)
            )


class TestUnlocksBeforeRemovingItsOwnTree:
    def test_prepare_worktree_unlocks_before_removing_an_existing_tree(
        self, tmp_path
    ) -> None:
        path = tmp_path / "renovate-auto"
        path.mkdir()
        recorder = _RecordingRun(registered=[str(path)])

        renovate_agent.prepare_worktree(
            str(tmp_path), str(path), "worktree-renovate-auto", _tools(recorder)
        )

        subs = recorder.subcommands()
        # git worktree remove refuses a locked tree outright, and one --force does not
        # override a lock (git requires it twice) — the unlock must precede the remove.
        assert subs.index("unlock") < subs.index("remove")
        assert path.is_dir(), "a registered tree is git's to remove, not ours to rmtree"


class TestReclaimsAnUnregisteredDirectory:
    """#1477: a killed session leaves a directory git has no worktree record of.

    `worktree remove` refuses a path it does not know, the return code was discarded, and the
    `worktree add` that followed died on `fatal: ... already exists` — every tick from
    2026-09-08 to 2026-09-10.
    """

    def test_an_unregistered_directory_is_removed_and_the_add_proceeds(
        self, tmp_path
    ) -> None:
        path = tmp_path / "renovate-auto"
        path.mkdir()
        (path / "ansible.cfg").write_text("debris\n")
        recorder = _RecordingRun(registered=[str(tmp_path)])

        renovate_agent.prepare_worktree(
            str(tmp_path), str(path), "worktree-renovate-auto", _tools(recorder)
        )

        assert not path.exists(), "the orphan must be gone before `worktree add` runs"
        assert recorder.subcommands() == ["list", "prune", "add", "lock"]

    def test_a_directory_that_cannot_be_removed_raises_naming_it(
        self, tmp_path
    ) -> None:
        """The rejecting half: reclaiming is best-effort, and a failure must not reach `add`."""
        path = tmp_path / "renovate-auto"
        path.mkdir()
        tools = _tools(_RecordingRun(), rmtree=lambda *a, **k: None)

        with pytest.raises(RuntimeError, match="orphaned directory"):
            renovate_agent.prepare_worktree(
                str(tmp_path), str(path), "worktree-renovate-auto", tools
            )


class TestReusabilityDoesNotReadThePrimaryCheckout:
    def test_an_unregistered_directory_is_never_git_statused(self, tmp_path) -> None:
        """git searches UPWARD, so `git -C <orphan> status` answers about the primary checkout."""
        path = tmp_path / "renovate-auto"
        path.mkdir()
        recorder = _RecordingRun(registered=[str(tmp_path)])

        reusable, why = renovate_agent.worktree_is_reusable(
            str(tmp_path), str(path), "worktree-renovate-auto", _tools(recorder)
        )

        assert reusable and why == ""
        assert not any("status" in argv for argv in recorder.calls)

    def test_a_registered_tree_with_uncommitted_work_is_still_refused(
        self, tmp_path
    ) -> None:
        """The rejecting half: the check that protects unlanded work must still fire."""
        path = tmp_path / "renovate-auto"
        path.mkdir()

        def fake_run(argv, cwd=None, timeout=120):
            if "list" in argv and "--porcelain" in argv:
                return 0, f"worktree {path}\nHEAD abc\n\n"
            if "status" in argv:
                return 0, " M ansible/deploy.yml\n"
            return 0, ""

        reusable, why = renovate_agent.worktree_is_reusable(
            str(tmp_path), str(path), "worktree-renovate-auto", _tools(fake_run)
        )

        assert not reusable and "uncommitted changes" in why
