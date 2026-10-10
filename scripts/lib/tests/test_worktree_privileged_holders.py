"""Tests for `lib.worktrees.privileged_holders` and `holder_root`: asking the root helper.

The helper itself, `/usr/local/libexec/worktree-holders`, is tested in
`ansible/roles/setup/initial_setup/tests/test_worktree_holders.py`. These tests cover when
the caller asks it, which root it asks about, and how it reads the answer (#4170, #4021).

Run: uv run pytest scripts/lib/tests/test_worktree_privileged_holders.py
"""

import json
import os
import pwd
import subprocess
from pathlib import Path

from _worktree_proc import _unreadable_proc
from lib.proc_testing import write_exec
from lib.worktrees import holder_root, privileged_holders, processes_using


def _helper_answering(tmp_path: Path, returncode: int = 0, stdout: str = ""):
    """An executable stand-in for the root helper, and a runner that returns this answer."""
    helper = write_exec(tmp_path / "worktree-holders", "exit 0\n")

    def run(argv, **_):
        assert argv == ["/usr/bin/sudo", "-n", str(helper)]
        return subprocess.CompletedProcess(argv, returncode, stdout, "sudo: denied")

    return str(helper), run


def test_the_helper_sees_another_uid_outside_this_slice_is_flagged(tmp_path):
    # #4170: `systemd-run --uid=claude --working-directory=<tree>` lands in claude's slice,
    # where the unprivileged scan cannot look. The helper reads it as root.
    root = tmp_path / "worktrees"
    tree, sibling = root / "a", root / "b"
    helper, run = _helper_answering(
        tmp_path, stdout=f"4242\tcwd\t{tree}/sub\n4243\tcwd\t{sibling}\n"
    )

    found = privileged_holders(
        tree, helper=helper, root=root, run=run, status=tmp_path / "status"
    )

    assert found == [(4242, f"cwd {tree}/sub")]


def test_a_helper_failure_or_unreadable_process_refuses_removal_is_flagged(tmp_path):
    root = tmp_path / "worktrees"
    failed, run_failed = _helper_answering(tmp_path, returncode=1)
    garbled, run_garbled = _helper_answering(tmp_path, stdout="4242 cwd /x\n")
    blind, run_blind = _helper_answering(
        tmp_path, stdout="4242\tunreadable\tcwd: EPERM\n"
    )

    for helper, run in (
        (failed, run_failed),
        (garbled, run_garbled),
        (blind, run_blind),
    ):
        assert privileged_holders(
            root / "a", helper=helper, root=root, run=run, status=tmp_path / "status"
        ), helper


def test_no_helper_or_a_tree_outside_its_root_falls_back_to_the_slice_rule_is_clean(
    tmp_path,
):
    # A host without the hand apply, a caller the helper's 0750 mode shuts out, and a tree
    # the helper never reports on: each must scan /proc itself rather than read "no holder".
    root = tmp_path / "worktrees"
    helper, run = _helper_answering(tmp_path, stdout="")
    shut_out = tmp_path / "not-mine"
    shut_out.write_text("")
    shut_out.chmod(0o640)

    assert (
        privileged_holders(root / "a", helper=str(tmp_path / "absent"), root=root)
        is None
    )
    assert privileged_holders(root / "a", helper=str(shut_out), root=root) is None
    assert (
        privileged_holders(tmp_path / "elsewhere", helper=helper, root=root, run=run)
        is None
    )

    me = os.getuid()
    proc = _unreadable_proc(
        tmp_path / "proc", 4242, me + 1, f"/user.slice/user-{me}.slice/session-7.scope"
    )
    found = processes_using(str(tmp_path), proc=proc, privileged=lambda _: None)
    assert [pid for pid, _ in found] == [4242]


def _status(tmp_path: Path, no_new_privs: int) -> Path:
    status = tmp_path / f"status-{no_new_privs}"
    status.write_text(f"Name:\tpython3\nNoNewPrivs:\t{no_new_privs}\n")
    return status


def test_a_caller_with_no_new_privs_falls_back_without_asking_sudo_is_clean(tmp_path):
    # claude-rc.service runs the agent's sessions with NoNewPrivileges=yes, where sudo cannot
    # gain root. Asking would fail and refuse every removal, the failure #4017 reverted.
    root = tmp_path / "worktrees"

    def run(argv, **_):
        raise AssertionError(f"asked sudo: {argv}")

    helper, _ = _helper_answering(tmp_path)
    found = privileged_holders(
        root / "a", helper=helper, root=root, run=run, status=_status(tmp_path, 1)
    )

    assert found is None


def test_a_caller_without_no_new_privs_asks_the_helper_is_flagged(tmp_path):
    root = tmp_path / "worktrees"
    helper, run = _helper_answering(tmp_path, returncode=1)

    assert privileged_holders(
        root / "a", helper=helper, root=root, run=run, status=_status(tmp_path, 0)
    )


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
