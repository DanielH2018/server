"""`CheckRun` and `CheckSuite` parse GitHub's check shapes, and one run grades alike on every path.

Run: uv run pytest hosts/lib/tests/test_ci_verdict.py
"""

import pytest
from ci_verdict import (
    _CI_NO_VERDICT_CONCLUSIONS,
    CI_GATE_NAME,
    CI_GATE_NEEDS_JOBS,
    CI_PROVE_JOB,
    CheckRun,
    CheckSuite,
    RunState,
    checks_verdict,
    ci_verdict,
    jobs_verdict,
    parse_check_suites,
    parse_rollup,
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


# Two entries of `GET /repos/DanielH2018/server/commits/bd39c048c/check-suites`, cut to the
# fields beside the ones the type reads: an app that registered nothing, and the CI run.
SUITES = [
    {
        "id": 121321898339,
        "head_branch": "master",
        "head_sha": "bd39c048cd019f8967e83648d2f705eaba287194",
        "status": "queued",
        "conclusion": None,
        "app": {"id": 46505, "slug": "gitguardian", "name": "GitGuardian"},
        "latest_check_runs_count": 0,
        "rerequestable": True,
    },
    {
        "id": 121321905377,
        "head_branch": "master",
        "head_sha": "bd39c048cd019f8967e83648d2f705eaba287194",
        "status": "completed",
        "conclusion": "success",
        "app": {"id": 15368, "slug": "github-actions", "name": "GitHub Actions"},
        "latest_check_runs_count": 15,
        "rerequestable": True,
    },
]


def test_a_rest_check_suite_listing_parses_to_its_fields():
    assert parse_check_suites(SUITES) == [
        CheckSuite(121321898339, "queued", None, 0, "gitguardian"),
        CheckSuite(121321905377, "completed", "success", 15, "github-actions"),
    ]


def test_a_check_suite_that_is_not_an_object_is_refused():
    with pytest.raises(ValueError, match="not an object"):
        parse_check_suites([121321905377])


def _completed(*conclusions: str | None) -> list[CheckRun]:
    return [CheckRun("x", "completed", conclusion) for conclusion in conclusions]


@pytest.mark.parametrize(
    ("checks", "verdict"),
    [
        ([], "pending"),
        (_completed("success", "skipped", "neutral"), "pass"),
        (_completed("success") + [CheckRun("x", "in_progress", None)], "pending"),
        (_completed("failure") + [CheckRun("x", "in_progress", None)], "fail"),
        (_completed("success", "action_required"), "fail"),
        *[
            (_completed("success", conclusion), "pending")
            for conclusion in sorted(_CI_NO_VERDICT_CONCLUSIONS, key=str)
        ],
    ],
)
def test_the_checks_of_one_head_reduce_to_their_worst_state(checks, verdict):
    assert checks_verdict(checks) == verdict


def test_a_null_rollup_is_no_checks_and_any_other_shape_is_refused():
    assert parse_rollup(None) == []
    with pytest.raises(ValueError, match="not a list"):
        parse_rollup({"nodes": []})
