#!/usr/bin/env python3
"""What a CI run cost the runner pool: slot-seconds, queue waits and the pytest legs' setup.

Run: uv run python scripts/dev/ci_slot_cost.py RUN_ID [RUN_ID ...]
     uv run python scripts/dev/ci_slot_cost.py --recent 20   # newest green master runs

WHY. The account runs at most 20 hosted jobs at once, and a full sweep of ci.yml asks for 25.
One run's wall in isolation therefore misses what an extra job costs: every job holds a runner
slot for its whole duration, setup included, and every other run queued behind the pool waits
for that slot. The shard counts in ci.yml are chosen on the two numbers this prints (#8456),
and the per-leg setup it prints is what a cut to the setup steps is measured by (#8457).

For each run it prints:

- the slot-seconds, summed over every job that ran, then per job family (the name before
  ` (`, so the six `pytest (shard N of 6)` legs are one `pytest` row);
- the queue wait at p50 and p90 over the run's jobs, and each family's median wait;
- the median time a pytest leg spent outside `Run the test suite`, and that time per step.

A job's queue wait is `started_at - created_at`. GitHub creates a job's record when the jobs
it `needs` have finished, not when the run starts, so `created_at` already marks the moment
it became ready (checked on master run 43594559130: every `host_packages_bootstrap` leg was
created when `host_packages_build` completed). A skipped job never held a runner and is left
out.

`--recent N` reads the newest N green `push` runs on master that ran at least one pytest leg,
then prints one line per run and a pooled block over all of them; `--all` prints each run's
full block instead of its line.
"""

import re
import statistics
import sys as _sys
from datetime import datetime
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))  # scripts/

from lib.cli_help import ArgumentParser, answer_help
from lib.gh import gh_json
from lib.json_types import as_object, as_object_list

CI_WORKFLOW = "ci.yml"
CI_BRANCH = "master"
PYTEST_PREFIX = "pytest (shard"
SUITE_STEP = "Run the test suite"
# How many green master runs `--recent` lists to find N that ran pytest. A push whose tree a
# landing already proved skips every leg.
RUN_WINDOW = 60

_ACTION_SHA = re.compile(r"@[0-9a-f]{40}\b")


def _epoch(iso: str | None) -> float | None:
    return (
        datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() if iso else None
    )


def job_spans(jobs: list[dict]) -> list[dict]:
    """The jobs that held a runner, each as its name, family and three timestamps.

    Args:
        jobs: the `jobs` list of the Actions API's `runs/{id}/jobs` response.

    Returns:
        One dict per job that started and finished, with `name`, `family`, `queued`,
        `started` and `finished` (epoch seconds) and its raw `steps`. A skipped job is left
        out: the API stamps it as finishing before it started.
    """
    spans = []
    for job in jobs:
        queued, started = _epoch(job.get("created_at")), _epoch(job.get("started_at"))
        finished = _epoch(job.get("completed_at"))
        if job.get("conclusion") == "skipped":
            continue
        if queued is None or started is None or finished is None or finished < started:
            continue
        name = str(job.get("name", ""))
        spans.append(
            {
                "name": name,
                "family": name.split(" (", 1)[0],
                "queued": queued,
                "started": started,
                "finished": finished,
                "steps": job.get("steps") or [],
            }
        )
    return spans


def p90(values: list[float]) -> float:
    """The 90th percentile by linear interpolation between the closest ranks."""
    if len(values) == 1:
        return values[0]
    return statistics.quantiles(values, n=10, method="inclusive")[-1]


def leg_setup(span: dict) -> tuple[float, dict[str, float]] | None:
    """A pytest leg's seconds outside `Run the test suite`, and each other step's seconds.

    Returns None for a leg whose suite step never ran, since it measured no setup.
    """
    suite, steps = None, {}
    for step in span["steps"]:
        began, ended = _epoch(step.get("started_at")), _epoch(step.get("completed_at"))
        if began is None or ended is None:
            continue
        name = _ACTION_SHA.sub("", str(step.get("name", "")))
        if name == SUITE_STEP:
            suite = ended - began
        else:
            steps[name] = steps.get(name, 0.0) + ended - began
    if suite is None:
        return None
    return span["finished"] - span["started"] - suite, steps


def run_cost(jobs: list[dict]) -> dict:
    """One run's slot-seconds, queue waits and pytest-leg setup, from its jobs list."""
    spans = job_spans(jobs)
    by_family: dict[str, list[dict]] = {}
    for span in spans:
        by_family.setdefault(span["family"], []).append(span)
    legs = [
        setup
        for span in spans
        if span["name"].startswith(PYTEST_PREFIX)
        if (setup := leg_setup(span)) is not None
    ]
    return {
        "jobs": len(spans),
        "slot_s": sum(s["finished"] - s["started"] for s in spans),
        "waits": [s["started"] - s["queued"] for s in spans],
        "families": {
            family: {
                "jobs": len(members),
                "slot_s": sum(s["finished"] - s["started"] for s in members),
                "wait_median_s": statistics.median(
                    s["started"] - s["queued"] for s in members
                ),
            }
            for family, members in by_family.items()
        },
        "leg_outside_s": [outside for outside, _ in legs],
        "leg_steps": [steps for _, steps in legs],
    }


