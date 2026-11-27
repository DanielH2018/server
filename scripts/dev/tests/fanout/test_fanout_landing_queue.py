"""The landing-queue gate: `launch` holds a server batch while too many landings are in flight.

Run: uv run pytest scripts/dev/tests/fanout/test_fanout_landing_queue.py
"""

import socket
import urllib.error

from dev.fanout_lib.launch_gates import MAX_LANDINGS_IN_FLIGHT
from dev.fanout_lib.place import main
from dev.findings_lib.issue_model import Issue, IssueState
from _fanout_fakes import HOST_KEY, fake_tools, ok

HEADROOM = f"1\n12884901888\n1\n12884901888\n0\n{HOST_KEY}\n"
ISSUE = Issue(1, "one", IssueState.OPEN, body="body", labels=frozenset({"claude"}))


def _launch(tmp_path, landings):
    tools, run = fake_tools(
        answers={"daniel-server": ok(HEADROOM)}, issues=[ISSUE], landings=landings
    )
    argv = ["launch", "--host", "daniel-server", "--batch", "1"]
    return main([*argv, "--manifest-root", str(tmp_path)], tools), run


def test_a_queue_at_the_limit_launches(tmp_path):
    code, run = _launch(tmp_path, MAX_LANDINGS_IN_FLIGHT)
    assert code == 0
    assert any("systemd-run" in command for _, command, _ in run.calls)


def test_a_queue_over_the_limit_refuses_naming_the_wait(tmp_path, capsys):
    code, run = _launch(tmp_path, MAX_LANDINGS_IN_FLIGHT + 1)
    assert code == 3
    err = capsys.readouterr().err
    assert f"{MAX_LANDINGS_IN_FLIGHT + 1} landings are in flight" in err
    assert "`cc-wait fanout-headroom`" in err
    assert run.host_calls == []


def test_a_depth_read_that_fails_holds_nothing(tmp_path, capsys):
    code, _ = _launch(tmp_path, urllib.error.URLError("connection refused"))
    assert code == 0
    assert "could not read how many landings are in flight" in capsys.readouterr().err


def test_a_dotfiles_launch_ignores_the_server_landing_queue(tmp_path):
    here = socket.gethostname()
    issue = Issue(
        763, "dotfiles", IssueState.OPEN, body="b", labels=frozenset({"claude"})
    )
    tools, _ = fake_tools(answers={here: ok(HEADROOM)}, issues=[issue], landings=99)
    argv = ["launch", "--repo", "DanielH2018/dotfiles", "--batch", "763"]
    assert main([*argv, "--manifest-root", str(tmp_path)], tools) == 0
