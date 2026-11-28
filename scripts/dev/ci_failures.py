#!/usr/bin/env python3
"""Survey CI failures across every attempt of every workflow run in a date range.

`gh run list` reports only a run's latest attempt, so a run that went red and passed on re-run
reads as green. This tool lists the runs in windows under the search cap, asks each re-run run
for its earlier attempts, and reads the log of each failed step. After a count line it prints a
tally by failing job, step and test module, with distinct branches and last-seen date, and the
same tally over the flaky attempts alone: the red ones whose run went green on a later attempt
of the same commit. An empty flaky tally prints as one `0 flaky` line. `--all` adds one line per
red attempt, followed by its failed job, step and module rows, so a tally row leads to the run
whose log to read. The last line is `CI-FAILURES: red=<n> flaky=<n>` for a caller to read. A
red attempt is a `failure`, `timed_out` or
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
    uv run python scripts/dev/ci_failures.py --since 2026-11-13 --workflow ci.yml

`--until` includes the named day and defaults to today (UTC). `--workflow` surveys one workflow
file's runs. A week of every workflow's runs costs several times the API calls a week of `ci.yml`
alone does, so a flake survey of one workflow passes it. Run it with the sandbox off, like `gh`.

Exit codes: 0 the report printed, even when a log had expired, and the report counts those; 1 a
`gh` call that lists runs, attempts or jobs failed, and stderr carries the last line of `gh`'s
stderr; 64 a bad flag.
"""

import json
import re
import subprocess
import sys
from collections import Counter, defaultdict
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, time, timedelta
from typing import Any


from lib.cli_help import ArgumentParser, warn
from lib.exit_codes import FAILED, OK
from lib.gh import gh
from lib.gh_rate_limit import is_rate_limited
from lib.repo_paths import REPO

from lib import host_modules  # noqa: F401
from ci_verdict import WorkflowRun

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


def runs_between(start: datetime, end: datetime, listing: str) -> list[WorkflowRun]:
    """List the runs `listing` returns created from `start` to `end`, splitting past the cap.

    `listing` is a runs endpoint whose query already holds its filters. Both bounds are
    inclusive.
    """
    pages = _api_pages(f"{listing}&created={_stamp(start)}..{_stamp(end)}")
    runs = [WorkflowRun.parse(run) for page in pages for run in page["workflow_runs"]]
    if pages[0]["total_count"] < SEARCH_CAP:
        return runs
    if end - start <= ONE_SECOND:
        warn(
            f"{_stamp(start)} reaches the {SEARCH_CAP}-run search cap; runs may be missing"
        )
        return runs
    middle = (start + (end - start) / 2).replace(microsecond=0)
    return runs_between(start, middle, listing) + runs_between(
        middle + ONE_SECOND, end, listing
    )


def runs_in_range(
    since: date, until: date, branch: str | None = None, workflow: str | None = None
) -> list[WorkflowRun]:
    """List every run created from the start of `since` to the end of `until`.

    A `branch` keeps only the runs of that head branch, and a `workflow` file name, such as
    `ci.yml`, only the runs of that workflow.
    """
    listing = f"{ACTIONS}/workflows/{workflow}/runs" if workflow else f"{ACTIONS}/runs"
    listing += "?per_page=100" + (f"&branch={branch}" if branch else "")
    start = datetime.combine(since, time.min, UTC)
    last = datetime.combine(until, time.max, UTC).replace(microsecond=0)
    runs: dict[int | None, WorkflowRun] = {}
    while start <= last:
        end = min(start + START_WINDOW - ONE_SECOND, last)
        runs.update((run.id, run) for run in runs_between(start, end, listing))
        start = end + ONE_SECOND
    return list(runs.values())


def red_attempts(run: WorkflowRun) -> list[WorkflowRun]:
    """Return the run's red attempts as runs; the search result is its latest attempt."""
    earlier = [
        WorkflowRun.parse(_api_pages(f"{ACTIONS}/runs/{run.id}/attempts/{number}")[0])
        for number in range(1, run.attempt)
    ]
    return [attempt for attempt in [*earlier, run] if attempt.conclusion in RED]


