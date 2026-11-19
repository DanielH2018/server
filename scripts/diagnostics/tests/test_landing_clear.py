"""The `landing-clear` source cc-wait reads, with both GitHub reads handed in as callables.

Run: uv run pytest scripts/diagnostics/tests/test_landing_clear.py
"""

import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import landing_clear
import pytest
from deploy_tools.land_lib import serial_lock, turn
from lib import proc_testing

RUN_URL = "https://github.com/DanielH2018/server/actions/runs/123"


def _run(status="completed", conclusion="success"):
    return {
        "status": status,
        "conclusion": conclusion,
        "head_sha": "c3640004deadbeef",
        "html_url": RUN_URL,
    }


def _read(hold="none", run=None, serial=False):
    return landing_clear.read(lambda: hold, lambda: run or _run(), lambda: serial)


def _fail():
    raise RuntimeError("gh api exited 1: HTTP 502")


def test_no_hold_and_a_green_master_is_clear():
    assert _read() == {
        "state": "clear",
        "detail": "DEPLOY_HOLD none; master CI green c3640004",
    }


def test_an_absent_hold_variable_is_no_hold():
    assert _read(hold=None)["state"] == "clear"


def test_a_red_master_blocks_and_names_the_run():
    state = _read(run=_run(conclusion="failure"))
    assert state == {"state": "blocked", "detail": f"master CI is red: {RUN_URL}"}


@pytest.mark.parametrize(
    "run",
    [_run(status="in_progress", conclusion=None), _run(conclusion="cancelled")],
    ids=["pending", "cancelled"],
)
def test_a_pending_or_cancelled_master_is_not_a_blocker(run):
    assert _read(run=run)["state"] == "clear"


def test_a_live_serial_landing_blocks():
    state = _read(serial=True)
    assert state["state"] == "blocked"
    assert "serial PR is live" in state["detail"]


def test_both_blockers_are_named():
    state = _read(hold="abc1234", run=_run(conclusion="failure"))
    assert state["detail"] == (
        f"DEPLOY_HOLD is set; `probe.py landing` names it | master CI is red: {RUN_URL}"
    )


@pytest.mark.parametrize(
    "hold_read, ci_read",
    [(_fail, _run), (lambda: "none", _fail)],
    ids=["hold", "ci"],
)
def test_a_failed_read_exits_1_and_prints_no_state(hold_read, ci_read, capsys):
    def read_state():
        return landing_clear.read(hold_read, ci_read, lambda: False)

    assert landing_clear.main([], read_state=read_state) == 1
    out = capsys.readouterr()
    assert out.out == ""
    assert "HTTP 502" in out.err


def test_describe_declares_a_failure_state_cc_wait_accepts(capsys):
    # cc-wait refuses a source whose terminal states all exit 0, and owns codes 2 and 75.
    assert landing_clear.main(["--describe"]) == 0
    terminal = json.loads(capsys.readouterr().out)["terminal"]
    assert terminal["clear"] == 0
    assert any(code not in (0, 2, 75) for code in terminal.values()), terminal


def _gh_exits(code, stdout="", stderr=""):
    def fake_gh(*args, **kwargs):
        return subprocess.CompletedProcess(["gh", *args], code, stdout, stderr)

    return fake_gh


def _ci_read(code, stdout="", stderr=""):
    return lambda: landing_clear._master_ci_run(_gh_exits(code, stdout, stderr))


def test_no_gh_login_ends_the_wait_as_gh_unauthenticated():
    state = landing_clear.read(
        deploy_hold=lambda: pytest.fail("hold read after a gh exit 4"),
        master_ci_run=_ci_read(4, stderr="To get started with GitHub CLI"),
    )
    assert state["state"] == "gh-unauthenticated"
    assert state["state"] in landing_clear.TERMINAL


def test_any_other_gh_failure_is_a_failed_read(capsys):
    ci_read = _ci_read(1, stderr="HTTP 401: Bad credentials")

    def read_state():
        return landing_clear.read(lambda: "none", ci_read)

    assert landing_clear.main([], read_state=read_state) == 1
    assert capsys.readouterr().out == ""


def test_a_master_with_no_ci_run_reads_pending():
    state = landing_clear.read(
        lambda: "none", _ci_read(0, '{"workflow_runs": []}'), lambda: False
    )
    assert state == {"state": "clear", "detail": "DEPLOY_HOLD none; master CI pending"}


def _hold(directory, name):
    return turn.try_take(directory / name)


def test_a_held_serial_lock_keeps_the_wait_open_and_freeing_it_clears(
    tmp_path, monkeypatch
):
    monkeypatch.setenv(serial_lock.TURN_DIR_ENV, str(tmp_path))
    held = _hold(tmp_path, serial_lock.SERIAL_NAME)
    assert serial_lock.serial_landing_live("anywhere")
    held()
    assert not serial_lock.serial_landing_live("anywhere")


def test_a_held_landing_slot_is_not_a_serial_landing(tmp_path, monkeypatch):
    monkeypatch.setenv(serial_lock.TURN_DIR_ENV, str(tmp_path))
    held = _hold(tmp_path, "homelab-land-slot-0.lock")
    assert not serial_lock.serial_landing_live("anywhere")
    held()


def test_an_unanswered_turn_host_is_a_failed_read_not_clear(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.delenv(serial_lock.TURN_DIR_ENV, raising=False)
    bin_dir = proc_testing.fake_bin(
        tmp_path, ssh="echo 'ssh: connection refused' >&2\nexit 255\n"
    )
    monkeypatch.setenv("PATH", proc_testing.path_with(bin_dir))

    def read_state():
        return landing_clear.read(
            lambda: "none",
            lambda: _run(),
            lambda: serial_lock.serial_landing_live("daniel-server"),
        )

    assert landing_clear.main([], read_state=read_state) == 1
    assert capsys.readouterr().out == ""
