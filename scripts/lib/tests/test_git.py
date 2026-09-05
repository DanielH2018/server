"""The shared git runner: the repository is chosen by cwd, never by the environment."""

import subprocess

import pytest

from git import git, git_dirty, git_stdout


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


def test_a_clean_tree_is_clean(tmp_path):
    _init_repo(tmp_path)
    assert git_dirty(tmp_path) is False


def test_a_modified_tracked_file_is_flagged(tmp_path):
    _init_repo(tmp_path)
    (tmp_path / "a.txt").write_text("changed\n")
    assert git_dirty(tmp_path) is True
    # Still dirty with untracked excluded: the change is to a TRACKED file.
    assert git_dirty(tmp_path, include_untracked=False) is True


def test_an_untracked_file_is_flagged_only_when_untracked_files_count(tmp_path):
    """The distinction the whole helper exists for.

    One untracked file counted by a bare `--porcelain` check is what parks the GitOps
    deployer; a job that commits a known set of files wants the other answer, so that an
    operator's unrelated scratch file does not make it skip its run.
    """
    _init_repo(tmp_path)
    (tmp_path / "scratch.txt").write_text("scratch\n")
    assert git_dirty(tmp_path) is True
    assert git_dirty(tmp_path, include_untracked=False) is False


def test_paths_scope_the_question(tmp_path):
    _init_repo(tmp_path)
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.txt").write_text("b\n")
    assert git_dirty(tmp_path, paths=["sub"]) is True
    assert git_dirty(tmp_path, paths=["a.txt"]) is False


def test_an_ambient_git_dir_cannot_redirect_the_answer(tmp_path, monkeypatch):
    """`cwd` decides, not the environment — the hazard `git -C` does not protect against."""
    clean, dirty = tmp_path / "clean", tmp_path / "dirty"
    clean.mkdir()
    dirty.mkdir()
    _init_repo(clean)
    _init_repo(dirty)
    (dirty / "scratch.txt").write_text("scratch\n")
    monkeypatch.setenv("GIT_DIR", str(dirty / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(dirty))
    assert git_dirty(clean) is False
