"""Tests for `lib.worktrees.privileged_holders` and `holder_root`: asking the root helper.

The helper itself, `/usr/local/libexec/worktree-holders`, is tested in
`ansible/roles/setup/initial_setup/tests/test_worktree_holders.py`, which also drives this
caller over a real socket. These tests cover when the caller asks, which root it asks about,
how a failed connection is read, and how it reads the answer (#4170, #4021, #4297).

Run: uv run pytest scripts/lib/tests/test_worktree_privileged_holders.py
"""

import errno
import json
import os
import pwd
import socket
from pathlib import Path

from _worktree_proc import _unreadable_proc
from lib.worktrees import holder_root, privileged_holders, processes_using

SOCK = "/run/worktree-holders.sock"


def _answering(raw: bytes):
    """A stand-in for `ask_holders` that returns `raw` for the socket the caller names."""

    def ask(sock):
        assert sock == SOCK
        return raw

    return ask


def _failing(err: int):
    def ask(sock):
        raise OSError(err, os.strerror(err), sock)

    return ask


def _ask(tmp_path: Path, ask):
    root = tmp_path / "worktrees"
    return privileged_holders(root / "a", sock=SOCK, root=root, ask=ask)


def test_the_helper_sees_another_uid_outside_this_slice_is_flagged(tmp_path):
    # #4170: `systemd-run --uid=claude --working-directory=<tree>` lands in claude's slice,
    # where the unprivileged scan cannot look. The helper reads it as root.
    root = tmp_path / "worktrees"
    tree, sibling = root / "a", root / "b"
    raw = f"ok\n4242\tcwd\t{tree}/sub\n4243\tcwd\t{sibling}\nend\n".encode()

    found = privileged_holders(tree, sock=SOCK, root=root, ask=_answering(raw))

    assert found == [(4242, f"cwd {tree}/sub")]


def test_an_answer_with_its_framing_and_no_holder_leaves_the_tree_free_is_clean(
    tmp_path,
):
    assert _ask(tmp_path, _answering(b"ok\nend\n")) == []


def test_a_refused_garbled_or_truncated_answer_refuses_removal_is_flagged(tmp_path):
    # A socket carries no exit status. An instance that died mid-scan sends a prefix of a good
    # answer, or nothing, and neither may read as "no holder".
    for raw in (
        b"refused\t/etc/worktree-holders.json maps no worktree root for uid 1001\n",
        b"",
        b"ok\n",
        b"ok\n4242\tcwd\t/x\n",
        b"4242\tcwd\t/x\nend\n",
        b"ok\n4242 cwd /x\nend\n",
        b"ok\nend\nok\n",
        b"ok\n4242\tunreadable\tcwd: EPERM\nend\n",
        "ok\n4242\tcwd\t/café\nend\n".encode(),
        b"ok\n4242\tcwd\t/x\\x00y\nend\n",
    ):
        found = _ask(tmp_path, _answering(raw))
        assert found and found[0][0] in (0, 4242), raw


def test_a_refused_connection_or_timeout_refuses_removal_is_flagged(tmp_path):
    # The socket is there, so the scan applies: a refused connection is a stopped service
    # whose file stayed behind, and a timeout is a hung instance.
    for err in (errno.ECONNREFUSED, errno.ETIMEDOUT):
        found = _ask(tmp_path, _failing(err))
        assert found and found[0][0] == 0, err


def test_no_socket_a_group_that_shuts_this_uid_out_or_a_tree_outside_its_root_falls_back_is_clean(
    tmp_path, capsys
):
    # A host without the apply, a session older than its user's group grant, and a tree the
    # helper never reports on: each must scan /proc itself rather than read "no holder", and
    # the first two say so on stderr.
    root = tmp_path / "worktrees"

    assert _ask(tmp_path, _failing(errno.ENOENT)) is None
    assert _ask(tmp_path, _failing(errno.EACCES)) is None
    assert "cannot connect to" in capsys.readouterr().err
    assert (
        privileged_holders(
            tmp_path / "elsewhere", sock=SOCK, root=root, ask=_answering(b"")
        )
        is None
    )

    me = os.getuid()
    proc = _unreadable_proc(
        tmp_path / "proc", 4242, me + 1, f"/user.slice/user-{me}.slice/session-7.scope"
    )
    found = processes_using(str(tmp_path), proc=proc, privileged=lambda _: None)
    assert [pid for pid, _ in found] == [4242]


def test_the_caller_asks_about_the_root_the_helper_maps_it_to_is_flagged(tmp_path):
    # #4021: the agent user's root is its own clone, /var/lib/claude/server, which the root map
    # names. Asking only under ~/server/.claude/worktrees could disagree with the root the
    # helper scans, and then an empty answer would read as "no holder".
    clone = tmp_path / "clone" / ".claude" / "worktrees"
    roots = tmp_path / "worktree-holders.json"
    roots.write_text(json.dumps({pwd.getpwuid(os.getuid()).pw_name: str(clone)}))

    assert holder_root(str(roots)) == clone


def test_with_no_map_entry_the_caller_falls_back_to_its_home_checkout_is_clean(
    tmp_path,
):
    # No map, no entry for this user, or a relative path: the helper is not installed for
    # this user, so the root only decides that the slice-rule fallback runs.
    home = Path(pwd.getpwuid(os.getuid()).pw_dir) / "server" / ".claude" / "worktrees"
    other = tmp_path / "other.json"
    other.write_text(json.dumps({"someone-else": "/elsewhere"}))
    relative = tmp_path / "relative.json"
    relative.write_text(json.dumps({pwd.getpwuid(os.getuid()).pw_name: "rel/wt"}))

    for roots in (tmp_path / "absent.json", other, relative):
        assert holder_root(str(roots)) == home, roots


def test_processes_using_takes_the_helpers_answer_over_the_proc_scan_is_flagged(
    tmp_path,
):
    answer = [(4242, "cwd /w/a")]

    assert (
        processes_using(
            str(tmp_path), proc=tmp_path / "no-proc", privileged=lambda _: answer
        )
        == answer
    )


def test_a_real_socket_this_uid_may_not_write_falls_back_is_clean(tmp_path, capsys):
    # The group gate through a real connect: a socket whose mode shuts this uid out raises
    # EACCES, which reads as "not granted yet", not as a failed scan.
    root = tmp_path / "worktrees"
    sock = tmp_path / "worktree-holders.sock"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as bound:
        bound.bind(str(sock))
        bound.listen(1)
        sock.chmod(0o000)
        found = privileged_holders(root / "a", sock=str(sock), root=root)

    assert found is None
    assert "Permission denied" in capsys.readouterr().err
