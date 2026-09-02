"""The shared git runner: the repository is chosen by cwd, never by the environment."""

from __future__ import annotations

import subprocess

import pytest

from git import git, git_stdout


def _init_repo(path):
    env = {"GIT_CONFIG_GLOBAL": "/dev/null", "HOME": str(path), "PATH": "/usr/bin:/bin"}
    subprocess.run(
        ["git", "init", "-q", "-b", "master", str(path)], check=True, env=env
    )
    (path / "a.txt").write_text("a\n")
    subprocess.run(["git", "add", "a.txt"], cwd=path, check=True, env=env)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.com",
            "commit",
            "-q",
            "-m",
            "init",
            "--no-gpg-sign",
        ],
        cwd=path,
        check=True,
        env=env,
    )


def test_git_stdout_reads_the_repo_at_cwd(tmp_path):
    _init_repo(tmp_path)
    assert git_stdout("rev-parse", "--abbrev-ref", "HEAD", cwd=tmp_path) == "master"


def test_an_inherited_git_dir_does_not_redirect_the_call(tmp_path, monkeypatch):
    """The hook case: GIT_DIR points elsewhere, and cwd must still win."""
    other = tmp_path / "other"
    other.mkdir()
    _init_repo(other)
    mine = tmp_path / "mine"
    mine.mkdir()
    _init_repo(mine)
    (mine / "b.txt").write_text("b\n")
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(other))
    assert "b.txt" in git_stdout("status", "--porcelain", cwd=mine)


def test_check_true_raises_on_a_bad_ref(tmp_path):
    _init_repo(tmp_path)
    with pytest.raises(subprocess.CalledProcessError):
        git("rev-parse", "--verify", "no-such-ref", cwd=tmp_path)


def test_check_false_returns_the_exit_code(tmp_path):
    _init_repo(tmp_path)
    done = git("rev-parse", "--verify", "no-such-ref", cwd=tmp_path, check=False)
    assert done.returncode != 0
