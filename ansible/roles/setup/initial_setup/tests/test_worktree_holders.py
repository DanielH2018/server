"""The root-run worktree holder scan, against a fake /proc and a real unix socket.

A fake entry's `cwd` is a symlink, which `os.readlink` reads the way it reads the kernel's.
A `socket.socketpair()` stands in for the connection systemd hands the service: its
`SO_PEERCRED` names this test's own uid, so the root map names that uid's user.

Run: uv run pytest ansible/roles/setup/initial_setup/tests/test_worktree_holders.py
"""

import ast
import json
import os
import pwd
import socket
import threading
from pathlib import Path

import pytest
import worktree_holders
from lib.worktrees import (
    WORKTREE_HOLDER_ROOTS,
    escape_holder,
    privileged_holders,
    read_answer,
)
from worktree_holders import answer, escape, main, render, scan, serve

# Ubuntu 24.04's /usr/bin/python3, which the shebang names. Root must not run an interpreter
# a non-root user can write, so the uv-managed host Python is not an option.
DISTRO_PYTHON = (3, 12)
ME = pwd.getpwuid(os.getuid()).pw_name


def _entry(proc: Path, pid: int, cwd: Path | None = None, environ: bytes | None = None):
    entry = proc / str(pid)
    entry.mkdir(parents=True)
    if cwd is not None:
        (entry / "cwd").symlink_to(cwd)
    if environ is not None:
        (entry / "environ").write_bytes(environ)
    return entry


def _mapped(tmp_path: Path, mapping) -> Path:
    roots = tmp_path / "worktree-holders.json"
    roots.write_text(json.dumps(mapping))
    return roots


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


def _caller_reads(tree: Path, text: str):
    """What the caller makes of the service sending `text`, for `tree`."""
    return read_answer(text.encode("ascii"), tree, "worktree-holders.sock")


# Each a directory name a caller reading a line at a time would misread: a bare newline makes
# an unparseable line, a newline and tabs forge a holder line for tree `b`, U+2028 is a line
# break to `str.splitlines()` though it is not `\n`, and a tab forges a field.
LINE_BREAKERS = (
    "a\nnot a holder line",
    "a\n4242\tcwd\t/b",
    "a\u2028x",
    "a\tcwd",
    "a\x85",
)


@pytest.mark.parametrize("name", LINE_BREAKERS, ids=repr)
def test_a_cwd_with_a_line_break_is_reported_on_one_line_and_holds_only_its_tree_is_clean(
    tmp_path, name
):
    # #4294: the process holds its own tree and nothing else. Before the escape, the caller
    # read an unparseable line and refused every removal while the process lived.
    root = tmp_path / "worktrees"
    held, unrelated = root / name, root / "b"
    held.mkdir(parents=True)
    unrelated.mkdir()
    _entry(tmp_path / "proc", 7, cwd=held, environ=b"")
    text = f"ok\n{render(scan(root, proc=tmp_path / 'proc'))}end\n"

    assert len(text.split("\n")) == 4
    assert _caller_reads(held, text) == [(7, f"cwd {escape(str(held))}")]
    assert _caller_reads(unrelated, text) == []


def test_a_cwd_printed_without_the_escape_breaks_the_caller_is_flagged(tmp_path):
    # The red half: the same values printed as-is refuse the unrelated tree or forge a holder.
    root = tmp_path / "worktrees"
    broken = f"ok\n7\tcwd\t{root}/a\nnot a holder line\nend\n"
    forged = f"ok\n7\tcwd\t{root}/a\n4242\tcwd\t{root}/b\nend\n"

    assert _caller_reads(root / "b", broken)[0][1].startswith(
        "worktree-holders.sock answered an unparseable line"
    )
    assert _caller_reads(root / "b", forged) == [(4242, f"cwd {root}/b")]


