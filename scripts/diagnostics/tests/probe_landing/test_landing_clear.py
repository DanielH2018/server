"""The `landing-clear` source cc-wait reads, with both GitHub reads handed in as callables.

Run: uv run pytest scripts/diagnostics/tests/probe_landing/test_landing_clear.py
"""

import json
import subprocess


from diagnostics import landing_clear
from diagnostics.probe_lib import landing_blockers
import pytest
from deploy_tools.land_lib import serial_lock, turn
from ci_verdict import WorkflowRun

RUN_URL = "https://github.com/DanielH2018/server/actions/runs/123"


def _run(status="completed", conclusion: str | None = "success"):
    return WorkflowRun.parse(
        {
            "status": status,
            "conclusion": conclusion,
            "head_sha": "c3640004deadbeef",
            "html_url": RUN_URL,
        }
    )


def _read(hold: str | None = "none", run=None):
    return landing_clear.read(lambda: hold, lambda: run or _run())


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
        return landing_clear.read(hold_read, ci_read)

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
    state = landing_clear.read(lambda: "none", _ci_read(0, '{"workflow_runs": []}'))
    assert state == {"state": "clear", "detail": "DEPLOY_HOLD none; master CI pending"}


def _hold(directory, name):
    release = turn.try_take(directory / name)
    assert release is not None
    return release


def _serial_landing_of(directory, pr):
    """Hold the locks a serial landing of `pr` holds in `directory`; the call that ends it."""
    serial = _hold(directory, serial_lock.SERIAL_NAME)
    own = _hold(directory, serial_lock.lock_name(pr))
    serial_lock.record_holder("anywhere", pr)

    def end():
        serial()
        own()

    return end


def test_a_held_serial_lock_names_its_pr_and_freeing_it_clears(tmp_path, monkeypatch):
    monkeypatch.setenv(serial_lock.TURN_DIR_ENV, str(tmp_path))
    end = _serial_landing_of(tmp_path, "9418")
    assert serial_lock.serial_landing("anywhere") == "9418"
    end()
    assert serial_lock.serial_landing("anywhere") is None


def test_a_number_left_by_an_ended_landing_names_no_holder(tmp_path, monkeypatch):
    monkeypatch.setenv(serial_lock.TURN_DIR_ENV, str(tmp_path))
    _serial_landing_of(tmp_path, "9386")()
    held = _hold(tmp_path, serial_lock.SERIAL_NAME)
    assert serial_lock.serial_landing("anywhere") == ""
    held()


def test_a_held_landing_slot_is_not_a_serial_landing(tmp_path, monkeypatch):
    monkeypatch.setenv(serial_lock.TURN_DIR_ENV, str(tmp_path))
    held = _hold(tmp_path, "homelab-land-slot-0.lock")
    assert serial_lock.serial_landing("anywhere") is None
    held()


def test_the_probe_and_the_wait_agree_a_live_serial_landing_blocks_nothing(
    tmp_path, monkeypatch
):
    monkeypatch.setenv(serial_lock.TURN_DIR_ENV, str(tmp_path))
    end = _serial_landing_of(tmp_path, "9418")
    snap = landing_blockers.collect(
        lambda path: {"runs": []},
        lambda argv: "[]",
        serial=lambda: serial_lock.serial_landing("anywhere"),
    )
    wait = landing_clear.read(lambda: "none", _run)
    end()
    assert snap["serial"] == {"live": True, "pr": "9418"}
    assert (snap["blockers"], wait["state"]) == ([], "clear")
