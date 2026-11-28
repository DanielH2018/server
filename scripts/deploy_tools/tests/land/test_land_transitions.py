"""A landing's transition lines and state records, read through the fakes of a whole landing.

Run: uv run pytest scripts/deploy_tools/tests/land/test_land_transitions.py
"""

import json
import os
from dataclasses import replace
from itertools import pairwise

from _land_fakes import EPOCH, PRIMARY, Fakes, build_classifier, build_tools
from _land_turn_fakes import HEAD, MERGED, OPEN, START_VIEW
from deploy_tools import land
from deploy_tools.land_lib.options import Options
from deploy_tools.land_lib.transitions import EVENT, State
from deploy_tools.promote_lib.wait import WaitResult, WaitVerdict

from lib import host_modules  # noqa: F401
from host_lib import parse_logfmt

LAND = ["--arm-merge", "--await-merge"]


def _fakes(polls, **kwargs) -> Fakes:
    """A draft PR that `--arm-merge --await-merge` lands, polled as `polls` read."""
    return Fakes(
        gh_views={
            "state,title,body,reviewDecision": {
                "state": "OPEN",
                "title": "Record each state",
                "body": "",
            },
            "headRefOid": {"headRefOid": HEAD},
            "state,mergeable,headRefOid": list(polls),
            START_VIEW: {"isDraft": True, **OPEN},
        },
        **kwargs,
    )


def _settles() -> Fakes:
    """A draft whose run is pending for one poll, then green, and whose promotion settles."""
    return _fakes(
        [OPEN, OPEN, MERGED],
        await_ci=[(75, "pending"), (0, "CI green")],
        land_wait=[
            WaitResult(WaitVerdict.SETTLED, detail="1 applied", tags=("sonarr",))
        ],
    )


def _moves(calls) -> list[dict[str, str]]:
    """The transition lines among the `logger` calls, parsed."""
    lines = [parse_logfmt(c[1][0]) for c in calls if c[0] == "logger"]
    return [fields for fields in lines if fields.get("event") == EVENT]


def _records(calls) -> list[dict]:
    """Every state record the landing wrote, oldest first."""
    return [json.loads(c[1][1]) for c in calls if c[0] == "record"]


def _assert_each_move_follows_the_last(moves, verdict: str) -> None:
    assert moves, "the landing wrote no transition line"
    for before, after in pairwise([{"to": ""}, *moves]):
        assert after["from"] == before["to"], moves
        assert after["pr"] == "999" and float(after["at"]) > EPOCH, after
    assert [m["verdict"] for m in moves] == [""] * (len(moves) - 1) + [verdict]
    assert moves[-1]["to"] == State.VERDICT


def test_a_settled_landing_writes_a_transition_line_for_each_state_it_entered(land_run):
    rc, out, _, calls, _ = land_run(LAND, _settles())
    assert rc == 0 and "VERDICT: settled" in out, out
    moves = _moves(calls)
    _assert_each_move_follows_the_last(moves, "settled")
    assert [m["to"] for m in moves] == [
        "armed",
        "queued",
        "holding",
        "testing",
        "merging",
        "merged",
        "resolving",
        "master-ci",
        "promoting",
        "host-wait",
        "grading",
        "verdict",
    ]


def test_a_settled_landing_leaves_a_state_record_naming_its_verdict_and_history(
    land_run,
):
    _, _, _, calls, _ = land_run(LAND, _settles())
    moves, last = _moves(calls), _records(calls)[-1]
    assert (last["pr"], last["host"], last["pid"]) == ("999", "daniel-box", os.getpid())
    assert (last["state"], last["verdict"]) == ("verdict", "settled")
    assert last["since"] == float(moves[-1]["at"])
    assert last["history"] == [
        {"state": m["to"], "since": float(m["at"])} for m in moves[:-1]
    ]


def test_the_master_ci_wait_counts_its_checks_in_the_state_record(land_run):
    fakes = _settles()
    fakes.checks = (4, 9)
    _, _, _, calls, _ = land_run(LAND, fakes)
    assert {
        "state": "master-ci",
        "progress": {"done": 4, "total": 9, "what": "checks finished"},
    } in [{"state": r["state"], "progress": r["progress"]} for r in _records(calls)]


def test_a_landing_that_times_out_in_the_turn_ends_on_merge_timeout(land_run):
    fakes = _fakes([OPEN], await_ci=[(75, "pending")], checks=(1, 3))
    rc, out, _, calls, _ = land_run(LAND, fakes)
    assert rc == 75 and "VERDICT: merge-timeout" in out, out
    moves = _moves(calls)
    _assert_each_move_follows_the_last(moves, "merge-timeout")
    assert [m["to"] for m in moves] == [
        "armed",
        "queued",
        "holding",
        "testing",
        "verdict",
    ]
    # The record the deploy UI read while the landing waited: its state, entered when, and
    # the CI read's count, which comes after the state it belongs to.
    waiting = _records(calls)[-2]
    assert waiting["state"] == "testing" and waiting["verdict"] == ""
    assert waiting["since"] == float(moves[-2]["at"])
    assert waiting["progress"] == {"done": 1, "total": 3, "what": "checks finished"}


def test_writers_that_raise_leave_the_verdict_as_it_was(capsys):
    _, settled, _ = _land(capsys, _settles())
    rc, out, calls = _land(capsys, _settles(), failing_writers=True)
    assert rc == 0
    assert _results(out) == _results(settled)
    assert "could not write PR #999's state record (no turn host)" in out
    # The first failure stops the record, so a dead turn host costs one try, not one per state.
    assert len([c for c in calls if c[0] == "record"]) == 1


def _land(capsys, fakes: Fakes, failing_writers: bool = False):
    """One landing of PR 999 through `land._land`: its exit code, stdout and calls.

    With `failing_writers`, `tools.logger` raises always and the line raises on `record`.
    """
    tools, calls = build_tools(fakes)
    if failing_writers:
        line = tools.line
        assert line is not None

        def logger(text: str) -> None:
            raise OSError("logger: no syslog")

        def line_failing_on_record(op, *args):
            if op == "record":
                calls.append(("record", args, {}))
                raise OSError("no turn host")
            return line(op, *args)

        tools = replace(tools, logger=logger, line=line_failing_on_record)
    opts = Options(pr="999", primary=PRIMARY, arm_merge=True, await_merge=True)
    rc = land._land(opts, tools, build_classifier(fakes, calls), LAND)
    return rc, capsys.readouterr().out, calls


def _results(out: str) -> list[str]:
    return [line for line in out.splitlines() if line.startswith("LAND-RESULT:")]