def failed_steps(attempt: WorkflowRun) -> list[tuple[str, dict[str, Any] | None]]:
    """Return (job name, failed step) pairs; a red job with no failed step carries None."""
    pages = _api_pages(
        f"{ACTIONS}/runs/{attempt.id}/attempts/{attempt.attempt}/jobs?per_page=100"
    )
    found: list[tuple[str, dict[str, Any] | None]] = []
    for job in (job for page in pages for job in page["jobs"]):
        if job["conclusion"] not in RED:
            continue
        steps = [s for s in job.get("steps", []) if s["conclusion"] in RED]
        found += [(job["name"], step) for step in steps] or [(job["name"], None)]
    return found


def read_log(attempt: WorkflowRun) -> dict[str, Logged] | None:
    """Return each job's (step label, log text) lines, or None when `gh` cannot read the log."""
    done = gh(
        "run",
        "view",
        str(attempt.id),
        "--attempt",
        str(attempt.attempt),
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


def read_attempt(attempt: WorkflowRun) -> tuple[list[tuple[str, str, str]], bool]:
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
    attempts: Iterable[WorkflowRun],
    read: Iterable[tuple[list[tuple[str, str, str]], bool]],
) -> list[str]:
    """Return one line per (job, step, module) row: its count, branches and last-seen date."""
    counts: Counter[tuple[str, str, str]] = Counter()
    branches: dict[tuple[str, str, str], set[str]] = defaultdict(set)
    last_seen: dict[tuple[str, str, str], str] = {}
    for attempt, (rows, _) in zip(attempts, read, strict=True):
        day = attempt.started_at.date().isoformat()
        for row in rows:
            counts[row] += 1
            branches[row].add(attempt.head_branch)
            last_seen[row] = max(last_seen.get(row, ""), day)
    return [
        f"{count:>5}  {len(branches[row]):>8}  {last_seen[row]}  {' / '.join(row)}"
        for row, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    ]


def _passed_on_rerun(attempt: WorkflowRun, final: WorkflowRun) -> bool:
    """Whether a later attempt of the same run, so the same commit, ended green."""
    return final.attempt > attempt.attempt and final.conclusion == "success"


def render(
    since: date,
    until: date,
    scanned: int,
    attempts: list[WorkflowRun],
    read: list[tuple[list[tuple[str, str, str]], bool]],
    latest: dict[int | None, WorkflowRun],
    show_all: bool = False,
) -> list[str]:
    """Render the report: a count line, the tally, the flake tally, then a `CI-FAILURES:` line.

    `show_all` adds each red attempt and its own rows before the `CI-FAILURES:` line.
    """
    runs = {attempt.id for attempt in attempts}
    unread = sum(not readable for _, readable in read)
    hint = "; --all to list each" if attempts and not show_all else ""
    out = [
        f"{len(attempts)} red attempts in {len(runs)} of {scanned} runs,"
        f" {since} to {until}{hint}",
    ]
    if unread:
        out.append(f"{unread} red attempts had no readable log, so they show no module")
    header = "count  branches  last seen   job / step / module"
    if attempts:
        out += ["", header, *_tally(attempts, read)]
    flaky = [
        (attempt, entry)
        for attempt, entry in zip(attempts, read, strict=True)
        if _passed_on_rerun(attempt, latest[attempt.id])
    ]
    out += ["", f"{len(flaky)} flaky: red, then green on a re-run of the same commit"]
    if flaky:
        out += [header, *_tally([a for a, _ in flaky], [entry for _, entry in flaky])]
    if show_all and attempts:
        out += ["", "red attempts", *_attempt_lines(attempts, read, latest)]
    out += ["", f"CI-FAILURES: red={len(attempts)} flaky={len(flaky)}"]
    return out


