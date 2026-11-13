"""`scripts/dev/ci_failures.py` run as its CLI, against a fake GitHub on `PATH`.

`ci_failures_fake_github.txt` holds the fake. It answers as both `gh` and `ghread`, so the tool
may read through either. It serves the REST shapes of run 42041106424 and reproduces the traps a
by-hand survey hit: a run object that reports only its latest attempt, a `created=` search capped
at 1,000 results, a `--job` log read that returns the latest attempt, and `ghread api` refusing a
job log's terminal escapes. Only `run view --log-failed` ties a log line to its step.

Output format is the tool's own, so each test asserts on identifiers in the printed lines.

Run: uv run pytest scripts/dev/tests/test_ci_failures.py
"""

import json
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from lib.proc_testing import fake_bin, run
from lib.repo_paths import REPO

SCRIPT = REPO / "scripts" / "dev" / "ci_failures.py"

SINCE = "2026-10-30"
UNTIL = "2026-11-06"

FAKE_GITHUB = Path(__file__).with_name("ci_failures_fake_github.txt")


def _stamped(*lines: str) -> list[str]:
    """Timestamp log lines as the Actions runner does, with its BOM on the first."""
    stamped = [f"2026-11-05T14:10:00.0000000Z {line}" for line in lines]
    stamped[0] = "﻿" + stamped[0]
    return stamped


def _step(name: str, *lines: str, number: int = 5, conclusion: str = "failure") -> dict:
    return {
        "name": name,
        "number": number,
        "conclusion": conclusion,
        "lines": _stamped(
            *(lines or ("##[error]Process completed with exit code 1.",))
        ),
    }


def _job(job_id: int, name: str, *steps: dict, conclusion: str = "failure") -> dict:
    return {
        "id": job_id,
        "name": name,
        "conclusion": conclusion,
        "steps": [
            _step("Set up job", "Job started", number=1, conclusion="success"),
            *steps,
        ],
    }


def _attempt(*jobs: dict, conclusion: str = "failure") -> dict:
    return {"conclusion": conclusion, "jobs": list(jobs)}


def _run(
    run_id: int,
    *attempts: dict,
    name: str = "CI",
    branch: str = "master",
    created: str = "2026-11-05T14:02:04Z",
    event: str = "push",
) -> dict:
    return {
        "id": run_id,
        "name": name,
        "event": event,
        "head_branch": branch,
        "created_at": created,
        "attempts": list(attempts),
    }


def _green_attempt() -> dict:
    return _attempt(
        _job(
            990_000_000_000,
            "green",
            _step("Run the suite", "1 passed", conclusion="success"),
            conclusion="success",
        ),
        conclusion="success",
    )


def _pytest_job(job_id: int, *modules: str, job: str = "pytest") -> dict:
    """A job whose failed step prints pytest's short summary naming each module."""
    return _job(
        job_id,
        job,
        _step(
            "Run the suite",
            "##[group]Run uv run pytest",
            "##[endgroup]",
            "=== short test summary info ===",
            *(f"FAILED tests/repo/{m}.py::test_case - assert 1 == 2" for m in modules),
            "##[error]Process completed with exit code 1.",
        ),
    )


def _survey(tmp_path: Path, *runs: dict) -> subprocess.CompletedProcess[str]:
    """Run the survey over the week the issue names, against a fake GitHub holding `runs`."""
    fake = f"#!{sys.executable} -IS\n{FAKE_GITHUB.read_text()}"
    stubs = fake_bin(tmp_path / "bin", gh=fake, ghread=fake)
    (stubs / "world.json").write_text(json.dumps(runs))
    argv = [sys.executable, str(SCRIPT), "--since", SINCE, "--until", UNTIL]
    done = run(argv, cwd=REPO, stub_bin=stubs)
    assert done.returncode == 0, (
        f"ci_failures.py exited {done.returncode}: {done.stderr}"
    )
    return done


