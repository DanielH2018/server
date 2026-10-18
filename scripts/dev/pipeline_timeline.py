#!/usr/bin/env python3
"""Join each merged PR's stage timestamps into one row, and keep a dated snapshot of them.

Run: uv run python scripts/dev/pipeline_timeline.py [--since YYYY-MM-DD] [--out DIR] [--json]

WHY. A PR's calendar time is spread over four stores, and none of them can answer "where did
the time go" alone:

- GitHub has the issue, the PR's open and merge times, and nothing about the landing.
- The fan-out review records (`~/.local/state/fanout-review/<batch>-<saved>.json`) have each
  phase's duration and the time the record was saved, and nothing after the land phase.
- The landing ledger (`event=landing` lines under the `landing-annotation` syslog tag) has
  every land.sh attempt and its verdict, and nothing before the attempt started.
- The server autolander's journal says why it left a PR for a person, and keeps saying it
  every pass until someone lands it.

The two journal stores rotate. journald holds about ten days on daniel-box (`SystemMaxUse=1G`),
so a question asked a fortnight later finds half its inputs gone. Each run therefore appends
its rows to `<out>/<UTC date>.jsonl`, which outlives the journal. The rows of a later snapshot
supersede an earlier one's for the same PR.

The gaps each row derives, in hours, with None when a stamp is missing:

| Gap | From | To |
|---|---|---|
| `issue_to_batch` | the issue's creation | the batch's start (saved minus its phases) |
| `batch_to_pr` | the batch's start | the PR's creation |
| `pr_to_land` | the PR's creation | the first land.sh attempt's start |
| `land_to_merge` | the first attempt's start | the merge (negative: merged by hand first) |
| `merge_to_done` | the merge | the last attempt's end |

Each row also carries what a change cost to get right, and what CI cost to judge it:

- `first_ci` is the conclusion of the PR's first CI run that reached a verdict. A draft's
  `skipped` run and a `cancelled` one are not verdicts. `ci_runs` counts the runs that were.
- `ci_queue_min` is the longest any job of the final head's run waited for a runner, and
  `ci_wall_min` that run's span from the first job queued to the last done. #5526 measured
  the queue once at a median 6m12s against the 20-job cap, more than the run itself.
- `reverted_by` is the merged PR that reverted this one (`reverts` has the two forms).
- `review_findings` and `fix_rounds` come from the batch's review record.

The summary prints the first-time-green rate, the share reverted with each pair listed,
and the CI queue and wall medians. The pairs are listed because a planned drill's revert
counts too, so a person judges which were failures. A fix that lands as a new PR without
reverting is not counted; nothing reliable links it to the PR it fixes.

`pr_to_land` is the idle gap a person or a hold accounts for, and `held_by` names the
autolander's reason when it gave one. Only daniel-server batches pass the autolander, so a
daniel-box PR's `held_by` is always empty.

The summary also prints the share of the window with N fan-out batches live. A long
`issue_to_batch` while that share sits at 0 or 1 means the launches, not the slots, are what
an issue waits on.

Each host keeps its own review records, so a daniel-server batch is joined only when its
records are passed too:

    scp 'daniel-server:.local/state/fanout-review/*.json' /tmp/server-reviews/
    uv run python scripts/dev/pipeline_timeline.py \\
        --review-dir ~/.local/state/fanout-review --review-dir /tmp/server-reviews
"""

import json
import re
import statistics
import subprocess
import sys as _sys
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))  # scripts/

from lib.cli_help import ArgumentParser, answer_help
from lib.gh import gh_json

REVIEW_DIR = Path.home() / ".local" / "state" / "fanout-review"
OUT_DIR = Path.home() / ".local" / "state" / "pipeline-timeline"
LEDGER_TAG = "landing-annotation"
LANDER_UNIT = "claude-fanout-server-lander.service"
# How far before --since an issue may have been filed and still be joined to a PR after it.
ISSUE_LOOKBACK = timedelta(days=21)
GAPS = ("issue_to_batch", "batch_to_pr", "pr_to_land", "land_to_merge", "merge_to_done")

