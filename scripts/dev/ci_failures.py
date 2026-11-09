#!/usr/bin/env python3
"""Survey CI failures across every attempt of every workflow run in a date range.

`gh run list` reports only a run's latest attempt, so a run that went red and passed on re-run
reads as green. This tool lists the runs in windows under the search cap, asks each re-run run
for its earlier attempts, and reads the log of each failed step. It prints three sections: a
tally by failing job, step and test module, with distinct branches and last-seen date; the same
tally over the flaky attempts alone, the red ones whose run went green on a later attempt of the
same commit; and one line per red attempt, followed by its failed job, step and module rows,
so a tally row leads to the run whose log to read. A red attempt is a `failure`, `timed_out` or
`startup_failure`; a `cancelled` attempt gave no verdict, so it does not count.

`gh run view --log-failed` usually labels every line `UNKNOWN STEP` and returns the whole failed
job. The tool then keeps the lines stamped inside the failed step's start and end seconds from
the jobs endpoint, so a passing step that shares a boundary second can still contribute a line.

It counts a test module from a `FAILED` or `ERROR` line in pytest's short-summary shape. It skips
a line with no runner timestamp and everything inside a step's `##[group]Run` header, because
that header echoes the step's env, and a step's env can carry a PR body. No log text
other than a module path reaches the report.

Usage:
    uv run python scripts/dev/ci_failures.py --since 2026-10-30 --until 2026-11-06

`--until` includes the named day and defaults to today (UTC). Run it with the sandbox off, like
`gh`.

Exit codes: 0 the report printed, even when a log had expired, and the report counts those; 1 a
`gh` call that lists runs, attempts or jobs failed (its stderr is printed); 64 a bad flag.
"""

import json
import re
import subprocess
import sys
from collections import Counter, defaultdict
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path as _Path
from typing import Any

# Reach `lib`: a directly-invoked script gets only its own directory on sys.path, and
# pyproject's `pythonpath` is a pytest setting.
sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

from lib.cli_help import ArgumentParser, warn
from lib.exit_codes import FAILED, OK
from lib.gh import gh
from lib.repo_paths import REPO

ACTIONS = "repos/{owner}/{repo}/actions"
SEARCH_CAP = 1000
START_WINDOW = timedelta(hours=12)
ONE_SECOND = timedelta(seconds=1)
RED = frozenset({"failure", "timed_out", "startup_failure"})
# `gh` prints each ESC byte of a log as a caret pair.
ANSI = re.compile(r"(?:\x1b|\^\[)\[[0-9;?]*[A-Za-z]")
STAMPED = re.compile(r"\ufeff?(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)\.\d+Z ?(.*)", re.DOTALL)
PYTEST_SUMMARY = re.compile(r"(?:FAILED|ERROR) (\S+?\.py)(?=::|\s|$)")
UNKNOWN_STEP = "UNKNOWN STEP"
NO_VALUE = "-"
LOG_TIMEOUT = 300.0
READERS = 8

type Logged = list[tuple[str, str]]


def _api_pages(path: str) -> list[dict[str, Any]]:
    """Return every page of a REST GET, with the query in `path` so `gh` keeps it a GET."""
    done = gh("api", path, "--paginate", "--slurp", cwd=REPO, timeout=LOG_TIMEOUT)
    return json.loads(done.stdout)


