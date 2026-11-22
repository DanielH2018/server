"""The CI types parse GitHub's check, status and run shapes, and one run grades alike on every path.

Run: uv run pytest hosts/lib/tests/test_ci_verdict.py
"""

from datetime import UTC, datetime

import pytest
from ci_verdict import (
    _CI_NO_VERDICT_CONCLUSIONS,
    CI_GATE_NAME,
    CI_GATE_NEEDS_JOBS,
    CI_PROVE_JOB,
    CheckRun,
    CheckSuite,
    CommitStatus,
    RunState,
    WorkflowRun,
    checks_verdict,
    ci_verdict,
    jobs_verdict,
    parse_check_suites,
    parse_commit_statuses,
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


def test_ci_verdict_refuses_an_empty_required_set():
    """An empty set would grade every commit green, so no caller may pass one."""
    with pytest.raises(ValueError, match="no required CI contexts"):
        ci_verdict(_jobs(""), frozenset())


def test_a_rest_commit_status_parses_to_its_creator_description_and_time():
    status = {
        "context": "ci/tested-tree",
        "state": "success",
        "creator": {"login": "github-actions[bot]", "id": 41898282},
        "description": "a" * 40,
        "target_url": "https://github.com/o/r/actions/runs/1",
        "updated_at": "2026-11-20T10:00:00Z",
    }
    assert parse_commit_statuses([status, {"context": "x"}]) == [
        CommitStatus(
            "ci/tested-tree",
            "success",
            "github-actions[bot]",
            "a" * 40,
            "https://github.com/o/r/actions/runs/1",
            datetime(2026, 11, 20, 10, tzinfo=UTC),
        ),
        CommitStatus("x", "", None, "", "", datetime.min.replace(tzinfo=UTC)),
    ]


@pytest.mark.parametrize("updated_at", [None, "yesterday", 1700000000])
def test_a_commit_status_with_no_readable_time_reads_as_the_earliest(updated_at):
    parsed = CommitStatus.parse({"context": "x", "updated_at": updated_at})
    assert parsed.updated_at == datetime.min.replace(tzinfo=UTC)


@pytest.mark.parametrize(
    "raw",
    [
        {
            "id": 7,
            "status": "completed",
            "conclusion": "failure",
            "head_sha": "b" * 40,
            "html_url": "https://github.com/o/r/actions/runs/7",
            "run_attempt": 2,
            "head_branch": "claude/x",
            "name": "CI",
            "created_at": "2026-11-21T10:00:00Z",
            "run_started_at": "2026-11-21T10:05:00Z",
        },
        {
            "databaseId": 7,
            "status": "COMPLETED",
            "conclusion": "FAILURE",
            "headSha": "b" * 40,
            "url": "https://github.com/o/r/actions/runs/7",
            "attempt": 2,
            "headBranch": "claude/x",
            "name": "CI",
            "createdAt": "2026-11-21T10:00:00Z",
            "startedAt": "2026-11-21T10:05:00Z",
        },
    ],
    ids=["rest", "gh-run-list"],
)
def test_a_workflow_run_parses_alike_from_rest_and_from_gh(raw):
    assert WorkflowRun.parse(raw) == WorkflowRun(
        id=7,
        status="completed",
        conclusion="failure",
        head_sha="b" * 40,
        url="https://github.com/o/r/actions/runs/7",
        attempt=2,
        head_branch="claude/x",
        name="CI",
        created_at=datetime(2026, 11, 21, 10, 0, tzinfo=UTC),
        started_at=datetime(2026, 11, 21, 10, 5, tzinfo=UTC),
    )


def test_a_rest_workflow_run_names_its_repository_and_head_tree():
    run = WorkflowRun.parse(
        {"repository": {"full_name": "o/r"}, "head_commit": {"tree_id": "c" * 40}}
    )
    assert (run.repository, run.head_tree) == ("o/r", "c" * 40)


def test_a_workflow_run_missing_its_attempt_and_times_reads_as_attempt_one_and_earliest():
    run = WorkflowRun.parse({"id": 7, "run_attempt": None, "created_at": "soon"})
    earliest = datetime.min.replace(tzinfo=UTC)
    assert (run.attempt, run.created_at, run.started_at) == (1, earliest, earliest)


@pytest.mark.parametrize(
    "status, conclusion, no_verdict",
    [
        ("completed", "", True),
        ("completed", "cancelled", True),
        ("completed", "failure", False),
        ("in_progress", None, False),
    ],
)
def test_a_completed_run_without_a_real_conclusion_is_no_verdict(
    status, conclusion, no_verdict
):
    run = WorkflowRun.parse({"status": status, "conclusion": conclusion})
    assert run.no_verdict is no_verdict


@pytest.mark.parametrize("parse", [CommitStatus.parse, WorkflowRun.parse])
def test_a_status_or_run_that_is_not_an_object_is_refused(parse):
    with pytest.raises(ValueError, match="not an object"):
        parse(["context", "state"])
