"""`lib.git_testing` builds a scratch repository the inherited `GIT_*` environment cannot reach.

The property under test is the one the module exists for: with `GIT_DIR` and `GIT_INDEX_FILE`
pointing at ANOTHER repository — exactly what `prek`'s pytest hook exports — every call still
reads and writes the directory it was handed. A helper that merely happened to work because
the variables were absent would pass a test that did not set them, so each case sets them.

Run: uv run pytest scripts/lib/tests/test_git_testing.py
"""

import os
import subprocess
from pathlib import Path

import pytest

from lib.git_testing import (
    AUTHOR_EMAIL,
    commit,
    git,
    git_out,
    init_repo,
    scrub_process_git_env,
    scrubbed_env,
)


@pytest.fixture
def decoy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A second repository, installed in the environment the way a git hook installs one."""
    other = init_repo(tmp_path / "decoy", initial_commit="decoy")
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(other))
    monkeypatch.setenv("GIT_INDEX_FILE", str(other / ".git" / "index"))
    return other


def test_the_scrub_drops_every_git_variable(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("GIT_DIR", "/nowhere/.git")
    monkeypatch.setenv("GIT_AUTHOR_DATE", "2000-01-01T00:00:00Z")
    env = scrubbed_env()
    assert "GIT_DIR" not in env
    # The prefix filter, not a list of the three hook variables: GIT_AUTHOR_DATE would
    # redirect a scratch commit's timestamp just as effectively, and leakguard leaves it.
    assert "GIT_AUTHOR_DATE" not in env
    assert env["GIT_AUTHOR_EMAIL"] == AUTHOR_EMAIL
    assert env["GIT_CONFIG_GLOBAL"] == os.devnull


def test_an_override_survives_the_scrub(monkeypatch: pytest.MonkeyPatch):
    """`test_setup_drift_check.py` needs `GIT_TEST_ASSUME_DIFFERENT_OWNER` back afterwards."""
    monkeypatch.setenv("GIT_DIR", "/nowhere/.git")
    env = scrubbed_env(GIT_TEST_ASSUME_DIFFERENT_OWNER="1")
    assert env["GIT_TEST_ASSUME_DIFFERENT_OWNER"] == "1"
    assert "GIT_DIR" not in env


def test_a_commit_lands_in_the_handed_repo_not_the_one_in_the_environment(
    tmp_path: Path, decoy: Path
):
    """The failure this module exists to prevent, driven rather than described."""
    repo = init_repo(tmp_path / "scratch")
    sha = commit(repo, "first", **{"a.txt": "a\n"})

    assert git_out(repo, "rev-parse", "HEAD") == sha
    assert git_out(repo, "log", "-1", "--format=%s") == "first"
    # The decoy still holds only its own commit: nothing above reached it.
    assert git_out(decoy, "log", "--format=%s") == "decoy"


def test_a_file_passed_as_none_is_deleted(tmp_path: Path):
    repo = init_repo(tmp_path / "scratch")
    commit(repo, "add", **{"gone.txt": "x\n"})
    commit(repo, "remove", **{"gone.txt": None})
    assert not (repo / "gone.txt").exists()
    assert git_out(repo, "log", "--format=%s", "-1") == "remove"


def test_a_nested_path_commits_through_its_parent(tmp_path: Path):
    repo = init_repo(tmp_path / "scratch")
    commit(repo, "nested", **{"a/b/c.txt": "deep\n"})
    assert (repo / "a" / "b" / "c.txt").read_text() == "deep\n"


def test_a_bare_repo_takes_a_push(tmp_path: Path, decoy: Path):
    origin = init_repo(tmp_path / "origin.git", bare=True)
    repo = init_repo(tmp_path / "clone", initial_commit="base")
    git(repo, "remote", "add", "origin", str(origin))
    git(repo, "push", "-q", "origin", "master")
    assert git_out(origin, "log", "--format=%s") == "base"


def test_a_repo_with_no_initial_commit_has_no_head(tmp_path: Path):
    """The default is empty on purpose: `rev-parse HEAD` failing there is a case tests need."""
    repo = init_repo(tmp_path / "scratch")
    assert git(repo, "rev-parse", "HEAD", check=False).returncode != 0


def test_check_false_returns_the_failure_instead_of_raising(tmp_path: Path):
    repo = init_repo(tmp_path / "scratch")
    assert git(repo, "cat-file", "-e", "deadbeef", check=False).returncode != 0
    with pytest.raises(subprocess.CalledProcessError):
        git(repo, "cat-file", "-e", "deadbeef")


def test_scrub_process_git_env_clears_the_running_process(
    monkeypatch: pytest.MonkeyPatch, decoy: Path
):
    """For a subject that runs git in-process and builds no environment of its own."""
    dropped = scrub_process_git_env(monkeypatch)
    assert "GIT_DIR" in dropped
    assert [name for name in os.environ if name.startswith("GIT_")] == []
