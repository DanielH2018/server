"""Tests for `probe.py sweep-agreement`.

The journal lines have the shape Loki returned them in from daniel-box on 2026-11-13. Each
sweep's lines come from `local_sweep.report`, the function the unit prints with, so a change to
what the unit prints reaches these tests.
"""

import os
import subprocess
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from ci_verdict import CI_GATE_NAME
from diagnostics.probe_lib import core, sweep_agreement
from diagnostics.probe_lib.sweep_agreement import (
    GATE_DAYS,
    GATE_HOSTED_REDS,
    GATE_SWEEPS,
    LIMIT,
)
from lib import yaml_fast
from lib.exit_codes import (
    PROBE_FAILED,
    PROBE_NOT_APPLICABLE,
    PROBE_OK,
    PROBE_UNREADABLE,
)
from lib.repo_paths import REPO

import local_sweep
from local_sweep import Check, Sweep

NOW = 1794585600.0  # 2026-11-13 16:00 UTC
SHA = "0ef9986e19be3fc3703f44ae084f8cab81243e48"
TREE = "fc8dba645c0a10aa949fdbe7c0a42c232056151b"
HOOKS = "hooks (lint + validate + secrets + docs)"
PYTEST_SKIPPED = "pytest (shard ${{ matrix.shard }} of ${{ strategy.job-total }})"
FAILED = "FAILED hosts/lib/tests/test_box_firewall_and_maintenance.py::test_reconcile"


def _sweep(pytest_verdict="pass", sha=SHA, seconds=344, run=None) -> Sweep:
    detail = (FAILED,) if pytest_verdict == "fail" else ()
    checks = (Check("hooks", "pass", 103), Check("pytest", pytest_verdict, 240, detail))
    return Sweep(sha, TREE, checks, seconds, run=run)


def _lines(lines, at: float, pid: int):
    """Loki rows for `lines` as one run printed them, a millisecond apart, ending at `at`."""
    head = f"2026-11-13T15:16:09.771638+00:00 daniel-box local-sweep[{pid}]: "
    first = int(at * 1e9) - len(lines) * 1_000_000
    return [(first + n * 1_000_000, head + line) for n, line in enumerate(lines, 1)]


def _rows(sweep: Sweep, at: float = NOW - 60, pid: int = 2058159):
    """The Loki rows of one run: every line the unit printed, under one pid."""
    return _lines(local_sweep.report(sweep), at, pid)


def _check_run(run_id: int, name: str, conclusion: str | None) -> dict:
    status = "completed" if conclusion else "in_progress"
    return {"id": run_id, "name": name, "status": status, "conclusion": conclusion}


def _runs(hooks="success", pytest_shard="success", gate="success", also=()):
    """One commit's check-runs. `pytest_shard` is what shard 3 concluded.

    `also` is more check-runs of the commit, as (id, name, conclusion).
    """
    runs = [_check_run(1, HOOKS, hooks)]
    if pytest_shard == "skipped":
        runs.append(_check_run(2, PYTEST_SKIPPED, "skipped"))
    else:
        runs += [
            _check_run(
                10 + shard,
                f"pytest (shard {shard} of 6)",
                pytest_shard if shard == 3 else "success",
            )
            for shard in range(1, 7)
        ]
    runs += [_check_run(*one) for one in also]
    return [*runs, _check_run(99, CI_GATE_NAME, gate)]


def _run(rows, hosted, capsys, since="8d"):
    """Run the probe over `rows`. `hosted` is the check-runs of each commit, or the jobs of
    each run id, and a read of anything else fails."""
    paths = {
        f"actions/runs/{key}/jobs"
        if isinstance(key, int)
        else f"commits/{key}/check-runs": runs
        for key, runs in hosted.items()
    }
    code = sweep_agreement.run_sweep_agreement(
        SimpleNamespace(since=since),
        read_rows=lambda seconds, now: rows,
        read_check_runs=paths.__getitem__,
        now=NOW,
    )
    return code, capsys.readouterr().out


