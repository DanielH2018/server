"""`scripts/dev/ci_failures.py` on what real `gh` does and the fake in `test_ci_failures.py` does not.

Real `gh run view --log-failed` returns a failed job's whole log labelled `UNKNOWN STEP`, and it
can fail on an expired log. The overlays below replace the fake's functions to serve those shapes.
This module also holds the red-attempt membership rule and the survey's failure paths.

Run: uv run pytest scripts/dev/tests/test_ci_failures_real_gh.py
"""

import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from lib.proc_testing import fake_bin, run
from lib.repo_paths import REPO
from test_ci_failures import (
    FAKE_GITHUB,
    SCRIPT,
    SINCE,
    UNTIL,
    _attempt,
    _green_attempt,
    _job,
    _pytest_job,
    _run,
    _step,
    _survey,
)


def test_a_window_past_the_cap_is_split(tmp_path):
    # All inside one half-day window, so only splitting that window reaches the oldest run.
    start = datetime(2026, 10, 30, 1, tzinfo=timezone.utc)
    gap = timedelta(hours=10) / 1100
    filler = [
        _run(
            44000000000 + n,
            {"conclusion": "success", "jobs": []},
            created=(start + gap * (n + 1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        for n in range(1010)
    ]
    oldest = _run(
        41141558428,
        _attempt(_pytest_job(127000000008, "test_past_a_full_window")),
        created=start.strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    out = _survey(tmp_path, oldest, *filler).stdout
    assert "test_past_a_full_window" in out, out[:2000]


def test_unknown_step_lines_count_only_inside_the_failed_steps_seconds():
    import ci_failures

    # Real `gh run view --log-failed` labels the whole failed job this way.
    logged = [
        ("UNKNOWN STEP", "2026-11-05T14:09:01.5Z FAILED a/test_before.py::t - x"),
        ("UNKNOWN STEP", "2026-11-05T14:09:02.5Z FAILED a/test_inside.py::t - x"),
        ("UNKNOWN STEP", "2026-11-05T14:32:52.7Z FAILED a/test_last_second.py::t - x"),
        ("UNKNOWN STEP", "2026-11-05T14:32:53.0Z FAILED a/test_after.py::t - x"),
    ]
    step = {
        "name": "Run the suite",
        "started_at": "2026-11-05T14:09:02Z",
        "completed_at": "2026-11-05T14:32:52Z",
    }
    assert ci_failures.failing_modules(ci_failures.step_lines(logged, step)) == {
        "a/test_inside.py",
        "a/test_last_second.py",
    }


def test_caret_escapes_and_collection_errors_name_their_module():
    import ci_failures

    # `gh` prints an ESC byte as the two characters ^[ when it writes a log.
    lines = [
        "2026-11-05T14:09:02.5Z ^[[31mFAILED^[[0m a/test_caret.py::t - x",
        "2026-11-05T14:09:02.5Z ERROR a/test_collection.py - ImportError",
        "2026-11-05T14:09:02.5Z FAILED to fetch the index",
    ]
    assert ci_failures.failing_modules(lines) == {
        "a/test_caret.py",
        "a/test_collection.py",
    }


def test_a_timestamped_failed_line_in_a_run_header_is_not_counted():
    import ci_failures

    lines = [
        "2026-11-05T14:09:02.5Z ##[group]Run cd job",
        "2026-11-05T14:09:02.5Z   PR_BODY: x",
        "2026-11-05T14:09:02.5Z FAILED a/test_echoed.py::t - x",
        "2026-11-05T14:09:02.5Z ##[endgroup]",
        "2026-11-05T14:09:03.5Z FAILED a/test_real.py::t - x",
    ]
    assert ci_failures.failing_modules(lines) == {"a/test_real.py"}


# Appended to the fake's source, these replace its functions by name. Real `gh` returned a failed
# job's whole log labelled UNKNOWN STEP, with passing steps in it, and the jobs endpoint carries
# each step's own seconds.
_WHOLE_JOB_LOG = r"""
def run_log(argv):
    run = RUNS[next(int(a) for a in argv if a.isdigit())]
    number = int(argv[argv.index("--attempt") + 1])
    rows = [
        "%s\tUNKNOWN STEP\t%s" % (job["name"], line)
        for job in run["attempts"][number - 1]["jobs"]
        if job["conclusion"] == "failure"
        for step in job["steps"]
        for line in step["lines"]
    ]
    sys.stdout.write("\n".join(rows) + "\n")


_job_object = job_object


def job_object(run, number, job):
    document = _job_object(run, number, job)
    for written, step in zip(document["steps"], job["steps"]):
        written["started_at"] = step.get("started_at", run["created_at"])
        written["completed_at"] = step.get("completed_at", run["created_at"])
    return document

"""
_LOG_EXPIRED = """
def run_log(argv):
    die("gh: log not found")

"""
_LISTING_FAILS = """
def search(query):
    die("gh: HTTP 500 from the listing")

"""


def _survey_with(
    tmp_path: Path, overlay: str, *runs: dict
) -> subprocess.CompletedProcess[str]:
    """Run the survey against the fake with `overlay` replacing some of its functions."""
    source = FAKE_GITHUB.read_text().removesuffix("main()\n") + overlay + "main()\n"
    fake = f"#!{sys.executable} -IS\n{source}"
    stubs = fake_bin(tmp_path / "bin", gh=fake, ghread=fake)
    (stubs / "world.json").write_text(json.dumps(runs))
    argv = [sys.executable, str(SCRIPT), "--since", SINCE, "--until", UNTIL]
    return run(argv, cwd=REPO, stub_bin=stubs)


def _step_at(second: int, name: str, line: str, conclusion: str) -> dict:
    stamp = f"2026-11-05T14:10:{second:02d}"
    return {
        "name": name,
        "number": second,
        "conclusion": conclusion,
        "started_at": f"{stamp}Z",
        "completed_at": f"{stamp}Z",
        "lines": [f"{stamp}.5000000Z {line}"],
    }


def test_whole_job_log_counts_only_the_failed_steps_seconds(tmp_path):
    job = _job(
        127000000009,
        "pytest",
        _step_at(
            10,
            "Run the flaky retry",
            "FAILED ansible/tests/repo/test_in_the_retry.py::test_case - assert 0",
            "success",
        ),
        _step_at(
            20,
            "Run the suite",
            "FAILED ansible/tests/repo/test_in_the_suite.py::test_case - assert 0",
            "failure",
        ),
    )
    done = _survey_with(tmp_path, _WHOLE_JOB_LOG, _run(43000000005, _attempt(job)))
    assert done.returncode == 0, done.stderr
    assert "test_in_the_suite" in done.stdout, done.stdout
    assert "Run the suite" in done.stdout, done.stdout
    assert "test_in_the_retry" not in done.stdout, done.stdout


def test_cancelled_attempt_is_clean(tmp_path):
    cancelled = _run(
        43000000006,
        _attempt(
            _pytest_job(127000000010, "test_in_a_cancelled_attempt"),
            conclusion="cancelled",
        ),
        _green_attempt(),
    )
    out = _survey(tmp_path, cancelled).stdout
    assert "43000000006" not in out, out
    assert "test_in_a_cancelled_attempt" not in out, out


@pytest.mark.parametrize("conclusion", ["timed_out", "startup_failure"])
def test_timed_out_and_startup_failure_attempts_are_flagged(tmp_path, conclusion):
    job = _job(
        127000000011,
        "stalled",
        _step("Run the suite", conclusion="cancelled"),
        conclusion=conclusion,
    )
    out = _survey(
        tmp_path, _run(43000000007, _attempt(job, conclusion=conclusion))
    ).stdout
    assert "run 43000000007 attempt 1" in out, out
    assert "stalled / - / -" in out, f"want the red job with no failed step:\n{out}"


def test_unreadable_log_is_counted_and_keeps_its_job_row(tmp_path):
    attempt = _attempt(_pytest_job(127000000012, "test_behind_the_expired_log"))
    done = _survey_with(tmp_path, _LOG_EXPIRED, _run(43000000008, attempt))
    assert done.returncode == 0, done.stderr
    assert "1 red attempts had no readable log" in done.stdout, done.stdout
    assert "pytest / Run the suite / -" in done.stdout, done.stdout


def test_a_readable_log_adds_no_unreadable_line(tmp_path):
    attempt = _attempt(_pytest_job(127000000013, "test_read"))
    out = _survey(tmp_path, _run(43000000009, attempt)).stdout
    assert "no readable log" not in out, out


def test_a_failed_listing_exits_1_with_gh_stderr(tmp_path):
    done = _survey_with(tmp_path, _LISTING_FAILS)
    assert done.returncode == 1, done.stdout
    assert "HTTP 500 from the listing" in done.stderr, done.stderr


def test_one_second_at_the_search_cap_warns(tmp_path):
    filler = [
        _run(
            44000000000 + n,
            {"conclusion": "success", "jobs": []},
            created="2026-10-30T01:00:00Z",
        )
        for n in range(1000)
    ]
    done = _survey_with(tmp_path, "", *filler)
    assert done.returncode == 0, done.stderr
    assert "search cap" in done.stderr, done.stderr


def test_a_window_under_the_search_cap_does_not_warn(tmp_path):
    done = _survey_with(tmp_path, "", _run(43000000010, _green_attempt()))
    assert done.returncode == 0, done.stderr
    assert "search cap" not in done.stderr, done.stderr
