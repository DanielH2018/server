#!/usr/bin/env python3
"""Tests for how `prune_worktrees.py --prune` treats orphan directories under .claude/worktrees/.

An orphan is a directory git no longer registers as a worktree (#4425). One with no regular
files, or whose every file's content is already in the object store, holds nothing git lacks
and `--prune` removes it. One holding content git never stored is kept and reported with each
such file named. Each test drives the script as a subprocess against a scratch repo, so the
object-store check runs against real git.

Run: uv run pytest scripts/dev/tests/test_prune_worktrees_orphan_dirs.py
"""

import subprocess
import sys
from pathlib import Path

from lib.git_testing import (
    commit,
    git,
    init_repo,
    scrub_process_git_env,
    scrubbed_env,
)
from lib.proc_testing import run

PRUNER = Path(__file__).resolve().parents[1] / "prune_worktrees.py"

# The scratch repo commits this content as `a.txt`, so a copy of it is already stored.
TRACKED_CONTENT = "one\n"


def _repo(tmp_path: Path, monkeypatch) -> Path:
    scrub_process_git_env(monkeypatch)
    repo = tmp_path / "repo"
    init_repo(repo)
    commit(repo, "init", **{"a.txt": TRACKED_CONTENT})
    git(repo, "update-ref", "refs/remotes/origin/master", "master")
    return repo


def _orphan(repo: Path, name: str) -> Path:
    path = repo / ".claude" / "worktrees" / name
    path.mkdir(parents=True)
    return path


def _prune(repo: Path):
    return run([sys.executable, str(PRUNER), "--prune"], cwd=repo, env=scrubbed_env())


def test_prune_removes_an_orphan_holding_only_empty_directories(tmp_path, monkeypatch):
    repo = _repo(tmp_path, monkeypatch)
    orphan = _orphan(repo, "bridge-empty")
    (orphan / "ansible" / "roles").mkdir(parents=True)
    (orphan / "docs").mkdir()

    done = _prune(repo)

    assert done.returncode == 0, done.stderr
    assert not orphan.exists(), done.stdout


def test_prune_removes_an_orphan_whose_files_git_already_stores(tmp_path, monkeypatch):
    repo = _repo(tmp_path, monkeypatch)
    orphan = _orphan(repo, "bridge-copy")
    (orphan / "a.txt").write_text(TRACKED_CONTENT)
    # The same stored content under another path is still content git holds.
    (orphan / "sub").mkdir()
    (orphan / "sub" / "renamed-copy.txt").write_text(TRACKED_CONTENT)

    done = _prune(repo)

    assert done.returncode == 0, done.stderr
    assert not orphan.exists(), done.stdout


def test_prune_keeps_an_orphan_with_unstored_content_and_names_the_file(
    tmp_path, monkeypatch
):
    repo = _repo(tmp_path, monkeypatch)
    orphan = _orphan(repo, "bridge-work")
    (orphan / "fresh-content.txt").write_text("work git never stored\n")

    done = _prune(repo)

    assert done.returncode == 0, done.stderr
    assert orphan.is_dir()
    assert "fresh-content.txt" in done.stdout, done.stdout


def test_prune_removes_an_orphan_whose_only_other_file_is_its_gitdir_pointer(
    tmp_path, monkeypatch
):
    repo = _repo(tmp_path, monkeypatch)
    orphan = _orphan(repo, "bridge-pointer")
    (orphan / ".git").write_text(f"gitdir: {repo}/.git/worktrees/bridge-pointer\n")
    (orphan / "a.txt").write_text(TRACKED_CONTENT)

    done = _prune(repo)

    assert done.returncode == 0, done.stderr
    assert not orphan.exists(), done.stdout


def test_prune_keeps_an_orphan_whose_git_file_holds_more_than_a_pointer(
    tmp_path, monkeypatch
):
    repo = _repo(tmp_path, monkeypatch)
    orphan = _orphan(repo, "bridge-not-pointer")
    (orphan / ".git").write_text("gitdir: /elsewhere\nnote git never stored\n")

    done = _prune(repo)

    assert orphan.is_dir()
    assert ".git" in done.stdout, done.stdout


