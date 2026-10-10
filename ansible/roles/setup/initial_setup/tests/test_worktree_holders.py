"""The root-run worktree holder scan, against a fake /proc.

A fake entry's `cwd` is a symlink, which `os.readlink` reads the way it reads the kernel's.

Run: uv run pytest ansible/roles/setup/initial_setup/tests/test_worktree_holders.py
"""

import ast
import os
from pathlib import Path

import pytest
import worktree_holders
from worktree_holders import main, scan

# Ubuntu 24.04's /usr/bin/python3, which the shebang names. Root must not run an interpreter
# a non-root user can write, so the uv-managed host Python is not an option.
DISTRO_PYTHON = (3, 12)


def _entry(proc: Path, pid: int, cwd: Path | None = None, environ: bytes | None = None):
    entry = proc / str(pid)
    entry.mkdir(parents=True)
    if cwd is not None:
        (entry / "cwd").symlink_to(cwd)
    if environ is not None:
        (entry / "environ").write_bytes(environ)
    return entry


def test_a_process_with_its_cwd_in_a_worktree_is_flagged(tmp_path):
    root = tmp_path / "worktrees"
    (root / "a").mkdir(parents=True)
    _entry(tmp_path / "proc", 7, cwd=root / "a", environ=b"")
    _entry(tmp_path / "proc", 8, cwd=tmp_path, environ=b"")

    assert scan(root, proc=tmp_path / "proc") == [(7, "cwd", str(root / "a"))]


def test_only_the_project_dir_value_leaves_environ_is_flagged(tmp_path):
    # A Claude process carries tokens in its environ, and the sweep journals this output.
    root = tmp_path / "worktrees"
    (root / "a").mkdir(parents=True)
    environ = f"TOKEN=secret\0CLAUDE_PROJECT_DIR={root / 'a'}\0".encode()
    _entry(tmp_path / "proc", 7, cwd=tmp_path, environ=environ)

    found = scan(root, proc=tmp_path / "proc")

    assert found == [(7, "CLAUDE_PROJECT_DIR", str(root / "a"))]
    assert "secret" not in repr(found)


def test_a_process_root_cannot_read_is_reported_unreadable_is_flagged(tmp_path):
    # readlink on a regular file fails with EINVAL, which stands in for any refusal that is
    # not "the process is gone".
    root = tmp_path / "worktrees"
    root.mkdir()
    entry = _entry(tmp_path / "proc", 7)
    (entry / "cwd").write_text("")

    assert [(pid, kind) for pid, kind, _ in scan(root, proc=tmp_path / "proc")] == [
        (7, "unreadable")
    ]


def test_a_process_that_exited_mid_scan_is_skipped_is_clean(tmp_path):
    root = tmp_path / "worktrees"
    root.mkdir()
    _entry(tmp_path / "proc", 7)

    assert scan(root, proc=tmp_path / "proc") == []


def test_main_refuses_an_argument_or_a_missing_sudo_uid_is_flagged(monkeypatch):
    # The sudoers rule allows no arguments; a path argument would let the caller ask root
    # about any directory.
    monkeypatch.setenv("SUDO_UID", str(os.getuid()))
    assert main(["worktree-holders", "/root"]) == 2
    monkeypatch.delenv("SUDO_UID")
    assert main(["worktree-holders"]) == 2


def test_the_helper_parses_on_the_distro_interpreter_is_clean():
    # ruff formats for the repo's 3.14 and rewrites `except (A, B):` into a form 3.12 rejects.
    ast.parse(
        Path(worktree_holders.__file__).read_text(), feature_version=DISTRO_PYTHON
    )


def test_syntax_newer_than_the_distro_interpreter_is_flagged():
    with pytest.raises(SyntaxError):
        ast.parse(
            "try:\n    pass\nexcept A, B:\n    pass\n", feature_version=DISTRO_PYTHON
        )