def _wait_line(waits: list[float]) -> str:
    if not waits:
        return "queue wait: no job ran"
    return f"queue wait p50 {statistics.median(waits):.0f}s, p90 {p90(waits):.0f}s"


def _setup_lines(outside: list[float], steps: list[dict[str, float]]) -> list[str]:
    if not outside:
        return ["pytest legs: none ran the suite"]
    names = {name for leg in steps for name in leg}
    medians = sorted(
        ((statistics.median(leg.get(n, 0.0) for leg in steps), n) for n in names),
        reverse=True,
    )
    return [
        f"pytest legs: {len(outside)}, median {statistics.median(outside):.1f}s outside "
        f"'{SUITE_STEP}'"
    ] + [f"  {seconds:6.1f}s  {name}" for seconds, name in medians if seconds >= 0.5]


def head_line(run_id: int, cost: dict) -> str:
    """One run's totals on one line."""
    return (
        f"run {run_id}: {cost['jobs']} jobs, {cost['slot_s']:.0f} slot-seconds, "
        f"{_wait_line(cost['waits'])}"
    )


def _family_lines(families: dict[str, dict]) -> list[str]:
    return [
        f"  {f['slot_s']:6.0f} slot-s  {f['jobs']:>2.0f} job(s)  "
        f"median wait {f['wait_median_s']:4.0f}s  {family}"
        for family, f in sorted(families.items(), key=lambda kv: -kv[1]["slot_s"])
    ]


def render(run_id: int, cost: dict) -> str:
    """The report for one run: totals, one line per family by slot-seconds, the leg setup."""
    return "\n".join(
        [
            head_line(run_id, cost),
            *_family_lines(cost["families"]),
            *_setup_lines(cost["leg_outside_s"], cost["leg_steps"]),
        ]
    )


def render_pooled(costs: list[dict]) -> str:
    """One summary over several runs, each family's figures the median of its runs."""
    waits = [w for c in costs for w in c["waits"]]
    head = (
        f"{len(costs)} runs: median "
        f"{statistics.median(c['slot_s'] for c in costs):.0f} slot-seconds, "
        f"{_wait_line(waits)}"
    )
    names = {name for c in costs for name in c["families"]}
    families = {
        name: {
            key: statistics.median(
                c["families"][name][key] for c in costs if name in c["families"]
            )
            for key in ("jobs", "slot_s", "wait_median_s")
        }
        for name in names
    }
    outside = [o for c in costs for o in c["leg_outside_s"]]
    steps = [s for c in costs for s in c["leg_steps"]]
    return "\n".join([head, *_family_lines(families), *_setup_lines(outside, steps)])


def read_jobs(run_id: int, gh_json=gh_json) -> list[dict]:
    """The jobs of CI run `run_id`, from the Actions jobs API.

    Raises:
        ValueError: the response is not an object holding a `jobs` list of objects.
    """
    listed = gh_json(
        "api", f"repos/{{owner}}/{{repo}}/actions/runs/{run_id}/jobs?per_page=100"
    )
    return as_object_list(as_object(listed, "the jobs response").get("jobs"), "jobs")


def recent_runs(count: int, gh_json=gh_json) -> dict[int, list[dict]]:
    """The jobs of the newest `count` green master push runs that ran a pytest leg.

    Raises:
        RuntimeError: fewer than `count` of the newest `RUN_WINDOW` green runs ran pytest.
    """
    args = (
        f"run list --workflow {CI_WORKFLOW} --branch {CI_BRANCH} --event push"
        f" --status success --limit {RUN_WINDOW} --json databaseId"
    ).split()
    listed = as_object_list(gh_json(*args) or [], "the run list")
    found: dict[int, list[dict]] = {}
    for run in listed:
        if len(found) == count:
            break
        run_id = int(str(run["databaseId"]))
        jobs = read_jobs(run_id, gh_json)
        if any(s["name"].startswith(PYTEST_PREFIX) for s in job_spans(jobs)):
            found[run_id] = jobs
    if len(found) < count:
        raise RuntimeError(
            f"{len(found)} of the newest {len(listed)} green {CI_BRANCH} runs ran pytest, "
            f"not the {count} asked for"
        )
    return found


def main(argv: list[str] | None = None) -> int:
    answer_help(__doc__, argv)
    parser = ArgumentParser(description="Print what CI runs cost the runner pool")
    parser.add_argument("run_ids", nargs="*", type=int, metavar="RUN_ID")
    parser.add_argument(
        "--all",
        action="store_true",
        help="with several runs, print each run's families and leg setup, not one line each",
    )
    parser.add_argument(
        "--recent",
        type=int,
        metavar="N",
        help="read the newest N green master runs that ran pytest, and pool them",
    )
    args = parser.parse_args(argv)
    if bool(args.run_ids) == bool(args.recent):
        parser.error("name RUN_IDs or pass --recent N, not both or neither")
    runs = (
        recent_runs(args.recent)
        if args.recent
        else {run_id: read_jobs(run_id) for run_id in args.run_ids}
    )
    costs = {run_id: run_cost(jobs) for run_id, jobs in runs.items()}
    if len(costs) == 1 or args.all:
        print("\n\n".join(render(run_id, cost) for run_id, cost in costs.items()))
    else:
        print("\n".join(head_line(run_id, cost) for run_id, cost in costs.items()))
    if len(costs) > 1:
        print("\n" + render_pooled(list(costs.values())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
