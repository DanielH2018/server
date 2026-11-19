"""`CheckRun` parses both of GitHub's check shapes, and one run grades alike on every path.

Run: uv run pytest hosts/lib/tests/test_ci_verdict.py
"""

import pytest
from ci_verdict import (
    CI_GATE_NAME,
    CI_GATE_NEEDS_JOBS,
    CI_PROVE_JOB,
    CheckRun,
    RunState,
    ci_verdict,
    jobs_verdict,
)


def _jobs(neutral: str) -> list[CheckRun]:
    """`prove` and every gate need succeeded, except `neutral`, which concluded neutral."""
    return [
        CheckRun(name, "completed", "neutral" if name == neutral else "success")
        for name in (CI_PROVE_JOB, *CI_GATE_NEEDS_JOBS)
    ]


def test_a_neutral_need_grades_pass_through_the_jobs_as_it_does_as_a_required_context():
    need = sorted(CI_GATE_NEEDS_JOBS)[0]
    runs = _jobs(neutral=need)

    assert jobs_verdict(runs) == "pass"
    assert ci_verdict(runs, frozenset({CI_GATE_NAME})) == "pass"
    assert ci_verdict(runs, frozenset({need})) == "pass"


@pytest.mark.parametrize(
    ("conclusion", "state"),
    [
        ("success", RunState.PASS),
        ("neutral", RunState.PASS),
        ("skipped", RunState.SKIPPED),
        ("cancelled", RunState.PENDING),
        ("failure", RunState.FAIL),
        ("action_required", RunState.FAIL),
    ],
)
def test_a_rest_check_run_parses_to_its_state_and_suite(conclusion, state):
    run = CheckRun.parse(
        {
            "name": "x",
            "status": "completed",
            "conclusion": conclusion,
            "check_suite": {"id": 7},
        }
    )

    assert (run.state, run.suite_id) == (state, 7)


@pytest.mark.parametrize(
    ("node", "state"),
    [
        ({"name": "x", "status": "IN_PROGRESS", "conclusion": ""}, RunState.PENDING),
        ({"name": "x", "conclusion": "NEUTRAL"}, RunState.PASS),
        ({"name": "x", "conclusion": "CANCELLED"}, RunState.PENDING),
        ({"context": "x", "state": "PENDING"}, RunState.PENDING),
        ({"context": "x", "state": "SUCCESS"}, RunState.PASS),
        ({"context": "x", "state": "ERROR"}, RunState.FAIL),
    ],
)
def test_a_rollup_node_parses_to_the_state_its_rest_run_would_have(node, state):
    run = CheckRun.from_rollup(node)

    assert (run.name, run.state) == ("x", state)


def test_a_check_run_that_is_not_an_object_is_refused():
    with pytest.raises(ValueError, match="not an object"):
        CheckRun.parse(["x"])
