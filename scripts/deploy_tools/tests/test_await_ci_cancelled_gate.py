"""`await_ci` reads a gate failed by its own run's cancellation as no verdict (#5931).

PR #5922's run was cancelled mid-sweep, its `always()` gate failed on the cancelled jobs, and
`land.sh` ended `pr-ci-red`. A job past its `timeout-minutes` also reads `cancelled` on a run
nobody cancelled, so only the check suite's own `cancelled` conclusion excuses the gate.

Run: uv run pytest scripts/deploy_tools/tests/test_await_ci_cancelled_gate.py
"""

import await_ci
from lib.exit_codes import CI_PENDING
from deployer_lib.deploy_git import CI_GATE_NAME, CI_GATE_NEEDS_JOBS

REQUIRED = frozenset({CI_GATE_NAME})
# Suite 7's jobs were cancelled and its gate failed on them, as run 39791122833 attempt 1 did.
RUNS = [
    {
        "name": n,
        "status": "completed",
        "conclusion": "cancelled",
        "check_suite": {"id": 7},
    }
    for n in CI_GATE_NEEDS_JOBS
] + [
    {
        "name": CI_GATE_NAME,
        "status": "completed",
        "conclusion": "failure",
        "check_suite": {"id": 7},
    }
]


def _suites(conclusion):
    return lambda _s: [{"id": 7, "status": "completed", "conclusion": conclusion}]


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
