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
import subprocess
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


def _git(repo: pathlib.Path, *args: str) -> str:
    """Run git in `repo` with every inherited GIT_* variable removed.

    Same reason as scripts/dev/tests/test_prune_worktrees.py: under a pre-commit hook git
    exports GIT_DIR into the process, and `-C` does not override it.
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["GIT_AUTHOR_NAME"] = env["GIT_COMMITTER_NAME"] = "t"
    env["GIT_AUTHOR_EMAIL"] = env["GIT_COMMITTER_EMAIL"] = "t@example.invalid"
    return subprocess.run(
        ["git", *args], cwd=repo, env=env, check=True, capture_output=True, text=True
    ).stdout


def _repo_with_run_worktree(tmp_path, monkeypatch) -> tuple[pathlib.Path, pathlib.Path]:
    """A scratch repo whose `origin/master` holds two commits, plus the run worktree on
    `worktree-renovate-auto` at the second one. The worktree's branch starts level with master.

    Scrubs GIT_* from the environment for the code under test too: worktree_is_reusable's
    own git calls take no environment and would otherwise resolve to the live repository.
    """
    for var in [name for name in os.environ if name.startswith("GIT_")]:
        monkeypatch.delenv(var, raising=False)
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "--initial-branch=master")
    (repo / "a.txt").write_text("one\n")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-q", "-m", "init", "--no-gpg-sign")
    (repo / "a.txt").write_text("two\n")
    _git(repo, "commit", "-q", "-am", "base", "--no-gpg-sign")
    _git(repo, "update-ref", "refs/remotes/origin/master", "master")
    wt = repo / ".claude" / "worktrees" / "renovate-auto"
    _git(
        repo, "worktree", "add", "-q", "-b", "worktree-renovate-auto", str(wt), "master"
    )
    return repo, wt


class TestReusabilityAsksAboutContentNotAncestry:
    """A squash merge keeps a branch's content and discards its commits, so ancestry counts
    them forever. From 2026-09-14 the agent refused its own tree every day while its two
    commits sat on master as PR #1812 (#2014). Real git, not a fake: the premise under test
    is what `git merge-tree` answers for a squash and for a revert.
    """

    def test_a_squash_landed_branch_is_reusable(self, tmp_path, monkeypatch) -> None:
        repo, wt = _repo_with_run_worktree(tmp_path, monkeypatch)
        (wt / "a.txt").write_text("three\n")
        _git(wt, "commit", "-q", "-am", "bump one", "--no-gpg-sign")
        (wt / "b.txt").write_text("new\n")
        _git(wt, "add", "b.txt")
        _git(wt, "commit", "-q", "-m", "bump two", "--no-gpg-sign")
        _git(repo, "merge", "--squash", "-q", "worktree-renovate-auto")
        _git(repo, "commit", "-q", "-m", "squash of both", "--no-gpg-sign")
        _git(repo, "update-ref", "refs/remotes/origin/master", "master")
        # The stuck state itself: ancestry still counts the two commits as unlanded.
        ahead = _git(
            repo, "rev-list", "--count", "origin/master..worktree-renovate-auto"
        )
        assert ahead.strip() == "2"

        reusable, why = renovate_agent.worktree_is_reusable(
            str(repo), str(wt), "worktree-renovate-auto"
        )

        assert reusable and why == ""

    def test_a_revert_only_branch_is_refused(self, tmp_path, monkeypatch) -> None:
        """Merging a revert changes master's tree, so the branch still holds work."""
        repo, wt = _repo_with_run_worktree(tmp_path, monkeypatch)
        _git(wt, "revert", "--no-edit", "--no-gpg-sign", "HEAD")

        reusable, why = renovate_agent.worktree_is_reusable(
            str(repo), str(wt), "worktree-renovate-auto"
        )

        assert not reusable
        assert why == "worktree-renovate-auto holds 1 commit(s) not on origin/master"


class TestContainmentFailsClosed:
    """No verdict from git must read as NOT contained: a wrong yes here deletes work."""

    def _tools(self, merge_tree: tuple[int, str]) -> renovate_agent.AgentTools:
        def fake_run(argv, cwd=None, timeout=120):
            if "rev-parse" in argv:
                return 0, "0123abcd\n"
            if "merge-tree" in argv:
                return merge_tree
            raise AssertionError(f"unexpected call {argv}")

        return _tools(fake_run)

    def test_master_tree_as_the_merge_result_is_contained(self) -> None:
        tools = self._tools((0, "0123abcd\n"))
        assert renovate_agent.branch_content_is_on_master("/r", "b", tools)

    def test_a_conflicting_merge_tree_is_not_contained(self) -> None:
        tools = self._tools((1, "0123abcd\nCONFLICT (content): a.txt\n"))
        assert not renovate_agent.branch_content_is_on_master("/r", "b", tools)

    def test_empty_merge_tree_output_is_not_contained(self) -> None:
        tools = self._tools((0, ""))
        assert not renovate_agent.branch_content_is_on_master("/r", "b", tools)