def test_the_covered_jobs_are_the_ones_the_sweep_runs_and_their_names_match_ci():
    jobs = yaml_fast.safe_load((REPO / local_sweep.CI_WORKFLOW).read_text())["jobs"]
    covered = sweep_agreement.HOSTED_JOBS
    assert set(covered) == set(local_sweep.LOCAL)
    for job_id in jobs["prek"]["needs"]:
        name = str(jobs[job_id].get("name", job_id))
        owners = [job for job, prefix in covered.items() if name.startswith(prefix)]
        assert owners == ([job_id] if job_id in covered else []), name


@pytest.mark.parametrize(
    ("runs", "verdicts"),
    [
        # The proven path: both jobs skipped, and the gate passed them.
        (
            _runs(hooks="skipped", pytest_shard="skipped"),
            {"hooks": "proven", "pytest": "proven"},
        ),
        # A skip the gate did not pass proves nothing.
        (
            _runs(hooks="skipped", pytest_shard="skipped", gate="failure"),
            {"hooks": "none", "pytest": "none"},
        ),
        (_runs(pytest_shard="cancelled"), {"hooks": "pass", "pytest": "none"}),
        # A re-run shard has two check-runs under one name, and the newer one decides.
        (
            _runs(
                pytest_shard="failure", also=[(113, "pytest (shard 3 of 6)", "success")]
            ),
            {"hooks": "pass", "pytest": "pass"},
        ),
        # A commit with a proven run and a full run reads by the shards that ran.
        (
            _runs(also=[(2, PYTEST_SKIPPED, "skipped")]),
            {"hooks": "pass", "pytest": "pass"},
        ),
        # Any conclusion the gate would not pass is red, not only `failure`.
        (_runs(pytest_shard="neutral"), {"hooks": "pass", "pytest": "fail"}),
    ],
    ids=["proven", "skipped", "cancelled", "rerun", "proven-and-ran", "neutral"],
)
def test_a_commits_check_runs_reduce_to_one_verdict_per_covered_job(runs, verdicts):
    assert sweep_agreement.hosted(runs) == verdicts


def test_sweeps_that_agree_with_hosted_ci_pass(capsys):
    code, out = _run(_rows(_sweep()), {SHA: _runs(hooks="skipped")}, capsys)
    assert code == PROBE_OK
    assert out.startswith(
        "1 sweep, 0 of them of pull-request runs, over 0.0 days, p90 344s: 2 checks agree, "
        "0 red here only, 0 red hosted only, 0 not comparable; hosted CI ran 1 of the 2 "
        "compared itself, and 0 were red\n"
    )


def test_a_sweep_of_a_run_is_set_beside_that_runs_own_jobs(capsys):
    """The swept merge commit has no check-runs: GitHub hangs a run's on the head commit."""
    rows = _rows(_sweep(run=5))
    code, out = _run(rows, {5: _runs(pytest_shard="failure")}, capsys)
    assert code == PROBE_FAILED
    assert out.startswith("1 sweep, 1 of them of pull-request runs, ")
    assert "and 1 were red" in out
    assert f"  {SHA[:12]} of run 5  pytest  swept" in out


def test_a_commit_swept_twice_counts_once_by_its_newest_sweep(capsys):
    rows = [
        *_rows(_sweep("fail"), at=NOW - 3600, pid=1),
        *_rows(_sweep(), pid=2),
    ]
    _, out = _run(rows, {SHA: _runs()}, capsys)
    assert out.startswith("1 sweep, ")
    assert "2 checks agree, 0 red here only" in out


def test_a_sweep_red_where_hosted_is_green_is_listed_with_what_failed(capsys):
    code, out = _run(_rows(_sweep("fail")), {SHA: _runs()}, capsys)
    assert code == PROBE_OK
    assert f"  {SHA[:12]}  pytest  swept 2026-11-13 15:59Z\n" in out
    assert f"    {FAILED}\n" in out


def test_a_sweep_green_where_hosted_is_red_fails_the_probe(capsys):
    code, out = _run(_rows(_sweep()), {SHA: _runs(pytest_shard="failure")}, capsys)
    assert code == PROBE_FAILED
    assert f"  {SHA[:12]}  pytest  swept" in out
    assert "and 1 were red" in out


