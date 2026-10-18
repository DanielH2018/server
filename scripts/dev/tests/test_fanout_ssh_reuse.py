"""ssh connection reuse: one connection per host, a private socket directory, and the lifted budget.

Run: uv run pytest scripts/dev/tests/test_fanout_ssh_reuse.py
"""

import os

from fanout_lib.brief import Issue
from fanout_lib.place import main
from fanout_lib.transport import SSH_OPTS, _argv, control_dir
from _fanout_fakes import HOST_KEY, fake_tools, ok

HEADROOM = f"1\n12884901888\n1\n12884901888\n0\n{HOST_KEY}\n"
ISSUES = [Issue(n, f"issue {n}", f"body {n}", ("claude",)) for n in (1, 2, 3, 4)]


def test_a_control_directory_adds_the_reuse_options_to_a_remote_call():
    argv = _argv("daniel-server", "echo hi", "daniel-box", "/run/user/1000/fanout-ssh")
    assert argv[: 1 + len(SSH_OPTS)] == ["ssh", *SSH_OPTS]
    assert "ControlMaster=auto" in argv
    assert "ControlPath=/run/user/1000/fanout-ssh/%C" in argv
    assert argv[-2:] == ["daniel-server", "echo hi"]


def test_no_control_directory_leaves_the_call_as_it_was():
    assert _argv("daniel-server", "echo hi", "daniel-box", None) == [
        "ssh",
        *SSH_OPTS,
        "daniel-server",
        "echo hi",
    ]
    local = _argv("daniel-box", "echo hi", "daniel-box", "/run/user/1000/fanout-ssh")
    assert local == ["bash", "-c", "echo hi"]


def test_the_socket_directory_is_created_private(tmp_path):
    path = control_dir({"XDG_RUNTIME_DIR": str(tmp_path)}, os.getuid())
    assert path == str(tmp_path / "fanout-ssh")
    assert os.stat(path).st_mode & 0o777 == 0o700


def test_a_socket_directory_others_can_enter_is_refused(tmp_path):
    """The red half: a socket there is a shell on the remote host, so a shared one is unsafe."""
    shared = tmp_path / "fanout-ssh"
    shared.mkdir()
    shared.chmod(0o755)
    assert control_dir({"XDG_RUNTIME_DIR": str(tmp_path)}, os.getuid()) is None
    assert control_dir({"XDG_RUNTIME_DIR": str(tmp_path / "gone")}, os.getuid()) is None


def test_a_multiplexed_run_places_four_batches_on_one_remote_host(tmp_path):
    """The twin of `test_four_batches_pinned_to_one_host_is_refused_before_any_launch`."""
    tools, run = fake_tools(
        answers={"daniel-server": ok(HEADROOM)}, issues=ISSUES, multiplexed=True
    )
    argv = ["launch", "--host", "daniel-server", "--manifest-root", str(tmp_path)]
    for n in (1, 2, 3, 4):
        argv += ["--batch", str(n)]
    assert main(argv, tools) == 0
    assert sum("worktree add" in c[1] for c in run.host_calls) == 4