def test_a_project_dir_with_a_line_break_holds_only_its_tree_is_clean(tmp_path):
    # #4272 dropped such a value. Escaped, it is reported, and holds only the tree it names.
    root = tmp_path / "worktrees"
    (root / "a").mkdir(parents=True)
    poisoned = f"{root / 'a'}/x\n4242\tcwd\t{root / 'b'}"
    _entry(
        tmp_path / "proc",
        7,
        cwd=tmp_path,
        environ=f"CLAUDE_PROJECT_DIR={poisoned}\0".encode(),
    )
    text = f"ok\n{render(scan(root, proc=tmp_path / 'proc'))}end\n"

    assert [pid for pid, _ in _caller_reads(root / "a", text)] == [7]
    assert _caller_reads(root / "b", text) == []


def test_the_helper_and_the_callers_escape_one_way_is_clean():
    # The shell caller compares escaped forms, so both sides must escape identically.
    for value in (*LINE_BREAKERS, "/plain/path", "caf\u00e9", "\udcff", "back\\slash"):
        assert escape(value) == escape_holder(value)
        assert escape(value).isascii() and escape(value).isprintable()


def test_main_answers_under_the_callers_mapped_root_is_flagged(tmp_path):
    # #4021: the root comes from the map, keyed by the uid the kernel names, so the agent
    # user's removal in its own clone sees the operator's process there.
    root = tmp_path / "clone" / ".claude" / "worktrees"
    (root / "a").mkdir(parents=True)
    _entry(tmp_path / "proc", 7, cwd=root / "a", environ=b"")
    service, caller = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    with caller:
        code = main(
            ["worktree-holders"],
            roots=_mapped(tmp_path, {ME: str(root)}),
            proc=tmp_path / "proc",
            fd=service.detach(),
        )
        sent = caller.recv(65536).decode()

    assert (code, sent) == (0, f"ok\n7\tcwd\t{root / 'a'}\nend\n")


def test_a_uid_the_map_does_not_root_is_refused_is_flagged(tmp_path):
    # Fail closed: a missing map, no entry for the user, or a relative path each refuse, and
    # the refusal carries no `end`, so a caller cannot read it as "no holders".
    proc = tmp_path / "proc"
    proc.mkdir()

    for roots in (
        tmp_path / "absent.json",
        _mapped(tmp_path, {"someone-else": str(tmp_path)}),
        _mapped(tmp_path, {ME: "relative/worktrees"}),
    ):
        text = answer(os.getuid(), roots=roots, proc=proc)
        assert text.startswith("refused\t") and text.count("\n") == 1, roots
        assert _caller_reads(tmp_path / "a", text)[0][0] == 0, roots


def test_main_refuses_an_argument_or_a_stdin_that_is_not_a_socket_is_flagged(tmp_path):
    # The service takes nothing from the caller. A path argument would let it ask root about
    # any directory, and a pipe on stdin means nothing named the caller.
    read_end, write_end = os.pipe()
    os.close(write_end)
    try:
        assert main(["worktree-holders", "/root"]) == 2
        assert main(["worktree-holders"], fd=read_end) == 2
    finally:
        os.close(read_end)


def test_the_helper_and_the_caller_read_one_root_map_is_clean():
    assert worktree_holders.ROOTS == Path(WORKTREE_HOLDER_ROOTS)


def _serving(sock: Path, roots: Path, proc: Path) -> threading.Thread:
    """A listener at `sock` that answers one connection the way the service does."""
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(sock))
    listener.listen(1)

    def one():
        with listener:
            conn, _ = listener.accept()
            with conn:
                serve(conn, roots, proc)

    thread = threading.Thread(target=one, daemon=True)
    thread.start()
    return thread


def test_the_caller_reads_the_service_over_a_real_socket_is_flagged(tmp_path):
    # The transport end to end: connect, the kernel's SO_PEERCRED, the framed answer, the parse.
    root = tmp_path / "worktrees"
    (root / "a\u2028b").mkdir(parents=True)
    _entry(tmp_path / "proc", 7, cwd=root / "a\u2028b", environ=b"")
    sock = tmp_path / "worktree-holders.sock"
    thread = _serving(sock, _mapped(tmp_path, {ME: str(root)}), tmp_path / "proc")

    found = privileged_holders(root / "a\u2028b", sock=str(sock), root=root)
    thread.join(timeout=10)

    assert found == [(7, f"cwd {escape(str(root / 'a\u2028b'))}")]


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