def _rerun_green_on_master() -> dict:
    """Run 42041106424's shape: red on attempt 1, green on attempt 2."""
    return _run(
        42041106424,
        _attempt(
            _job(
                127294768714,
                "host_packages (24.04, 3.12)",
                _step("Run the updater cases in systemd containers", number=9),
            ),
            _job(
                127309241974,
                "prek (lint + validate + tests + secrets)",
                _step("Require every half to have succeeded", number=2),
            ),
        ),
        _green_attempt(),
    )


def test_run_green_on_rerun_is_reported_red_on_attempt_1(tmp_path):
    out = _survey(tmp_path, _rerun_green_on_master()).stdout
    lines = [line for line in out.splitlines() if "42041106424" in line]
    assert lines, f"run 42041106424 is missing from the survey:\n{out}"
    attempts = {
        int(number)
        for line in lines
        for number in re.findall(r"attempts?\W{0,3}(\d+)", line, re.IGNORECASE)
    }
    assert 1 in attempts, f"want the run red on attempt 1, got {attempts}:\n{out}"


def test_render_diff_pr_body_echo_is_clean(tmp_path):
    # The runner prints an env value's later lines bare, with no timestamp.
    judge = _step(
        "Judge the difference against the Render-diff line",
        '##[group]Run cd "$JOB_TREE"',
        "shell: /usr/bin/bash -e {0}",
        "env:",
        "  PR_BODY: ## What changed, and why",
    )
    judge["lines"] += [
        "",
        "FAILED tests/repo/test_quoted_in_pr_body.py::test_echoed - assert 1 == 2",
        "",
        *_stamped("##[endgroup]", "##[error]Process completed with exit code 1."),
    ]
    render_diff = _run(
        42241533482,
        _attempt(_job(127000000001, "render-diff", judge)),
        name="render-diff",
        branch="fix-render",
        event="pull_request",
    )
    out = _survey(tmp_path, render_diff).stdout
    assert "render-diff" in out, f"the red render-diff job is missing:\n{out}"
    assert "test_quoted_in_pr_body" not in out, (
        f"a PR-body echo counted as a failure:\n{out}"
    )


def test_pytest_failed_line_is_flagged(tmp_path):
    out = _survey(
        tmp_path,
        _run(43000000001, _attempt(_pytest_job(127000000002, "test_real_failure"))),
    ).stdout
    assert "test_real_failure" in out, f"a real pytest failure is missing:\n{out}"


def test_every_red_attempt_is_read_for_its_own_log(tmp_path):
    three_attempts = _run(
        43000000002,
        _attempt(_pytest_job(127000000003, "test_first_attempt_only")),
        _attempt(_pytest_job(127000000004, "test_second_attempt_only")),
        _green_attempt(),
    )
    out = _survey(tmp_path, three_attempts).stdout
    assert "test_first_attempt_only" in out, f"attempt 1's log was not read:\n{out}"
    assert "test_second_attempt_only" in out, f"attempt 2's log was not read:\n{out}"


def test_only_a_failed_steps_log_counts(tmp_path):
    job = _job(
        127000000005,
        "pytest",
        _step(
            "Run the flaky retry",
            "FAILED tests/repo/test_in_a_passing_step.py::test_case - assert 1 == 2",
            number=3,
            conclusion="success",
        ),
        _step(
            "Run the suite",
            "FAILED tests/repo/test_in_the_failed_step.py::test_case - assert 1 == 2",
        ),
    )
    out = _survey(tmp_path, _run(43000000003, _attempt(job))).stdout
    assert "test_in_the_failed_step" in out, (
        f"the failed step's log was not read:\n{out}"
    )
    assert "test_in_a_passing_step" not in out, (
        f"a passing step's log was counted:\n{out}"
    )


def test_log_with_terminal_escapes_is_read_and_not_echoed(tmp_path):
    coloured = _job(
        127000000006,
        "pytest",
        _step(
            "Run the suite",
            "\x1b[31mFAILED\x1b[0m tests/repo/test_coloured.py::test_case - assert 0",
        ),
    )
    out = _survey(tmp_path, _run(43000000004, _attempt(coloured))).stdout
    assert "test_coloured" in out, f"the coloured FAILED line was not parsed:\n{out}"
    assert "\x1b" not in out, f"terminal escapes reached the report: {out!r}"


