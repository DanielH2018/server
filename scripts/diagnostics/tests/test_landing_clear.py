"""The `landing-clear` source cc-wait reads, with both GitHub reads handed in as callables.

Run: uv run pytest scripts/diagnostics/tests/test_landing_clear.py
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import landing_clear
import pytest

RUN_URL = "https://github.com/DanielH2018/server/actions/runs/123"


def _run(status="completed", conclusion="success"):
    return {
        "status": status,
        "conclusion": conclusion,
        "head_sha": "c3640004deadbeef",
        "html_url": RUN_URL,
    }


def _read(hold="none", run=None):
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


def test_describe_names_clear_as_the_only_terminal_state(capsys):
    assert landing_clear.main(["--describe"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "terminal": {"clear": 0},
        "interval_s": landing_clear.INTERVAL_S,
    }
