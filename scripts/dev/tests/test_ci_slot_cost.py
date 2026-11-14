"""The slot-seconds, queue waits and pytest-leg setup `ci_slot_cost.py` reads off a run's jobs."""

import pytest

from ci_slot_cost import head_line, p90, recent_runs, run_cost

SHA = "0123456789abcdef0123456789abcdef01234567"


def _at(seconds: int) -> str:
    return f"2026-11-14T12:{seconds // 60:02d}:{seconds % 60:02d}Z"


def _step(name: str, began: int, ended: int) -> dict:
    return {"name": name, "started_at": _at(began), "completed_at": _at(ended)}


def _job(name, created, started, completed, conclusion="success", steps=()):
    return {
        "name": name,
        "conclusion": conclusion,
        "created_at": _at(created),
        "started_at": _at(started),
        "completed_at": _at(completed),
        "steps": list(steps),
    }


def _leg(shard: int, created: int, started: int, suite: tuple[int, int]) -> dict:
    return _job(
        f"pytest (shard {shard} of 2)",
        created,
        started,
        suite[1] + 2,
        steps=[
            _step("Run actions/checkout@v7.0.1", started, started + 6),
            _step(
                f"Run DanielH2018/server/.github/actions/setup@{SHA}",
                started + 6,
                suite[0],
            ),
            _step("Run the test suite", *suite),
        ],
    )


def test_a_jobs_wait_runs_from_its_creation_and_a_skipped_job_holds_no_slot():
    cost = run_cost(
        [
            _job("prove the pushed tree was tested", 0, 2, 10),
            _job("host_packages_bootstrap (24.04, 3.12)", 10, 70, 370),
            _job("host_packages_bootstrap (26.04, 3.14)", 10, 30, 330),
            # The API stamps a skipped job as finishing before it started.
            _job("hooks (lint)", 10, 10, 9, conclusion="skipped"),
        ]
    )

    assert cost["jobs"] == 3
    assert cost["slot_s"] == 8 + 300 + 300
    assert sorted(cost["waits"]) == [2, 20, 60]
    assert cost["families"]["host_packages_bootstrap"] == {
        "jobs": 2,
        "slot_s": 600,
        "wait_median_s": 40,
    }
    assert "hooks" not in cost["families"]


def test_a_legs_setup_is_its_span_outside_the_suite_step_with_action_shas_folded():
    cost = run_cost([_leg(1, 0, 5, (20, 140)), _leg(2, 0, 9, (30, 150))])

    # Leg 1 holds its runner 5..142 and its suite 20..140; leg 2 holds 9..152, suite 30..150.
    assert cost["leg_outside_s"] == [17, 23]
    assert cost["leg_steps"][1] == {
        "Run actions/checkout@v7.0.1": 6,
        "Run DanielH2018/server/.github/actions/setup": 15,
    }
    assert cost["families"]["pytest"]["jobs"] == 2


def test_the_p90_interpolates_between_ranks_and_takes_a_single_value_as_is():
    assert p90([float(n) for n in range(1, 11)]) == pytest.approx(9.1)
    assert p90([42.0]) == 42.0


def test_the_head_line_carries_slot_seconds_and_both_quantiles():
    cost = run_cost(
        [_job(f"pytest (shard {n} of 10)", 0, n, n + 100) for n in range(1, 11)]
    )

    assert head_line(7, cost) == (
        "run 7: 10 jobs, 1000 slot-seconds, queue wait p50 6s, p90 9s"
    )


def _fake_gh(runs: dict[int, list[dict]]):
    def gh_json(*args):
        if args[:2] == ("run", "list"):
            return [{"databaseId": run_id} for run_id in runs]
        run_id = int(args[1].split("/runs/")[1].split("/")[0])
        return {"jobs": runs[run_id]}

    return gh_json


def test_recent_runs_skip_a_run_whose_pytest_legs_were_all_skipped():
    leg = _leg(1, 0, 5, (20, 140))
    skipped = _job("pytest (shard 1 of 2)", 0, 0, 0, conclusion="skipped")
    gh_json = _fake_gh({3: [leg], 2: [skipped], 1: [leg]})

    assert list(recent_runs(2, gh_json=gh_json)) == [3, 1]
    with pytest.raises(
        RuntimeError, match="2 of the newest 3 green master runs ran pytest"
    ):
        recent_runs(3, gh_json=gh_json)
