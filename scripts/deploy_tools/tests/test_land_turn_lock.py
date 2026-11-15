"""The landing turn's lock and line, on the turn host and over ssh from another host.

Run: uv run pytest scripts/deploy_tools/tests/test_land_turn_lock.py
"""

import os
import subprocess
from pathlib import Path

import pytest

from deploy_tools.land_lib import line, turn
from lib.proc_testing import fake_bin, path_with

HEAD = "a" * 40


def test_the_lock_admits_one_holder_until_it_gives_the_turn_up(tmp_path):
    lock = tmp_path / "turn.lock"
    give_up = turn.try_take(lock)
    assert give_up is not None
    assert turn.try_take(lock) is None
    give_up()
    again = turn.try_take(lock)
    assert again is not None
    again()


def test_the_lock_file_is_readable_by_every_user_whatever_the_umask(tmp_path):
    lock = tmp_path / "turn.lock"
    old = os.umask(0o077)
    try:
        give_up = turn.try_take(lock)
    finally:
        os.umask(old)
    assert give_up is not None
    give_up()
    assert lock.stat().st_mode & 0o777 == 0o644


def test_a_landing_on_another_host_holds_the_same_lock_over_ssh(tmp_path, ssh_here):
    lock = tmp_path / "turn.lock"
    give_up = turn.try_take_over_ssh(lock)
    assert give_up is not None
    # The lock a landing on the turn host takes is the one the ssh session holds.
    assert turn.try_take(lock) is None
    give_up()
    here = turn.try_take(lock)
    assert here is not None
    assert turn.try_take_over_ssh(lock) is None
    here()


def test_a_turn_host_that_does_not_answer_raises_oserror(tmp_path, monkeypatch):
    stubs = fake_bin(
        tmp_path / "bin",
        ssh="#!/usr/bin/env bash\necho 'Warning: added a key' >&2\necho 'ssh: no route' >&2\nexit 255\n",
    )
    monkeypatch.setenv("PATH", path_with(stubs))
    with pytest.raises(OSError, match="ssh daniel-box: ssh: no route"):
        turn.try_take_over_ssh(tmp_path / "turn.lock")


def test_a_landing_off_the_turn_host_reaches_the_line_over_ssh(tmp_path, monkeypatch):
    seen = tmp_path / "ssh-args"
    stubs = fake_bin(
        tmp_path / "bin",
        ssh=f"#!/usr/bin/env bash\nprintf '%s\\n' \"$@\" > {seen}\n",
    )
    monkeypatch.setenv("PATH", path_with(stubs))
    turn._line("daniel-server")("leave", "7")
    args = seen.read_text().splitlines()
    assert turn.TURN_HOST in args
    assert args[-1].endswith(f"{line.DIR} leave 7")


@pytest.mark.parametrize(
    "source", [turn._HOLD, line._REMOTE], ids=["the-turn", "the-line"]
)
def test_what_runs_on_the_turn_host_compiles_under_a_hosts_own_python(source):
    """The repo's Python is newer than a host's, and the formatter writes its syntax."""
    system_python = Path("/usr/bin/python3")
    assert system_python.exists(), "no system python3 to stand for the turn host's"
    compiled = subprocess.run(
        [
            system_python,
            "-c",
            "import sys; compile(sys.argv[1], 'remote', 'exec')",
            source,
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert compiled.returncode == 0, compiled.stderr


@pytest.mark.parametrize("hostname", [turn.TURN_HOST, f"{turn.TURN_HOST}.lan"])
def test_a_landing_on_the_turn_host_keeps_the_line_itself(
    hostname, tmp_path, monkeypatch
):
    stubs = fake_bin(tmp_path / "bin", ssh="#!/usr/bin/env bash\nexit 255\n")
    monkeypatch.setenv("PATH", path_with(stubs))
    turn._line(hostname, tmp_path / "line")("join", "7", HEAD)
    assert (tmp_path / "line" / "7").read_text().split() == [HEAD]


@pytest.mark.parametrize("hostname", [turn.TURN_HOST, f"{turn.TURN_HOST}.lan"])
def test_a_landing_on_the_turn_host_locks_the_file_itself(hostname):
    # By value, not by effect: taking the turn here would lock the host's real file.
    assert turn._taker(hostname) is turn.try_take


def test_a_moved_turn_dir_keeps_the_lock_and_the_line_on_this_host(
    tmp_path, monkeypatch
):
    stubs = fake_bin(tmp_path / "bin", ssh="#!/usr/bin/env bash\nexit 255\n")
    monkeypatch.setenv("PATH", path_with(stubs))
    monkeypatch.setenv(turn.TURN_DIR_ENV, str(tmp_path / "turn"))
    (tmp_path / "turn").mkdir()
    give_up = turn._taker("daniel-server")()
    assert give_up is not None
    assert turn.try_take(tmp_path / "turn" / turn.LOCK.name) is None
    give_up()
    turn._line("daniel-server")("join", "7", HEAD)
    assert (tmp_path / "turn" / line.DIR.name / "7").read_text().split() == [HEAD]