def _stamp(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def runs_between(start: datetime, end: datetime) -> list[dict[str, Any]]:
    """List the runs created from `start` to `end`, both inclusive, splitting past the cap."""
    query = f"created={_stamp(start)}..{_stamp(end)}&per_page=100"
    pages = _api_pages(f"{ACTIONS}/runs?{query}")
    if pages[0]["total_count"] < SEARCH_CAP:
        return [run for page in pages for run in page["workflow_runs"]]
    if end - start <= ONE_SECOND:
        warn(
            f"{_stamp(start)} reaches the {SEARCH_CAP}-run search cap; runs may be missing"
        )
        return [run for page in pages for run in page["workflow_runs"]]
    middle = (start + (end - start) / 2).replace(microsecond=0)
    return runs_between(start, middle) + runs_between(middle + ONE_SECOND, end)


def runs_in_range(since: date, until: date) -> list[dict[str, Any]]:
    """List every run created from the start of `since` to the end of `until`."""
    start = datetime.combine(since, time.min, UTC)
    last = datetime.combine(until, time.max, UTC).replace(microsecond=0)
    runs: dict[int, dict[str, Any]] = {}
    while start <= last:
        end = min(start + START_WINDOW - ONE_SECOND, last)
        runs.update((run["id"], run) for run in runs_between(start, end))
        start = end + ONE_SECOND
    return list(runs.values())


def red_attempts(run: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the run's red attempts as run objects; the search result is its latest attempt."""
    earlier = [
        _api_pages(f"{ACTIONS}/runs/{run['id']}/attempts/{number}")[0]
        for number in range(1, run["run_attempt"])
    ]
    return [attempt for attempt in [*earlier, run] if attempt["conclusion"] in RED]


def failed_steps(attempt: dict[str, Any]) -> list[tuple[str, dict[str, Any] | None]]:
    """Return (job name, failed step) pairs; a red job with no failed step carries None."""
    pages = _api_pages(
        f"{ACTIONS}/runs/{attempt['id']}/attempts/{attempt['run_attempt']}/jobs?per_page=100"
    )
    found: list[tuple[str, dict[str, Any] | None]] = []
    for job in (job for page in pages for job in page["jobs"]):
        if job["conclusion"] not in RED:
            continue
        steps = [s for s in job.get("steps", []) if s["conclusion"] in RED]
        found += [(job["name"], step) for step in steps] or [(job["name"], None)]
    return found


def read_log(attempt: dict[str, Any]) -> dict[str, Logged] | None:
    """Return each job's (step label, log text) lines, or None when `gh` cannot read the log."""
    done = gh(
        "run",
        "view",
        str(attempt["id"]),
        "--attempt",
        str(attempt["run_attempt"]),
        "--log-failed",
        check=False,
        cwd=REPO,
        timeout=LOG_TIMEOUT,
    )
    if done.returncode != 0:
        return None
    by_job: dict[str, Logged] = defaultdict(list)
    for line in done.stdout.split("\n"):
        columns = line.split("\t", 2)
        if len(columns) == 3:
            by_job[columns[0]].append((columns[1], columns[2]))
    return by_job


def step_lines(logged: Logged, step: dict[str, Any]) -> list[str]:
    """Return the log text of one failed step from its job's lines.

    Lines labelled with the step's name win. Otherwise the `UNKNOWN STEP` lines count when their
    timestamp falls in the step's start and end seconds, which the jobs endpoint truncates.
    """
    named = [text for label, text in logged if label == step["name"]]
    if named:
        return named
    first = (step.get("started_at") or "")[:19]
    last = (step.get("completed_at") or "9999")[:19]
    return [
        text
        for label, text in logged
        if label == UNKNOWN_STEP
        and (stamped := STAMPED.match(text))
        and first <= stamped[1] <= last
    ]


def failing_modules(lines: Iterable[str]) -> set[str]:
    """Return the test modules a step's log reports in pytest's short-summary shape."""
    modules: set[str] = set()
    in_header = False
    for line in lines:
        stamped = STAMPED.match(ANSI.sub("", line).rstrip("\r"))
        if not stamped:
            continue
        text = stamped[2]
        if text.startswith("##[group]Run "):
            in_header = True
        elif text.startswith("##[endgroup]"):
            in_header = False
        elif not in_header and (found := PYTEST_SUMMARY.match(text)):
            modules.add(found[1])
    return modules


def read_attempt(attempt: dict[str, Any]) -> tuple[list[tuple[str, str, str]], bool]:
    """Return the attempt's (job, step, module) rows, and whether its log was readable."""
    log = read_log(attempt)
    rows: list[tuple[str, str, str]] = []
    for job, step in failed_steps(attempt):
        if step is None:
            rows.append((job, NO_VALUE, NO_VALUE))
            continue
        modules = failing_modules(step_lines((log or {}).get(job, []), step))
        rows += [
            (job, step["name"], module) for module in sorted(modules or {NO_VALUE})
        ]
    return rows, log is not None


def _tally(
    attempts: Iterable[dict[str, Any]],
    read: Iterable[tuple[list[tuple[str, str, str]], bool]],
) -> list[str]:
    """Return one line per (job, step, module) row: its count, branches and last-seen date."""
    counts: Counter[tuple[str, str, str]] = Counter()
    branches: dict[tuple[str, str, str], set[str]] = defaultdict(set)
    last_seen: dict[tuple[str, str, str], str] = {}
    for attempt, (rows, _) in zip(attempts, read, strict=True):
        day = attempt["run_started_at"][:10]
        for row in rows:
            counts[row] += 1
            branches[row].add(attempt["head_branch"])
            last_seen[row] = max(last_seen.get(row, ""), day)
    return [
        f"{count:>5}  {len(branches[row]):>8}  {last_seen[row]}  {' / '.join(row)}"
        for row, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    ]


def _passed_on_rerun(attempt: dict[str, Any], final: dict[str, Any]) -> bool:
    """Whether a later attempt of the same run, so the same commit, ended green."""
    return (
        final["run_attempt"] > attempt["run_attempt"]
        and final["conclusion"] == "success"
    )


def render(
    since: date,
    until: date,
    scanned: int,
    attempts: list[dict[str, Any]],
    read: list[tuple[list[tuple[str, str, str]], bool]],
    latest: dict[int, dict[str, Any]],
) -> list[str]:
    """Render the report: a header, the tally, the flake tally, then each red attempt's rows."""
    runs = {attempt["id"] for attempt in attempts}
    unread = sum(not readable for _, readable in read)
    out = [
        f"{len(attempts)} red attempts in {len(runs)} of {scanned} runs, {since} to {until}",
    ]
    if unread:
        out.append(f"{unread} red attempts had no readable log, so they show no module")
    header = "count  branches  last seen   job / step / module"
    out += ["", header, *_tally(attempts, read)]
    flaky = [
        (attempt, entry)
        for attempt, entry in zip(attempts, read, strict=True)
        if _passed_on_rerun(attempt, latest[attempt["id"]])
    ]
    out += ["", "flaky: red, then green on a re-run of the same commit", header]
    out += _tally([a for a, _ in flaky], [entry for _, entry in flaky])
    out += ["", "red attempts"]
    ordered = sorted(
        zip(attempts, read, strict=True),
        key=lambda entry: (entry[0]["run_started_at"], entry[0]["id"]),
    )
    for attempt, (rows, _) in ordered:
        final = latest[attempt["id"]]
        later = (
            f"  {final['conclusion']} on attempt {final['run_attempt']}"
            if final["run_attempt"] != attempt["run_attempt"]
            else ""
        )
        out.append(
            f"run {attempt['id']} attempt {attempt['run_attempt']}"
            f"  {attempt['run_started_at'][:10]}  {attempt['head_branch']}"
            f"  {attempt['name']}{later}"
        )
        out += [f"    {' / '.join(row)}" for row in rows]
    return out


def survey(
    since: date, until: date
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[tuple[list[tuple[str, str, str]], bool]],
]:
    """Return the runs in the range, their red attempts, and each attempt's rows and readability.

    Raises `CalledProcessError` or `TimeoutExpired` when a `gh` call that lists runs, attempts
    or jobs fails.
    """
    runs = runs_in_range(since, until)
    needing_a_read = [r for r in runs if r["run_attempt"] > 1 or r["conclusion"] in RED]
    with ThreadPoolExecutor(READERS) as pool:
        attempts = [
            a for found in pool.map(red_attempts, needing_a_read) for a in found
        ]
        read = list(pool.map(read_attempt, attempts))
    return runs, attempts, read


def failing_test_modules(since: date, until: date) -> tuple[set[str], int]:
    """Return every test module a red attempt in the range reported failing.

    Also return how many red attempts had no readable log: a module that failed only in one
    of those is missing from the set.
    """
    _, _, read = survey(since, until)
    modules = {
        module for rows, _ in read for _, _, module in rows if module != NO_VALUE
    }
    return modules, sum(not readable for _, readable in read)


def main(argv: list[str] | None = None) -> int:
    parser = ArgumentParser(description=__doc__.partition("\n")[0])
    parser.add_argument("--since", type=date.fromisoformat, required=True)
    parser.add_argument(
        "--until", type=date.fromisoformat, default=datetime.now(UTC).date()
    )
    args = parser.parse_args(argv)
    try:
        runs, attempts, read = survey(args.since, args.until)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        print(f"gh failed: {exc.stderr or exc}", file=sys.stderr)
        return FAILED
    latest = {run["id"]: run for run in runs}
    print("\n".join(render(args.since, args.until, len(runs), attempts, read, latest)))
    return OK


if __name__ == "__main__":
    sys.exit(main())
