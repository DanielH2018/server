"""The landing turn's lock and line, on the turn host and over ssh from another host.

Run: uv run pytest scripts/deploy_tools/tests/land_merge/test_land_turn_lock.py
"""

import json
import os
import pwd
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest

from _land_fakes import Fakes
from deploy_tools import land
from deploy_tools.land_lib import detach, fresh, line, pr_lock, turn
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
    give_up = turn.try_take_over_ssh([lock])
    assert give_up is not None
    # The lock a landing on the turn host takes is the one the ssh session holds.
    assert turn.try_take(lock) is None
    give_up()
    here = turn.try_take(lock)
    assert here is not None
    assert turn.try_take_over_ssh([lock]) is None
    here()


def test_a_hold_whose_client_stops_writing_frees_the_lock_with_its_stdin_open(
    tmp_path,
):
    """A client host that loses power sends no close, so its session's stdin never ends."""
    lock = tmp_path / "turn.lock"
    beat = 0.2
    hold = subprocess.Popen(
        [sys.executable, "-c", turn._HOLD, str(beat), "1", str(lock)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert hold.stdout and hold.stdout.readline().strip() == "held"
        assert turn.try_take(lock) is None
        started = time.monotonic()
        assert hold.wait(timeout=30) == 0
        assert time.monotonic() - started < 3 * beat + 2
        free = turn.try_take(lock)
        assert free is not None
        free()
    finally:
        hold.kill()
        hold.communicate()


def test_a_lock_held_over_ssh_outlasts_many_heartbeats(tmp_path, ssh_here):
    lock = tmp_path / "turn.lock"
    give_up = turn.try_take_over_ssh([lock], beat=0.1)
    assert give_up is not None
    time.sleep(1.0)
    assert turn.try_take(lock) is None
    give_up()
    free = turn.try_take(lock)
    assert free is not None
    free()


def test_a_lock_whose_ssh_session_died_is_given_up_without_raising(
    tmp_path, monkeypatch
):
    """A session ends unasked when the network drops or the host frees a silent lock."""
    stubs = fake_bin(tmp_path / "bin", ssh="#!/usr/bin/env bash\necho held\n")
    monkeypatch.setenv("PATH", path_with(stubs))
    give_up = turn.try_take_over_ssh([tmp_path / "turn.lock"], beat=0.05)
    assert give_up is not None
    time.sleep(0.5)
    give_up()


def test_a_turn_host_that_does_not_answer_raises_oserror(tmp_path, monkeypatch):
    stubs = fake_bin(
        tmp_path / "bin",
        ssh="#!/usr/bin/env bash\necho 'Warning: added a key' >&2\necho 'ssh: no route' >&2\nexit 255\n",
    )
    monkeypatch.setenv("PATH", path_with(stubs))
    with pytest.raises(OSError, match="ssh daniel-box: ssh: no route"):
        turn.try_take_over_ssh([tmp_path / "turn.lock"])


def test_a_landing_off_the_turn_host_reaches_the_line_over_ssh(tmp_path, monkeypatch):
    seen = tmp_path / "ssh-args"
    stubs = fake_bin(
        tmp_path / "bin",
        ssh=f"#!/usr/bin/env bash\nprintf '%s\\n' \"$@\" > {seen}\n",
    )
    monkeypatch.setenv("PATH", path_with(stubs))
    monkeypatch.delenv(turn.TURN_DIR_ENV)
    line.at_turn_host("daniel-server")("leave", "7")
    args = seen.read_text().splitlines()
    assert turn.TURN_HOST in args
    assert args[-1].endswith(f"{line.DIR} leave 7")


@pytest.mark.parametrize(
    "source",
    [turn._HOLD, line._REMOTE, pr_lock._HOLD],
    ids=["the-turn", "the-line", "the-pr-lock"],
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
    monkeypatch.delenv(turn.TURN_DIR_ENV)
    line.at_turn_host(hostname, tmp_path / "line")("join", "7", HEAD)
    assert (tmp_path / "line" / "7").read_text().split() == [HEAD]


@pytest.mark.parametrize("hostname", [turn.TURN_HOST, f"{turn.TURN_HOST}.lan"])
def test_a_landing_on_the_turn_host_locks_the_file_itself(hostname, monkeypatch):
    # By value, not by effect: taking the turn here would lock the host's real file.
    monkeypatch.delenv(turn.TURN_DIR_ENV)
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
    line.at_turn_host("daniel-server")("join", "7", HEAD)
    assert (tmp_path / "turn" / line.DIR.name / "7").read_text().split() == [HEAD]


def test_a_turn_given_up_is_free_while_a_handed_copy_of_it_stays_open(tmp_path):
    lock = tmp_path / "turn.lock"
    held = turn.try_take(lock)
    assert held is not None
    copy = os.dup(held.fd)
    held()
    again = turn.try_take(lock)
    assert again is not None
    again()
    os.close(copy)


def test_a_landing_that_hands_its_turn_over_leaves_it_locked(tmp_path, landing):
    lock = tmp_path / "turn.lock"
    ln, _ = landing(Fakes())
    ln.tools = replace(ln.tools, try_turn=lambda: turn.try_take(lock))

    def hand_over_in_turn() -> None:
        with turn.held(ln) as take_turn:
            assert take_turn("")[0]
            ln.hand_over_turn("its code lags master")

    with pytest.raises(turn.HandOver) as raised:
        hand_over_in_turn()
    assert turn.try_take(lock) is None
    os.close(raised.value.fd)
    assert turn.try_take(lock) is not None


def test_a_landing_handed_the_turn_holds_it_at_once_until_it_ends(
    tmp_path, landing, monkeypatch
):
    lock = tmp_path / "turn.lock"
    handed = turn.try_take(lock)
    assert handed is not None
    monkeypatch.setenv(fresh.TURN_FD_ENV, str(os.dup(handed.fd)))
    os.close(handed.fd)
    ln, calls = landing(Fakes())
    with turn.held(ln) as take_turn:
        assert take_turn("") == (True, 0)
        assert turn.try_take(lock) is None
    assert [c for c in calls if c[0] == "try_turn"] == []
    assert fresh.TURN_FD_ENV not in os.environ
    assert turn.try_take(lock) is not None


def test_a_landing_handed_the_turn_never_hands_it_on(tmp_path, landing, monkeypatch):
    handed = turn.try_take(tmp_path / "turn.lock")
    assert handed is not None
    monkeypatch.setenv(fresh.TURN_FD_ENV, str(handed.fd))
    ln, _ = landing(Fakes())
    with turn.held(ln) as take_turn:
        take_turn("")
        assert ln.hand_over_turn("master moved again") is None


def _live_landing(pr: str, hostname: str = "daniel-box", **where) -> pr_lock.Held:
    """The PR lock as another process's landing holds it: this process names no held PR."""
    held = pr_lock.take(pr, hostname, **where)
    assert isinstance(held, pr_lock.Held)
    del os.environ[pr_lock.HELD_ENV]
    return held


def test_a_second_landing_of_a_live_pr_is_refused_naming_the_first(land_run):
    first = _live_landing("999")
    try:
        rc, out, _, calls, logline = land_run(["--arm-merge", "--await-merge"])
    finally:
        first.release()
    result, verdict = out.rstrip().splitlines()[-2:]
    assert rc == 75
    assert result.startswith(
        "LAND-RESULT: verdict=refused cause=landing-live exit=75 pr=999 "
    )
    user = pwd.getpwuid(os.geteuid()).pw_name
    assert f"(host daniel-box, pid {os.getpid()}, user {user}, " in verdict
    # It read and merged nothing, and the live landing writes the PR's annotation.
    assert [c[0] for c in calls if c[0] in ("gh", "gh_json", "logger")] == []
    assert logline == ""


def test_a_landing_of_another_pr_is_not_refused(land_run):
    other = _live_landing("998")
    files = {"files": [{"path": "docs/landing.md"}], "changedFiles": 1}
    try:
        rc, out, *_ = land_run(
            [], Fakes(gh_views={"files,changedFiles": files}, derived=([], "pr"))
        )
    finally:
        other.release()
    assert rc == 0 and "VERDICT: nothing-to-deploy (PR #999" in out


def test_a_landing_run_by_the_landing_that_holds_the_lock_is_not_refused(land_run):
    """A landing that moves to a fresh checkout runs `land.sh` for its own PR as a child."""
    parent = pr_lock.take("999", "daniel-box")
    assert isinstance(parent, pr_lock.Held)
    files = {"files": [{"path": "docs/landing.md"}], "changedFiles": 1}
    try:
        rc, out, *_ = land_run(
            [], Fakes(gh_views={"files,changedFiles": files}, derived=([], "pr"))
        )
    finally:
        parent.release()
    assert rc == 0 and "VERDICT: nothing-to-deploy (PR #999" in out


def test_a_lock_left_by_a_dead_landing_refuses_nothing():
    scripts = Path(pr_lock.__file__).resolve().parents[2]
    dies_holding = (
        "import os\n"
        "from deploy_tools.land_lib import pr_lock\n"
        "assert isinstance(pr_lock.take('7', 'daniel-box'), pr_lock.Held)\n"
        "os._exit(0)\n"
    )
    dead = subprocess.Popen(
        [sys.executable, "-c", dies_holding],
        env={**os.environ, "PYTHONPATH": str(scripts)},
    )
    assert dead.wait(timeout=60) == 0
    left = Path(os.environ[turn.TURN_DIR_ENV]) / pr_lock.lock_name("7")
    assert json.loads(left.read_text())["pid"] == dead.pid

    held = pr_lock.take("7", "daniel-box")
    assert isinstance(held, pr_lock.Held)
    held.release()


def test_a_landing_on_another_host_holds_the_same_pr_lock_over_ssh(
    tmp_path, ssh_here, monkeypatch
):
    monkeypatch.delenv(turn.TURN_DIR_ENV)
    away = _live_landing("7", hostname="daniel-server", directory=tmp_path)
    try:
        # The ssh session writes the record after it answers, so the read waits for it.
        deadline = time.monotonic() + 30
        while not (tmp_path / pr_lock.lock_name("7")).read_text():
            assert time.monotonic() < deadline
            time.sleep(0.05)
        holder = pr_lock.take("7", "daniel-box", directory=tmp_path)
    finally:
        away.release()
    assert isinstance(holder, pr_lock.Holder)
    assert (holder.host, holder.pid) == ("daniel-server", str(os.getpid()))


def test_a_pr_lock_hold_whose_client_stops_writing_frees_the_lock_with_its_stdin_open(
    tmp_path, monkeypatch
):
    """A client host that loses power sends no close, so its session's stdin never ends."""
    monkeypatch.delenv(turn.TURN_DIR_ENV)
    monkeypatch.setenv(pr_lock.HELD_ENV, "")
    lock = tmp_path / pr_lock.lock_name("7")
    beat = 0.2
    hold = subprocess.Popen(
        [sys.executable, "-c", pr_lock._HOLD, str(beat), str(lock)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert hold.stdout and hold.stdout.readline().strip() == "held"
        assert isinstance(pr_lock.take("7", "daniel-box", tmp_path), pr_lock.Holder)
        started = time.monotonic()
        assert hold.wait(timeout=30) == 0
        assert time.monotonic() - started < 3 * beat + 2
        free = pr_lock.take("7", "daniel-box", tmp_path)
        assert isinstance(free, pr_lock.Held)
        free.release()
    finally:
        hold.kill()
        hold.communicate()


def test_a_landing_holds_its_pr_lock_over_ssh_past_three_heartbeats(
    tmp_path, ssh_here, monkeypatch
):
    monkeypatch.delenv(turn.TURN_DIR_ENV)
    beat = 0.2
    lock = _live_landing("7", hostname="daniel-server", directory=tmp_path, beat=beat)
    try:
        time.sleep(10 * beat)
        holder = pr_lock.take("7", "daniel-box", tmp_path)
        assert isinstance(holder, pr_lock.Holder)
        assert holder.pid == str(os.getpid())
    finally:
        lock.release()
    free = pr_lock.take("7", "daniel-box", tmp_path)
    assert isinstance(free, pr_lock.Held)
    free.release()


# The threads are the xdist worker's; `land.sh --detach` forks single-threaded. The ssh
# session passes to the fork, so this process never waits for it to end.
@pytest.mark.filterwarnings(
    "ignore:This process .* is multi-threaded:DeprecationWarning",
    "ignore:subprocess .* is still running:ResourceWarning",
)
def test_a_detached_landing_holds_its_pr_lock_over_ssh_past_three_heartbeats(
    tmp_path, ssh_here, landing, monkeypatch, capsys
):
    monkeypatch.delenv(turn.TURN_DIR_ENV)
    monkeypatch.setenv(pr_lock.HELD_ENV, "")
    # Refused so the fork creates no real scope when this suite runs inside a fan-out unit.
    fake_bin(tmp_path / "bin", busctl="#!/bin/sh\nexit 1\n")
    beat = 0.2
    done = tmp_path / "done"

    def until_done() -> int:
        deadline = time.monotonic() + 60
        while not done.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        return 0

    ln, _ = landing(Fakes())
    opts = replace(ln.opts, pr="7", log_dir=str(tmp_path / "logs"))
    tools = replace(ln.tools, fork=detach.fork)
    lock = pr_lock.take("7", "daniel-server", tmp_path, beat)
    assert isinstance(lock, pr_lock.Held)
    try:
        # The fork closes the descriptor of pytest's capture, which the landing would print to.
        with capsys.disabled():
            assert land._detached(opts, tools, until_done, lock) == 0
        time.sleep(10 * beat)
        holder = pr_lock.take("7", "daniel-box", tmp_path)
        assert isinstance(holder, pr_lock.Holder)
        assert holder.pid == str(detach.recorded_pid(Path(holder.log)))
    finally:
        done.touch()
    deadline = time.monotonic() + 30
    while not isinstance(
        free := pr_lock.take("7", "daniel-box", tmp_path), pr_lock.Held
    ):
        assert time.monotonic() < deadline, "the lock outlived its detached landing"
        time.sleep(0.05)
    free.release()


def test_a_turn_host_that_does_not_answer_lets_the_landing_go_on_without_the_pr_lock(
    tmp_path, monkeypatch, capsys
):
    stubs = fake_bin(
        tmp_path / "bin",
        ssh="#!/usr/bin/env bash\necho 'ssh: no route' >&2\nexit 255\n",
    )
    monkeypatch.setenv("PATH", path_with(stubs))
    monkeypatch.delenv(turn.TURN_DIR_ENV)
    assert pr_lock.take("7", "daniel-server") is None
    assert "going on without it" in capsys.readouterr().err
