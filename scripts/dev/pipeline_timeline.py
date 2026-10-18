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
        out[int(pr[1])] = {
            "issue": int(name["batch"]),
            "batch_start": saved.timestamp() - sum(phases.values()),
            "phases_min": {k: round(v / 60, 1) for k, v in phases.items()},
        }
    return out


def _epoch(iso: str | None) -> float | None:
    return (
        datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() if iso else None
    )


def _gap(a: float | None, b: float | None) -> float | None:
    return None if a is None or b is None else round((b - a) / 3600, 2)


def join(prs, issues, landings, holds, reviews) -> list[dict]:
    """One row per merged PR, with every stamp found and the gaps between them."""
    created = {i["number"]: _epoch(i["createdAt"]) for i in issues}
    rows = []
    for pr in prs:
        n = pr["number"]
        review = reviews.get(n, {})
        landing = landings.get(n, {})
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
            }
        )
    return rows


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
    args = parser.parse_args(argv)

    since = datetime.fromisoformat(args.since).replace(tzinfo=UTC)
    prs = gh_json(
        "pr", "list", "--state", "merged", "--limit", "1000",
        "--search", f"merged:>={args.since}",
        "--json", "number,title,createdAt,mergedAt",
    ) or []  # fmt: skip
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
    rows = join(prs, issues, landings, holds, reviews)

    args.out.mkdir(parents=True, exist_ok=True)
    snapshot = args.out / f"{datetime.now(UTC).date().isoformat()}.jsonl"
    with snapshot.open("a") as fh:
        fh.writelines(json.dumps(r, sort_keys=True) + "\n" for r in rows)
    print(json.dumps(rows, indent=2) if args.json else summarize(rows))
    print(f"appended {len(rows)} rows to {snapshot}", file=_sys.stderr)
    return 0


if __name__ == "__main__":
    _sys.exit(main())