def test_a_run_that_died_lends_its_lines_to_no_later_sweep(capsys):
    """A died run prints no result line, and the kernel reuses its pid."""
    rows = [
        *_lines(["Traceback of another pid"], NOW - 600, pid=7),
        *_lines(["Traceback of this pid, hours ago"], NOW - 3 * 3600, pid=8),
        *_rows(_sweep("fail"), pid=8),
    ]
    _, out = _run(sorted(rows), {SHA: _runs()}, capsys)
    assert FAILED in out
    assert "Traceback" not in out


def test_a_window_with_no_sweep_is_not_applicable(capsys):
    assert _run([], {}, capsys)[0] == PROBE_NOT_APPLICABLE


def test_a_window_that_fills_the_query_limit_is_unreadable(capsys):
    rows = _rows(_sweep())
    other_units = [(0, "daniel-box other[1]: noise")] * (LIMIT - 1 - len(rows))

    assert _run(other_units + rows, {SHA: _runs()}, capsys)[0] == PROBE_OK
    with pytest.raises(core.Unreadable) as unreadable:
        _run([(0, "noise"), *other_units, *rows], {SHA: _runs()}, capsys)
    assert unreadable.value.code == PROBE_UNREADABLE


def test_check_runs_github_did_not_return_are_unreadable_and_say_why(capsys):
    def refused(sha):
        raise subprocess.CalledProcessError(1, ["gh", "api"], stderr="gh: HTTP 403")

    with pytest.raises(core.Unreadable) as unreadable:
        sweep_agreement.run_sweep_agreement(
            SimpleNamespace(since="8d"),
            read_rows=lambda seconds, now: _rows(_sweep()),
            read_check_runs=refused,
            now=NOW,
        )
    assert unreadable.value.code == PROBE_UNREADABLE
    assert "HTTP 403" in capsys.readouterr().err


def _week(
    count: int = GATE_SWEEPS,
    days: float = GATE_DAYS + 0.5,
    slow: int = 0,
    hosted_reds: int = GATE_HOSTED_REDS,
    missed: bool = False,
):
    """`count` sweeps over `days`, one commit each, that agree with hosted CI.

    Args:
        slow: How many took a second longer than the gate allows.
        hosted_reds: How many commits are red on both sides.
        missed: Whether one more commit is red hosted and green in the sweep.
    """
    step = days * 86400 / count
    sweeps, hosted = [], {}
    for n in range(count):
        verdict = "fail" if n < hosted_reds else "pass"
        sha = f"{n:040x}"
        seconds = 601 if n >= count - slow else 344
        at = NOW - days * 86400 + n * step
        sweeps.append(sweep_agreement.Swept(at, _sweep(verdict, sha, seconds), ()))
        hosted[sweeps[-1].hosted_at] = {"hooks": "proven", "pytest": verdict}
    if missed:
        hosted[sweeps[-1].hosted_at]["pytest"] = "fail"
    return sweep_agreement.compare(sweeps, hosted)


@pytest.mark.parametrize(
    ("week", "met"),
    [
        (_week(), True),
        (_week(count=GATE_SWEEPS - 1), False),
        (_week(days=GATE_DAYS - 0.5), False),
        # The p90 is the nearest rank, so a tenth of the sweeps may be slow and no more.
        (_week(slow=GATE_SWEEPS // 10), True),
        (_week(slow=GATE_SWEEPS // 10 + 1), False),
        # A window of green hosted runs cannot show that the sweep catches a red one.
        (_week(hosted_reds=GATE_HOSTED_REDS - 1), False),
        (_week(missed=True), False),
    ],
    ids=[
        "met",
        "too-few-checks",
        "too-few-days",
        "a-tenth-slow",
        "over-a-tenth-slow",
        "too-few-hosted-reds",
        "missed-a-red",
    ],
)
def test_b2s_gate_needs_days_checks_speed_hosted_reds_and_no_missed_red(week, met):
    assert week.gate_met(NOW) is met