_JOURNAL_LINE = re.compile(r"^(?P<ts>\d+(?:\.\d+)?) \S+ \S+: (?P<msg>.*)$")
_FIELD = re.compile(r'(\w+)=("[^"]*"|\S*)')
_HOLD = re.compile(r"#(?P<pr>\d+) batch \S+ ineligible, (?P<why>[^:(]*)")
_RECORD_NAME = re.compile(r"^(?P<batch>\d+)\S*-(?P<saved>\d{8}T\d{6}Z)\.json$")
_PR_URL = re.compile(r"/pull/(\d+)$")
_REVERTS = re.compile(r"(?im)^Reverts \S*#(\d+)\s*$")
_REVERT_TITLE = re.compile(r"^Revert\b")
_PR_REF = re.compile(r"#(\d+)\b")
CI_WORKFLOW = "ci.yml"
# Parallel jobs-API readers; 8 keeps a run inside the token's 5000/h with room to spare.
JOB_READERS = 8
# The conclusions that judged the head. `skipped` (a draft) and `cancelled` judged nothing.
_CI_VERDICTS = frozenset({"success", "failure", "timed_out", "startup_failure"})


def _journal(lines):
    """Yield (epoch seconds, message) for each `journalctl -o short-unix` line that parses."""
    for line in lines:
        m = _JOURNAL_LINE.match(line.rstrip("\n"))
        if m:
            yield float(m["ts"]), m["msg"]


def parse_landings(lines) -> dict[int, dict]:
    """Every land.sh attempt per PR: first start, last end, attempt count and verdicts in order.

    The ledger line is written when an attempt ends and carries its `total`, so an attempt's
    start is its journal time minus that. A line with no PR number (`pr=unknown`) is dropped.
    """
    out: dict[int, dict] = {}
    for ts, msg in _journal(lines):
        if not msg.startswith("event=landing "):
            continue
        fields = {k: v.strip('"') for k, v in _FIELD.findall(msg)}
        if not fields.get("pr", "").isdigit():
            continue
        start = ts - float(fields.get("total") or 0)
        row = out.setdefault(
            int(fields["pr"]),
            {"land_start": start, "land_end": ts, "attempts": 0, "verdicts": []},
        )
        row["land_start"] = min(row["land_start"], start)
        row["land_end"] = max(row["land_end"], ts)
        row["attempts"] += 1
        row["verdicts"].append(fields.get("verdict", ""))
    return out


def parse_holds(lines) -> dict[int, dict]:
    """The autolander's longest-held reason per PR, with how long it repeated that reason.

    The lander prints one line per PR per pass, so the span from a reason's first line to
    its last is how long that reason held the PR.
    """
    spans: dict[tuple[int, str], list[float]] = {}
    for ts, msg in _journal(lines):
        m = _HOLD.search(msg)
        if not m:
            continue
        span = spans.setdefault((int(m["pr"]), m["why"].strip()), [ts, ts])
        span[0], span[1] = min(span[0], ts), max(span[1], ts)
    out: dict[int, dict] = {}
    for (pr, why), (first, last) in spans.items():
        hours = (last - first) / 3600
        if pr not in out or hours > out[pr]["held_h"]:
            out[pr] = {"held_by": why, "held_h": round(hours, 2)}
    return out


