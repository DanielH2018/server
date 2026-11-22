"""`await_ci` reads a gate failed by its own run's cancellation as no verdict.

A run cancelled mid-sweep fails its `always()` gate on the cancelled jobs, and `land.sh` would
end `pr-ci-red`. A job past its `timeout-minutes` also reads `cancelled` on a run
nobody cancelled, so only the check suite's own `cancelled` conclusion excuses the gate.

Run: uv run pytest scripts/deploy_tools/tests/test_await_ci_cancelled_gate.py
"""

import pytest

from deploy_tools import await_ci
from lib.exit_codes import CI_PENDING
from ci_verdict import (
    CI_GATE_NAME,
    CI_GATE_NEEDS_JOBS,
    parse_check_runs,
    parse_check_suites,
)

REQUIRED = frozenset({CI_GATE_NAME})
# Suite 7's jobs were cancelled and its gate failed on them.
RUNS = parse_check_runs(
    [
        {
            "name": n,
            "status": "completed",
            "conclusion": "cancelled",
            "check_suite": {"id": 7},
        }
        for n in CI_GATE_NEEDS_JOBS
    ]
    + [
        {
            "name": CI_GATE_NAME,
            "status": "completed",
            "conclusion": "failure",
            "check_suite": {"id": 7},
        }
    ]
)


def _suites(conclusion):
    return lambda _s: parse_check_suites(
        [{"id": 7, "status": "completed", "conclusion": conclusion}]
    )


def test_a_gate_failed_by_a_cancelled_run_is_pending():
    assert await_ci.verdict_for(
        "abc", lambda _s: RUNS, REQUIRED, _suites("cancelled")
    ) == ("pending")


def test_a_gate_failed_by_a_timed_out_job_stays_red():
    assert await_ci.verdict_for(
        "abc", lambda _s: RUNS, REQUIRED, _suites("failure")
    ) == ("fail")


def test_the_one_shot_pr_gate_waits_instead_of_reading_red():
    """`land.sh`'s merge loop calls `wait(head, 0)`; exit 1 there is `pr-ci-red`."""
    rc, line = await_ci.wait(
        "abc",
        0,
        0,
        sleep=lambda _s: None,
        fetch=lambda _s: RUNS,
        fetch_suites=_suites("cancelled"),
        descendants=lambda _s: [],
        fetch_tip=lambda: "tip",
    )
    assert rc == CI_PENDING, line


# Suite 8 is a draft run on the same head: every job and its gate skipped.
DRAFT_RUN = parse_check_runs(
    [
        {
            "name": n,
            "status": "completed",
            "conclusion": "skipped",
            "check_suite": {"id": 8},
        }
        for n in [*CI_GATE_NEEDS_JOBS, CI_GATE_NAME]
    ]
)


@pytest.mark.parametrize(
    "runs, names_the_draft", [(RUNS + DRAFT_RUN, True), (RUNS, False)]
)
def test_a_cancelled_ready_run_beside_a_draft_run_reads_as_draft_skipped(
    runs, names_the_draft
):
    """The draft run cancelled the ready run through ci.yml's concurrency group, and the merge
    wait sat silent until `merge_timeout`. A head with no draft run is only cancelled."""
    rc, line = await_ci.wait(
        "abc",
        0,
        0,
        sleep=lambda _s: None,
        fetch=lambda _s: runs,
        fetch_suites=lambda _s: parse_check_suites(
            [
                {"id": 7, "status": "completed", "conclusion": "cancelled"},
                {"id": 8, "status": "completed", "conclusion": "skipped"},
            ]
        ),
        descendants=lambda _s: [],
        fetch_tip=lambda: "tip",
    )
    assert rc == CI_PENDING, line
    assert (await_ci.DRAFT_SKIPPED in line) is names_the_draft