def _attempt_lines(
    attempts: list[WorkflowRun],
    read: list[tuple[list[tuple[str, str, str]], bool]],
    latest: dict[int | None, WorkflowRun],
) -> list[str]:
    """Return one line per red attempt, oldest first, each followed by its own rows."""
    out: list[str] = []
    ordered = sorted(
        zip(attempts, read, strict=True),
        key=lambda entry: (entry[0].started_at, entry[0].id or 0),
    )
    for attempt, (rows, _) in ordered:
        final = latest[attempt.id]
        later = (
            f"  {final.conclusion} on attempt {final.attempt}"
            if final.attempt != attempt.attempt
            else ""
        )
        out.append(
            f"run {attempt.id} attempt {attempt.attempt}"
            f"  {attempt.started_at.date().isoformat()}  {attempt.head_branch}"
            f"  {attempt.name}{later}"
        )
        out += [f"    {' / '.join(row)}" for row in rows]
    return out


def gh_failure(exc: subprocess.CalledProcessError | subprocess.TimeoutExpired) -> str:
    """Return one line naming why a `gh` call failed: the last line of its stderr.

    A rate-limit refusal also names `gh-quota status`, which shows the session spending it.
    """
    stderr = exc.stderr or ""
    if isinstance(stderr, bytes):
        stderr = stderr.decode(errors="replace")
    lines = [line.strip() for line in stderr.splitlines() if line.strip()]
    why = lines[-1] if lines else str(exc).splitlines()[0]
    if is_rate_limited(stderr) or re.search(r"\bHTTP (?:403|429)\b", stderr):
        why += "; run `gh-quota status` to see which session spends the quota"
    return f"gh failed: {why}"


def survey(
    since: date, until: date, branch: str | None = None, workflow: str | None = None
) -> tuple[
    list[WorkflowRun],
    list[WorkflowRun],
    list[tuple[list[tuple[str, str, str]], bool]],
]:
    """Return the runs in the range, their red attempts, and each attempt's rows and readability.

    A `branch` keeps only the runs of that head branch, and a `workflow` file name only the runs
    of that workflow.

    Raises `CalledProcessError` or `TimeoutExpired` when a `gh` call that lists runs, attempts
    or jobs fails.
    """
    runs = runs_in_range(since, until, branch, workflow)
    needing_a_read = [r for r in runs if r.attempt > 1 or r.conclusion in RED]
    with ThreadPoolExecutor(READERS) as pool:
        attempts = [
            a for found in pool.map(red_attempts, needing_a_read) for a in found
        ]
        read = list(pool.map(read_attempt, attempts))
    return runs, attempts, read


def failing_test_modules(
    since: date, until: date, branch: str | None = None
) -> tuple[set[str], int]:
    """Return every test module a red attempt in the range reported failing.

    A `branch` counts only the runs of that head branch.

    Also return how many red attempts had no readable log: a module that failed only in one
    of those is missing from the set.
    """
    _, _, read = survey(since, until, branch)
    modules = {
        module for rows, _ in read for _, _, module in rows if module != NO_VALUE
    }
    return modules, sum(not readable for _, readable in read)


def main(argv: list[str] | None = None) -> int:
    parser = ArgumentParser(description=(__doc__ or "").partition("\n")[0])
    parser.add_argument("--since", type=date.fromisoformat, required=True)
    parser.add_argument(
        "--until", type=date.fromisoformat, default=datetime.now(UTC).date()
    )
    parser.add_argument(
        "--all", action="store_true", help="also list each red attempt and its rows"
    )
    parser.add_argument(
        "--workflow",
        help="survey only this workflow's runs, by file name, such as ci.yml",
    )
    args = parser.parse_args(argv)
    try:
        runs, attempts, read = survey(args.since, args.until, workflow=args.workflow)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        print(gh_failure(exc), file=sys.stderr)
        return FAILED
    latest = {run.id: run for run in runs}
    report = render(args.since, args.until, len(runs), attempts, read, latest, args.all)
    print("\n".join(report))
    return OK


if __name__ == "__main__":
    sys.exit(main())
