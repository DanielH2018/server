"""Tests for the deploy staleness guard.

The guard exists because of a measured incident (2026-08-19): a worktree 48 commits
behind master deployed stale templates and reverted live config for ~9 minutes, while
every repo-side check stayed green. See scripts/deploy_tools/deploy_staleness.py for the mechanism.

The git fixtures here build throwaway repos in tmp_path. They MUST scrub GIT_* from the
environment: when pytest runs from a pre-commit hook, GIT_DIR/GIT_INDEX_FILE are set and
`git -C <tmpdir>` does NOT override them, so an unscrubbed test mutates the real repo.
That failure is invisible standalone — the run that can't see it is the one that causes it.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from deploy_staleness import (
    STALE_EXIT,
    behind_ahead,
    format_refusal,
    main,
)


def _git(repo: Path, *args: str) -> str:
    """Run git in `repo` with every inherited GIT_* variable removed."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["GIT_AUTHOR_NAME"] = env["GIT_COMMITTER_NAME"] = "t"
    env["GIT_AUTHOR_EMAIL"] = env["GIT_COMMITTER_EMAIL"] = "t@example.invalid"
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def _commit(repo: Path, name: str) -> None:
    (repo / name).write_text(name)
    _git(repo, "add", name)
    _git(repo, "commit", "-m", name, "--no-gpg-sign")


@pytest.fixture
def repos(tmp_path):
    """An 'origin' with one commit, and a clone tracking it. Returns (origin, clone)."""
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-q", "-b", "master")
    _commit(origin, "base")

    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "-q", str(origin), str(clone))
    return origin, clone


def test_a_current_tree_is_not_behind(repos):
    _origin, clone = repos
    assert behind_ahead(clone, "origin/master") == (0, 0)


def test_a_tree_ahead_only_is_not_behind(repos):
    """Normal branch work.

    A slice deploy runs from a worktree with unmerged commits, so 'ahead' must never trip the guard.
    """
    _origin, clone = repos
    _commit(clone, "mine")
    assert behind_ahead(clone, "origin/master") == (0, 1)


def test_a_tree_behind_is_detected(repos):
    origin, clone = repos
    _commit(origin, "theirs")
    _git(clone, "fetch", "-q", "origin")
    assert behind_ahead(clone, "origin/master") == (1, 0)


def test_the_incident_shape_is_behind_and_ahead(repos):
    """2026-08-19 was both: 48 behind and 15 ahead. Being ahead must not mask being behind."""
    origin, clone = repos
    _commit(origin, "theirs")
    _commit(clone, "mine")
    _git(clone, "fetch", "-q", "origin")
    behind, ahead = behind_ahead(clone, "origin/master")
    assert behind == 1
    assert ahead == 1


def test_main_exits_zero_when_current(repos):
    _origin, clone = repos
    assert main(["--repo", str(clone), "--no-fetch"]) == 0


def test_main_exits_zero_when_only_ahead(repos):
    _origin, clone = repos
    _commit(clone, "mine")
    assert main(["--repo", str(clone), "--no-fetch"]) == 0


def test_main_refuses_when_behind(repos):
    origin, clone = repos
    _commit(origin, "theirs")
    _git(clone, "fetch", "-q", "origin")
    assert main(["--repo", str(clone), "--no-fetch"]) == STALE_EXIT


def test_refusal_names_the_count_and_the_fix(repos):
    """The message has to say what to run. The incident's cost was not knowing to look."""
    msg = format_refusal(behind=48, ahead=15, ref="origin/master")
    assert "48" in msg
    assert "rebase" in msg
    assert "--skip-staleness-check" in msg


def test_an_unresolvable_ref_does_not_refuse(repos):
    """A tree with no origin/master (a fresh init, a detached CI checkout) must not be
    blocked from deploying. The guard is a staleness check, not a git-topology check."""
    _origin, clone = repos
    assert (
        main(["--repo", str(clone), "--no-fetch", "--ref", "origin/nonexistent"]) == 0
    )