def test_a_kept_orphan_names_only_the_files_git_never_stored(tmp_path, monkeypatch):
    repo = _repo(tmp_path, monkeypatch)
    orphan = _orphan(repo, "bridge-mixed")
    (orphan / "stored-copy.txt").write_text(TRACKED_CONTENT)
    (orphan / "truncated-template.j2").write_text("cpu_re")

    done = _prune(repo)

    assert orphan.is_dir()
    assert "truncated-template.j2" in done.stdout, done.stdout
    assert "stored-copy.txt" not in done.stdout, done.stdout


def test_prune_skips_an_orphan_a_process_still_uses(tmp_path, monkeypatch):
    # The idle sibling is the same kind of orphan, so it is removed while the held one stays:
    # what keeps the held one is the in-use check, not its content.
    repo = _repo(tmp_path, monkeypatch)
    held = _orphan(repo, "bridge-held")
    (held / "empty").mkdir()
    idle = _orphan(repo, "bridge-idle")
    (idle / "empty").mkdir()

    holder = subprocess.Popen(["sleep", "300"], cwd=held)
    try:
        done = _prune(repo)
    finally:
        holder.kill()
        holder.wait()

    # The held orphan is a removal that did not happen, so the run exits 1 and names it.
    assert done.returncode == 1, done.stderr
    assert f"could not remove {held}: in use" in done.stderr
    assert not idle.exists(), done.stdout
    assert held.is_dir(), done.stdout


def test_prune_brief_removes_an_empty_orphan_and_keeps_one_with_unstored_content(
    tmp_path, monkeypatch
):
    # `--prune --brief` is the weekly cron's invocation, so it must sweep orphans as `--prune` does.
    repo = _repo(tmp_path, monkeypatch)
    empty = _orphan(repo, "bridge-empty")
    (empty / "docs").mkdir()
    work = _orphan(repo, "bridge-work")
    (work / "fresh-content.txt").write_text("work git never stored\n")

    done = run(
        [sys.executable, str(PRUNER), "--prune", "--brief"],
        cwd=repo,
        env=scrubbed_env(),
    )

    assert done.returncode == 0, done.stderr
    assert not empty.exists(), done.stdout
    assert work.is_dir()
    assert f"kept orphan {work}: 1 file(s)" in done.stdout, done.stdout


def test_prune_keeps_an_orphan_whose_blob_is_stored_but_unreachable(
    tmp_path, monkeypatch
):
    # A staged-then-abandoned file leaves a loose blob no ref reaches, and the gc at the
    # prune's tail deletes it after a day, so the orphan may hold the last copy.
    repo = _repo(tmp_path, monkeypatch)
    orphan = _orphan(repo, "bridge-staged")
    staged = orphan / "staged-only.txt"
    staged.write_text("staged in a worktree, never committed\n")
    git(repo, "hash-object", "-w", str(staged))

    done = _prune(repo)

    assert done.returncode == 0, done.stderr
    assert orphan.is_dir()
    assert "staged-only.txt" in done.stdout, done.stdout


def test_report_without_prune_names_a_removable_orphan_and_deletes_nothing(
    tmp_path, monkeypatch
):
    # The report is a dry run: it must say the orphan is removable and leave it in place.
    repo = _repo(tmp_path, monkeypatch)
    orphan = _orphan(repo, "bridge-empty")
    (orphan / "docs").mkdir()

    done = run([sys.executable, str(PRUNER)], cwd=repo, env=scrubbed_env())

    assert done.returncode == 0, done.stderr
    assert orphan.is_dir(), done.stdout
    assert "holds nothing git lacks" in done.stdout, done.stdout
    assert "1 orphan dir(s)" in done.stdout, done.stdout