def test_red_run_past_the_search_cap_is_reported(tmp_path):
    # 1,010 newer runs push the oldest past the cap on one week-wide search. Green on re-run,
    # it also stays out of a status=failure search, which reads the latest attempt.
    start = datetime(2026, 10, 30, 1, tzinfo=timezone.utc)
    gap = timedelta(days=7) / 1100
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
        _attempt(_pytest_job(127000000007, "test_past_the_cap")),
        _green_attempt(),
        created=start.strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    out = _survey(tmp_path, oldest, *filler).stdout
    assert "test_past_the_cap" in out, (
        f"a run past the 1,000-result cap was lost:\n{out[:2000]}"
    )


def test_tally_row_carries_job_step_module_branches_and_last_seen(tmp_path):
    def red(run_id: int, branch: str, day: str, module: str) -> dict:
        return _run(
            run_id,
            _attempt(_pytest_job(run_id + 90_000_000_000, module)),
            branch=branch,
            created=f"2026-11-{day}T10:00:00Z",
            event="pull_request",
        )

    out = _survey(
        tmp_path,
        red(45000000001, "fix-alpha", "01", "test_tallied"),
        red(45000000002, "fix-beta", "04", "test_tallied"),
        red(45000000003, "fix-alpha", "03", "test_tallied"),
        red(45000000004, "fix-gamma", "02", "test_other"),
    ).stdout
    rows = [
        line
        for line in out.splitlines()
        if all(part in line for part in ("pytest", "Run the suite", "test_tallied"))
        and re.search(r"(?<!\d)3(?!\d)", line)
    ]
    assert len(rows) == 1, f"want one row tallying test_tallied 3 times:\n{out}"
    row = rows[0]
    assert "2026-11-04" in row, f"last-seen is the latest failure's date: {row}"
    assert "2026-11-03" not in row, row
    assert re.search(r"(?<!\d)2(?!\d)", row) or (
        row.count("fix-alpha") == 1 and "fix-beta" in row
    ), f"the row does not show two distinct branches: {row}"
    assert "fix-gamma" not in row and "test_other" not in row, (
        f"two modules merged: {row}"
    )


def test_flaky_section_tallies_only_runs_green_on_a_rerun(tmp_path):
    flaky = _run(
        46000000001,
        _attempt(_pytest_job(96000000001, "test_flaky")),
        _green_attempt(),
        event="pull_request",
    )
    still_red = _run(
        46000000002,
        _attempt(_pytest_job(96000000002, "test_broken")),
        _attempt(_pytest_job(96000000003, "test_broken")),
        event="pull_request",
    )
    out = _survey(tmp_path, flaky, still_red).stdout
    head, found, rest = out.partition("flaky: red, then green on a re-run")
    assert found, f"the survey printed no flaky section:\n{out}"
    section = rest.partition("red attempts")[0]
    assert "test_flaky" in section, section
    assert "test_broken" not in section, section
    assert "test_broken" in head, f"the full tally lost the still-red run:\n{out}"


def test_each_red_attempt_names_its_failed_steps(tmp_path):
    other = _run(
        47000000001,
        _attempt(_pytest_job(97000000001, "test_named")),
        created="2026-11-05T15:00:00Z",
        event="pull_request",
    )
    out = _survey(tmp_path, _rerun_green_on_master(), other).stdout
    section = out.partition("\nred attempts\n")[2]
    blocks = re.split(r"\n(?=run \d+)", section.strip())
    by_run = {block.split()[1]: block for block in blocks}
    updater = by_run["42041106424"]
    assert (
        "host_packages (24.04, 3.12) / Run the updater cases in systemd containers"
        in updater
    ), f"run 42041106424 does not name its failed step:\n{section}"
    assert "test_named" not in updater, f"another run's row leaked in:\n{section}"
    assert (
        "pytest / Run the suite / tests/repo/test_named.py" in (by_run["47000000001"])
    ), section