def load_reviews(directory: Path) -> dict[int, dict]:
    """Each PR's fan-out batch: its issue, its start time and its phase durations in minutes.

    The batch id leads with the issue number. The file name carries the time the record was
    saved, which is after its last phase, so the batch's start is that time minus the phase
    durations. A record that names no PR, or does not parse, is skipped; the latest record
    for a PR wins.
    """
    out: dict[int, dict] = {}
    for path in sorted(directory.glob("*.json")):
        name = _RECORD_NAME.match(path.name)
        if not name:
            continue
        try:
            record = json.loads(path.read_text())
        except OSError, ValueError:
            continue
        pr = _PR_URL.search(str(record.get("pr") or ""))
        if not pr:
            continue
        saved = datetime.strptime(name["saved"], "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
        phases = {
            k: v
            for k, v in (record.get("durations") or {}).items()
            if isinstance(v, (int, float))
        }
        findings = record.get("findings")
        out[int(pr[1])] = {
            "issue": int(name["batch"]),
            "batch_start": saved.timestamp() - sum(phases.values()),
            "phases_min": {k: round(v / 60, 1) for k, v in phases.items()},
            "review_findings": len(findings) if isinstance(findings, list) else None,
            "fix_rounds": sum(1 for k in ("fix", "ci-fix") if k in phases),
        }
    return out


def ci_by_branch(runs: list[dict]) -> dict[str, dict]:
    """Per head branch: the first verdict's conclusion, the verdict count, the newest run.

    `runs` is `gh run list --json databaseId,headBranch,conclusion,createdAt` for the CI
    workflow. The newest run is the one the final head's queue time is read from.
    """
    out: dict[str, dict] = {}
    for run in sorted(runs, key=lambda r: r.get("createdAt") or ""):
        row = out.setdefault(
            str(run.get("headBranch")),
            {"first_ci": None, "ci_runs": 0, "last_run": None},
        )
        row["last_run"] = run.get("databaseId")
        if run.get("conclusion") in _CI_VERDICTS:
            row["ci_runs"] += 1
            row["first_ci"] = row["first_ci"] or run["conclusion"]
    return out


def job_times(jobs: list[dict]) -> dict:
    """One run's longest runner wait and its span, in minutes, from its jobs' timestamps."""
    waits, created, done = [], [], []
    for job in jobs:
        queued, started = _epoch(job.get("created_at")), _epoch(job.get("started_at"))
        if queued is None or started is None:
            continue
        waits.append(started - queued)
        created.append(queued)
        if finished := _epoch(job.get("completed_at")):
            done.append(finished)
    if not waits:
        return {"ci_queue_min": None, "ci_wall_min": None}
    return {
        "ci_queue_min": round(max(waits) / 60, 1),
        "ci_wall_min": round((max(done) - min(created)) / 60, 1) if done else None,
    }


def read_job_times(run_id: int) -> dict:
    """`job_times` for one CI run, read from the Actions jobs API."""
    listed = gh_json(
        "api", f"repos/{{owner}}/{{repo}}/actions/runs/{run_id}/jobs?per_page=100"
    )
    found = listed.get("jobs") if isinstance(listed, dict) else None
    return job_times(
        [j for j in found if isinstance(j, dict)] if isinstance(found, list) else []
    )


def reverts(prs: list[dict]) -> dict[int, int]:
    """Each reverted PR's number, mapped to the merged PR that reverted it.

    GitHub's revert button writes `Reverts <repo>#<n>` in the body. A revert written by hand,
    as most here are, has a title starting `Revert` and names its target as the first
    `#<n>` in the title, else in the body: #5390 "Revert the images job ..." names #5378
    first. A planned drill's revert counts too, so the summary lists every pair for a
    person to judge rather than calling the count a failure rate on its own.
    """
    out: dict[int, int] = {}
    for pr in prs:
        body = str(pr.get("body") or "")
        targets = [int(m[1]) for m in _REVERTS.finditer(body)]
        if not targets and _REVERT_TITLE.match(str(pr.get("title") or "")):
            refs = _PR_REF.findall(str(pr.get("title") or "")) or _PR_REF.findall(body)
            targets = [int(r) for r in refs if int(r) != pr["number"]][:1]
        for target in targets:
            out.setdefault(target, pr["number"])
    return out


def _epoch(iso: str | None) -> float | None:
    return (
        datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() if iso else None
    )


def _gap(a: float | None, b: float | None) -> float | None:
    return None if a is None or b is None else round((b - a) / 3600, 2)


def join(prs, issues, landings, holds, reviews, ci=None, jobs=None) -> list[dict]:
    """One row per merged PR, with every stamp found and the gaps between them.

    Args:
        ci: `ci_by_branch`'s table; None leaves the CI fields empty.
        jobs: `job_times` per CI run id; None leaves the queue fields empty.
    """
    created = {i["number"]: _epoch(i["createdAt"]) for i in issues}
    reverted = reverts(prs)
    rows = []
    for pr in prs:
        n = pr["number"]
        review = reviews.get(n, {})
        landing = landings.get(n, {})
        runs = (ci or {}).get(str(pr.get("headRefName")), {})
        times = (jobs or {}).get(runs.get("last_run"), {})
        stamps = {
            "issue_created": created.get(review.get("issue")),
            "batch_start": review.get("batch_start"),
            "pr_created": _epoch(pr["createdAt"]),
            "land_start": landing.get("land_start"),
            "merged": _epoch(pr["mergedAt"]),
            "land_end": landing.get("land_end"),
        }
        rows.append(
            {
                "pr": n,
                "title": pr["title"],
                "issue": review.get("issue"),
                **stamps,
                "issue_to_batch": _gap(stamps["issue_created"], stamps["batch_start"]),
                "batch_to_pr": _gap(stamps["batch_start"], stamps["pr_created"]),
                "pr_to_land": _gap(stamps["pr_created"], stamps["land_start"]),
                "land_to_merge": _gap(stamps["land_start"], stamps["merged"]),
                "merge_to_done": _gap(stamps["merged"], stamps["land_end"]),
                "attempts": landing.get("attempts", 0),
                "verdicts": landing.get("verdicts", []),
                "phases_min": review.get("phases_min", {}),
                **holds.get(n, {"held_by": "", "held_h": None}),
                "first_ci": runs.get("first_ci"),
                "ci_runs": runs.get("ci_runs", 0),
                "ci_queue_min": times.get("ci_queue_min"),
                "ci_wall_min": times.get("ci_wall_min"),
                "reverted_by": reverted.get(n),
                "review_findings": review.get("review_findings"),
                "fix_rounds": review.get("fix_rounds"),
            }
        )
    return rows


def _median_p90(values: list[float]) -> str:
    if not values:
        return "-"
    values = sorted(values)
    p90 = values[min(len(values) - 1, int(len(values) * 0.9))]
    return f"median {statistics.median(values):.1f}, p90 {p90:.1f}"


def rates(rows: list[dict]) -> list[str]:
    """The first-time-green and change-failure rates, and the CI queue and wall times."""
    judged = [r for r in rows if r["first_ci"]]
    green = sum(1 for r in judged if r["first_ci"] == "success")
    failed = sum(1 for r in rows if r["reverted_by"])
    lines = [
        f"first-time green: {green} of {len(judged)} judged"
        + (f" ({green / len(judged):.0%})" if judged else ""),
        f"reverted: {failed} of {len(rows)}"
        + (f" ({failed / len(rows):.1%})" if rows else "")
        + "".join(
            f", #{r['pr']} by #{r['reverted_by']}" for r in rows if r["reverted_by"]
        ),
        "CI queue min (slowest job, final head): "
        + _median_p90(
            [r["ci_queue_min"] for r in rows if r["ci_queue_min"] is not None]
        ),
        "CI wall min (final head): "
        + _median_p90([r["ci_wall_min"] for r in rows if r["ci_wall_min"] is not None]),
    ]
    return lines


def concurrency(rows: list[dict]) -> dict[int, float]:
    """The share of the span from the first batch start to the last end with N batches live.

    A batch is live from its start to its last landing's end, or to the merge when it has no
    landing. A row with no batch is not a fan-out PR and is left out.
    """
    events = []
    for r in rows:
        end = r["land_end"] or r["merged"]
        if r["batch_start"] is None or end is None:
            continue
        events += [(r["batch_start"], 1), (end, -1)]
    if not events:
        return {}
    events.sort()
    spent: dict[int, float] = {}
    live, prev = 0, events[0][0]
    for t, step in events:
        spent[live] = spent.get(live, 0.0) + t - prev
        live, prev = live + step, t
    total = sum(spent.values()) or 1.0
    return {n: spent[n] / total for n in sorted(spent)}


def summarize(rows: list[dict]) -> str:
    """Median, p90 and count per gap, then the ten PRs with the longest idle gap."""
    lines = [f"{len(rows)} merged PRs", "gap              n   median_h   p90_h"]
    for gap in GAPS:
        values = sorted(r[gap] for r in rows if r[gap] is not None)
        if not values:
            lines.append(f"{gap:15} {0:3}          -       -")
            continue
        p90 = values[min(len(values) - 1, int(len(values) * 0.9))]
        lines.append(
            f"{gap:15} {len(values):3} {statistics.median(values):10.2f} {p90:7.2f}"
        )
    lines.extend(rates(rows))
    shares = concurrency(rows)
    lines.append(
        "share of time with N batches live: "
        + ", ".join(f"{n}={share:.0%}" for n, share in shares.items())
    )
    idle = sorted(
        (r for r in rows if r["pr_to_land"] is not None),
        key=lambda r: r["pr_to_land"],
        reverse=True,
    )[:10]
    lines.append("longest pr_to_land:")
    lines.extend(
        f"  #{r['pr']} {r['pr_to_land']:.1f}h {r['held_by'] or '-'} | {r['title'][:60]}"
        for r in idle
    )
    return "\n".join(lines)


def _objects(value: object) -> list[dict]:
    """The JSON objects of a `gh --json` list; anything else is no rows."""
    return [v for v in value if isinstance(v, dict)] if isinstance(value, list) else []


def _journal_lines(*selector: str, since: str) -> list[str]:
    done = subprocess.run(
        ["journalctl", *selector, "--since", since, "-o", "short-unix", "--no-pager"],
        capture_output=True,
        text=True,
        check=False,
    )
    return done.stdout.splitlines()


def main(argv: list[str] | None = None) -> int:
    answer_help(__doc__, argv)
    parser = ArgumentParser(description="Join merged PRs' stage timestamps")
    default_since = (datetime.now(UTC) - timedelta(days=7)).date().isoformat()
    parser.add_argument(
        "--since", default=default_since, help="merged on or after (UTC)"
    )
    parser.add_argument("--out", type=Path, default=OUT_DIR, help="snapshot directory")
    parser.add_argument(
        "--review-dir",
        type=Path,
        action="append",
        help=f"fan-out review records, once per host's copy (default {REVIEW_DIR})",
    )
    parser.add_argument(
        "--json", action="store_true", help="print the rows, not the summary"
    )
    parser.add_argument(
        "--no-ci-jobs",
        action="store_true",
        help="skip the one jobs call per PR that the queue times need",
    )
    args = parser.parse_args(argv)

    since = datetime.fromisoformat(args.since).replace(tzinfo=UTC)
    prs = _objects(gh_json(
        "pr", "list", "--state", "merged", "--limit", "1000",
        "--search", f"merged:>={args.since}",
        "--json", "number,title,createdAt,mergedAt,headRefName,body",
    ))  # fmt: skip
    ci = ci_by_branch(_objects(gh_json(
        "run", "list", "--workflow", CI_WORKFLOW, "--event", "pull_request",
        "--limit", "5000", "--created", f">={(since - ISSUE_LOOKBACK).date().isoformat()}",
        "--json", "databaseId,headBranch,conclusion,createdAt",
    )))  # fmt: skip
    jobs: dict = {}
    if not args.no_ci_jobs:
        wanted = {
            ci[b]["last_run"]
            for b in {str(p.get("headRefName")) for p in prs}
            if b in ci
        }
        # One call per run, each about 1.7s: serial, 240 PRs took 7 minutes (2026-10-18).
        with ThreadPoolExecutor(max_workers=JOB_READERS) as pool:
            ids = sorted(r for r in wanted if r)
            for run_id, times in zip(ids, pool.map(read_job_times, ids), strict=True):
                jobs[run_id] = times
    issues = gh_json(
        "issue", "list", "--state", "all", "--limit", "2000",
        "--search", f"created:>={(since - ISSUE_LOOKBACK).date().isoformat()}",
        "--json", "number,createdAt",
    ) or []  # fmt: skip
    landings = parse_landings(_journal_lines("-t", LEDGER_TAG, since=args.since))
    holds = parse_holds(_journal_lines("-u", LANDER_UNIT, since=args.since))
    reviews: dict[int, dict] = {}
    for directory in args.review_dir or [REVIEW_DIR]:
        reviews |= load_reviews(directory)
    rows = join(prs, issues, landings, holds, reviews, ci, jobs)

    args.out.mkdir(parents=True, exist_ok=True)
    snapshot = args.out / f"{datetime.now(UTC).date().isoformat()}.jsonl"
    with snapshot.open("a") as fh:
        fh.writelines(json.dumps(r, sort_keys=True) + "\n" for r in rows)
    print(json.dumps(rows, indent=2) if args.json else summarize(rows))
    print(f"appended {len(rows)} rows to {snapshot}", file=_sys.stderr)
    return 0


if __name__ == "__main__":
    _sys.exit(main())
