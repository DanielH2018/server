"""The root-run worktree holder scan, against a fake /proc.

A fake entry's `cwd` is a symlink, which `os.readlink` reads the way it reads the kernel's.

Run: uv run pytest ansible/roles/setup/initial_setup/tests/test_worktree_holders.py
"""

import ast
import json
import os
import pwd
import subprocess
import sys
from pathlib import Path

import pytest
import worktree_holders
from lib.worktrees import WORKTREE_HOLDER_ROOTS, privileged_holders
from worktree_holders import main, render, scan

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


def _unrelated_and_forged(tmp_path: Path):
    """A root with two trees, and a process outside both whose project dir is poisoned.

    Each value is one the caller would misread a line at a time: a bare newline makes an
    unparseable line, a newline and tabs forge a holder line for tree `b`, and U+2028 is a
    line break to `str.splitlines()` though it is not `\\n`.
    """
    root = tmp_path / "worktrees"
    (root / "a").mkdir(parents=True)
    (root / "b").mkdir()
    poisoned = (
        f"{root / 'a'}\nnot a holder line",
        f"{root / 'a'}\n4242\tcwd\t{root / 'b'}",
        f"{root / 'a'} not a holder line",
        f"{root / 'a'}\tcwd",
    )
    proc = tmp_path / "proc"
    for pid, value in enumerate(poisoned, start=7):
        _entry(
            proc, pid, cwd=tmp_path, environ=f"CLAUDE_PROJECT_DIR={value}\0".encode()
        )
    return root, proc


def _caller_reads(root: Path, tree: Path, output: str):
    """What `privileged_holders` makes of the helper printing `output`, for `tree`."""

    def run(argv, **_):
        return subprocess.CompletedProcess(argv, 0, output, "")

    return privileged_holders(tree, helper=sys.executable, root=root, run=run)


def test_a_project_dir_that_is_not_one_printable_line_is_dropped_is_clean(tmp_path):
    # #4272: one process of another uid must not stop every removal, nor keep a tree it does
    # not hold.
    root, proc = _unrelated_and_forged(tmp_path)
    output = render(scan(root, proc=proc))

    assert output == ""
    assert _caller_reads(root, root / "a", output) == []
    assert _caller_reads(root, root / "b", output) == []


def test_a_poisoned_project_dir_printed_as_is_breaks_the_caller_is_flagged(tmp_path):
    # The red half: without the filter, the same values refuse the unrelated tree and forge a
    # holder for `b`.
    root, _ = _unrelated_and_forged(tmp_path)
    unfiltered = render([(7, "CLAUDE_PROJECT_DIR", f"{root / 'a'}\nnot a holder line")])
    forged = render(
        [(8, "CLAUDE_PROJECT_DIR", f"{root / 'a'}\n4242\tcwd\t{root / 'b'}")]
    )

    assert _caller_reads(root, root / "b", unfiltered)[0][1].startswith(
        f"{sys.executable} printed an unparseable line"
    )
    assert _caller_reads(root, root / "b", forged) == [(4242, f"cwd {root / 'b'}")]


def test_the_cwd_of_a_process_with_a_poisoned_project_dir_is_still_reported_is_flagged(
    tmp_path,
):
    root = tmp_path / "worktrees"
    (root / "a").mkdir(parents=True)
    environ = f"CLAUDE_PROJECT_DIR={root / 'a'}\n\0".encode()
    _entry(tmp_path / "proc", 7, cwd=root / "a", environ=environ)

    assert scan(root, proc=tmp_path / "proc") == [(7, "cwd", str(root / "a"))]


def _mapped(tmp_path: Path, mapping) -> Path:
    roots = tmp_path / "worktree-holders.json"
    roots.write_text(json.dumps(mapping))
    return roots


def test_main_reports_under_the_sudo_users_mapped_root_is_flagged(
    tmp_path, monkeypatch, capsys
):
    # #4021: the root comes from the map, not from the caller's home, so the agent user's
    # removal in its own clone sees the operator's process there.
    me = pwd.getpwuid(os.getuid()).pw_name
    root = tmp_path / "clone" / ".claude" / "worktrees"
    (root / "a").mkdir(parents=True)
    _entry(tmp_path / "proc", 7, cwd=root / "a", environ=b"")
    monkeypatch.setenv("SUDO_UID", str(os.getuid()))

    code = main(
        ["worktree-holders"],
        roots=_mapped(tmp_path, {me: str(root)}),
        proc=tmp_path / "proc",
    )

    assert (code, capsys.readouterr().out) == (0, f"7\tcwd\t{root / 'a'}\n")


def test_main_refuses_a_user_the_map_does_not_root_is_flagged(tmp_path, monkeypatch):
    # Fail closed: a missing map, no entry for the user, or a relative path each exit 2, which
    # the caller reads as a refusal.
    me = pwd.getpwuid(os.getuid()).pw_name
    monkeypatch.setenv("SUDO_UID", str(os.getuid()))
    proc = tmp_path / "proc"
    proc.mkdir()

    for roots in (
        tmp_path / "absent.json",
        _mapped(tmp_path, {"someone-else": str(tmp_path)}),
        _mapped(tmp_path, {me: "relative/worktrees"}),
    ):
        assert main(["worktree-holders"], roots=roots, proc=proc) == 2, roots


def test_the_helper_and_the_caller_read_one_root_map_is_clean():
    assert worktree_holders.ROOTS == Path(WORKTREE_HOLDER_ROOTS)


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
