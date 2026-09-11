"""The shared git runner: the repository is chosen by cwd, never by the environment."""

import os
import subprocess
import time

import pytest

from git import git, gc_log_path, git_dirty, git_stdout, repair_object_store


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


# --- the object-store repair (#1435) ----------------------------------------------------
#
# Real git rather than mocks, because what is asserted here is what git DELETES: a mocked
# `git prune` proves the argv and nothing about whether the grace period holds.


def _unreachable_object(repo, text, age_days):
    """Write a loose object no ref points at, aged `age_days`, and return its file.

    `git prune` decides by the object file's mtime, so backdating the file is what makes an
    object old as far as the grace period is concerned.
    """
    blob = repo / "loose.txt"
    blob.write_text(text)
    sha = git("hash-object", "-w", "loose.txt", cwd=repo).stdout.strip()
    blob.unlink()
    path = repo / ".git" / "objects" / sha[:2] / sha[2:]
    assert path.exists(), path
    when = time.time() - age_days * 86400
    os.utime(path, (when, when))
    return path


def test_the_repair_drops_an_old_unreachable_object_and_clears_the_gc_log(tmp_path):
    """ACCEPT: worktree churn's leftovers go, and a stale gc.log goes with them.

    Both halves matter. While gc.log is there and fresh, `git gc --auto` prints it and exits
    however clean the store has since become.
    """
    _init_repo(tmp_path)
    stale = _unreachable_object(tmp_path, "left by a removed worktree\n", age_days=3)
    gc_log = tmp_path / ".git" / "gc.log"
    gc_log.write_text("warning: There are too many unreachable loose objects\n")

    lines = repair_object_store(tmp_path)

    assert not stale.exists(), lines
    assert not gc_log.exists(), lines
    assert any("automatic gc" in line for line in lines), lines


def test_the_repair_leaves_an_object_a_live_session_just_wrote(tmp_path):
    """REJECT: the grace period is real, so `--expire=now` cannot creep back in.

    Several sessions write into this object store at once. An object one of them has written
    but not yet pointed a ref at is unreachable and brand new, and deleting it destroys live
    work — which is why this prunes on a day's grace rather than immediately.
    """
    _init_repo(tmp_path)
    fresh = _unreachable_object(tmp_path, "another session is mid-commit\n", age_days=0)

    repair_object_store(tmp_path)

    assert fresh.exists()


def test_the_gc_log_is_read_from_the_shared_git_directory(tmp_path):
    """A worktree has a private git directory; gc.log only ever lives in the shared one."""
    repo, wt = tmp_path / "repo", tmp_path / "wt"
    _init_repo(repo)
    git("worktree", "add", "-q", "-b", "feature", str(wt), cwd=repo)

    assert gc_log_path(wt) == repo / ".git" / "gc.log"


def test_a_missing_gc_log_is_not_reported_as_a_repair(tmp_path):
    """The common case prints only the prune line, so a caller's report stays quiet."""
    _init_repo(tmp_path)

    lines = repair_object_store(tmp_path)

    assert not any("gc.log" in line for line in lines), lines
