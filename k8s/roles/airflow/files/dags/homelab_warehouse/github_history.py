"""Roll one day of the repo's pull requests, Actions runs and findings issues into the warehouse.

GitHub keeps all of it, but reading a quarter of it live costs thousands of API calls, and
every token of one user shares one quota. So each day is read once, in quiet hours, and kept
in `github.counts_daily`:

- `prs_merged`: merged pull requests per author, with the seconds from opening to merge.
- `ci_runs`: Actions runs per workflow and conclusion, with the seconds each attempt ran.
- `issues_opened` and `issues_closed`: findings issues, the ones labelled `claude`.

The Actions API returns at most 1,000 runs for one filtered query, and the repo starts about
2,500 a day, so runs are read an hour at a time, and a window whose total passes the cap is
halved. A run belongs to the day it was created.

Stdlib only, so the repo's tests import it without Airflow.

Typical usage example:

    api = GitHub(token)
    if api.remaining() < MIN_REMAINING:
        ...
    rows = day_rows(day, api.get)
"""

import re
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from datetime import date, datetime, timedelta, timezone

from .prom_rollup import read_json

API_URL = "https://api.github.com"
REPO = "DanielH2018/server"
FINDINGS_LABEL = "claude"
PAGE = 100
# The Actions API's cap on the results of one filtered query.
RESULT_CAP = 1000
# Calls left in the quota below which a run waits for the next window rather than spend them.
MIN_REMAINING = 2000
# A dynamic workflow, such as dependency-graph submission, numbers each run in its name.
_RUN_NUMBER = re.compile(r" #\d+$")

SCHEMA = "github"
COUNTS_TABLE = f"{SCHEMA}.counts_daily"
COUNTS_COLUMNS = ("day", "metric", "key", "detail", "n", "seconds")
DDL = (
    f"CREATE TABLE IF NOT EXISTS {COUNTS_TABLE} (day date NOT NULL, metric text NOT NULL, "
    "key text NOT NULL, detail text NOT NULL, n integer NOT NULL, "
    "seconds double precision NOT NULL, PRIMARY KEY (day, metric, key, detail))",
)

Get = Callable[[str, Mapping[str, object]], Mapping]


class GitHub:
    """Authenticated GETs against GitHub's REST API."""

    def __init__(self, token: str, opener: Callable = urllib.request.urlopen):
        self._token = token
        self._opener = opener

    def get(self, path: str, params: Mapping[str, object]) -> Mapping:
        """GET a REST path with the token, returning the parsed body."""
        request = urllib.request.Request(
            f"{API_URL}/{path}?{urllib.parse.urlencode(params)}",
            headers={
                "Authorization": f"Bearer {self._token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        return read_json(request, self._opener)

    def remaining(self) -> int:
        """Core REST calls left in the token's quota window."""
        return self.get("rate_limit", {})["resources"]["core"]["remaining"]


def _start(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=timezone.utc)


def _parse(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp)


def _stamp(at: datetime) -> str:
    return at.strftime("%Y-%m-%dT%H:%M:%SZ")


def _search_all(get: Get, query: str) -> list[Mapping]:
    items: list[Mapping] = []
    page = 1
    while True:
        body = get("search/issues", {"q": query, "per_page": PAGE, "page": page})
        items += body["items"]
        if len(items) >= body["total_count"] or not body["items"]:
            return items
        page += 1


def _count(get: Get, query: str) -> int:
    return get("search/issues", {"q": query, "per_page": 1})["total_count"]


def fetch_runs(get: Get, start: datetime, end: datetime) -> list[Mapping]:
    """Every Actions run created in [start, end), read an hour at a time.

    Raises:
      RuntimeError: A one-minute window still held more runs than `RESULT_CAP`.
    """

    def window(lo: datetime, hi: datetime) -> list[Mapping]:
        created = f"{_stamp(lo)}..{_stamp(hi - timedelta(seconds=1))}"
        runs: list[Mapping] = []
        page = 1
        while True:
            body = get(
                f"repos/{REPO}/actions/runs",
                {"created": created, "per_page": PAGE, "page": page},
            )
            if body["total_count"] > RESULT_CAP:
                if hi - lo <= timedelta(minutes=1):
                    raise RuntimeError(
                        f"{created}: {body['total_count']} runs, over the API's cap"
                    )
                middle = lo + (hi - lo) / 2
                return window(lo, middle) + window(middle, hi)
            runs += body["workflow_runs"]
            if len(runs) >= body["total_count"] or not body["workflow_runs"]:
                return runs
            page += 1

    runs: list[Mapping] = []
    hour = start
    while hour < end:
        runs += window(hour, min(hour + timedelta(hours=1), end))
        hour += timedelta(hours=1)
    return runs


def _seconds(start: str | None, end: str | None) -> float:
    if not start or not end:
        return 0.0
    return max((_parse(end) - _parse(start)).total_seconds(), 0.0)


def _summed(
    day: date, metric: str, parts: Sequence[tuple[str, str, float]]
) -> list[tuple]:
    sums: dict[tuple[str, str], list[float]] = {}
    for key, detail, seconds in parts:
        entry = sums.setdefault((key, detail), [0, 0.0])
        entry[0] += 1
        entry[1] += seconds
    return [
        (day, metric, key, detail, int(n), round(seconds, 1))
        for (key, detail), (n, seconds) in sorted(sums.items())
    ]


def pr_rows(day: date, prs: Sequence[Mapping]) -> list[tuple]:
    """`prs_merged` rows: one per author, with the seconds from opening to merge."""
    return _summed(
        day,
        "prs_merged",
        [
            (
                pr["user"]["login"],
                "",
                _seconds(pr["created_at"], pr["pull_request"]["merged_at"]),
            )
            for pr in prs
        ],
    )


def run_rows(day: date, runs: Sequence[Mapping]) -> list[tuple]:
    """`ci_runs` rows: one per workflow and conclusion, with the seconds each attempt ran."""
    return _summed(
        day,
        "ci_runs",
        [
            (
                _RUN_NUMBER.sub("", run["name"]),
                run.get("conclusion") or run["status"],
                _seconds(run.get("run_started_at"), run["updated_at"]),
            )
            for run in runs
        ],
    )


def day_rows(day: date, get: Get) -> list[tuple]:
    """Every `counts_daily` row for one UTC day."""
    iso = day.isoformat()
    prs = _search_all(get, f"repo:{REPO} is:pr is:merged merged:{iso}")
    runs = fetch_runs(get, _start(day), _start(day) + timedelta(days=1))
    issues = [
        (
            day,
            metric,
            FINDINGS_LABEL,
            "",
            _count(
                get, f"repo:{REPO} is:issue label:{FINDINGS_LABEL} {qualifier}:{iso}"
            ),
            0.0,
        )
        for metric, qualifier in (
            ("issues_opened", "created"),
            ("issues_closed", "closed"),
        )
    ]
    return pr_rows(day, prs) + run_rows(day, runs) + issues


def quality_problems(day: date, rows: Sequence[Mapping]) -> list[str]:
    """Every reason the day's `counts_daily` rows are not trustworthy; empty when they are.

    CI runs on every push to master and every scheduled workflow, so a day with no run means
    the read failed, not that the repo was idle. A day with no merged PR is possible.
    """
    problems = []
    if not any(row["metric"] == "ci_runs" for row in rows):
        problems.append(f"{day}: no Actions run")
    for row in rows:
        if row["n"] < 0 or row["seconds"] < 0:
            problems.append(f"{day}: {row['metric']} {row['key']} is negative")
    return problems
