"""Roll one day of Claude Code permission decisions and tool results up from Loki.

Two halves, and the load refuses a day where they disagree:

- **Counts** come from LogQL metric queries, which return aggregates and so have no line
  limit. They give every decision by entrypoint, tool, source and decision, and every tool
  result by entrypoint, tool and success.
- **The classification** of each `user_*` decision comes from raw events, paged an hour at a
  time. A page that comes back full is halved and fetched again, never read as complete.

A `config` or `hook` decision is a rule by definition. A `user_*` decision is not always a
person, and four traps decide what it was:

1. A raw query returns at most `PAGE_LIMIT` lines, so a full page may be cut short.
2. A `user_temporary` decision with no dialog, landing within `REMEMBERED_GRANT_MS` of the
   session's previous decision, reused a grant made earlier in the session: `remembered`.
3. A decision is paired with the `PermissionRequest` hook run since the session's previous
   decision, never the nearest one, because a person can take minutes to answer.
4. In an SDK entrypoint (anything but `cli`) a `user_*` source can be a settings rule or an
   automated approver. A dialog answered within `HUMAN_ANSWER_MS` is `machine`; a decision
   with no dialog on record is `unresolved`, since a rule and an unseen dialog read the same.

The rules match the claude-permission-audit plugin's `promptOutcome`, with trap 2 added.
`tool_parameters` and every content field are dropped in the query, so no command, prompt
or session id leaves Loki.

Stdlib only, so the repo's tests import it without Airflow.
"""

import urllib.parse
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from datetime import date, datetime, timedelta, timezone

from .prom_rollup import get_json

LOKI_URL = "http://loki.observability.svc.cluster.local:3100"
# observability_loki_retention (k8s/roles/observability/defaults/main.yml), in days.
LOKI_KEEP_DAYS = 31
PAGE_LIMIT = 5000
# A page still full at this width is refused rather than halved again.
MIN_PAGE = timedelta(minutes=1)
# The threshold sits in the gap between automatic answers (under 300 ms) and human ones (over 1.17
# s).
HUMAN_ANSWER_MS = 1000
# Above a remembered grant's lag (0.93 s or 1.43 s) after the session's previous decision.
REMEMBERED_GRANT_MS = 3000

STREAM = '{service_name="claude-code"}'
KEPT = (
    "event_name",
    "event_timestamp",
    "event_sequence",
    "session_id",
    "hook_event",
    "source",
    "decision",
    "tool_name",
    "app_entrypoint",
)
OUTCOMES = frozenset(
    {"rule", "hook", "prompted", "machine", "remembered", "unresolved"}
)

SCHEMA = "claude"
DECISION_TABLE = f"{SCHEMA}.decision_daily"
DECISION_COLUMNS = (
    "day",
    "entrypoint",
    "tool",
    "source",
    "decision",
    "outcome",
    "n",
)
RESULT_TABLE = f"{SCHEMA}.tool_result_daily"
RESULT_COLUMNS = ("day", "entrypoint", "tool", "success", "n")
DDL = (
    f"CREATE TABLE IF NOT EXISTS {DECISION_TABLE} (day date NOT NULL, "
    "entrypoint text NOT NULL, tool text NOT NULL, source text NOT NULL, "
    "decision text NOT NULL, outcome text NOT NULL, n integer NOT NULL, "
    "PRIMARY KEY (day, entrypoint, tool, source, decision, outcome))",
    f"CREATE TABLE IF NOT EXISTS {RESULT_TABLE} (day date NOT NULL, "
    "entrypoint text NOT NULL, tool text NOT NULL, success boolean NOT NULL, "
    "n integer NOT NULL, PRIMARY KEY (day, entrypoint, tool, success))",
)


def _start(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=timezone.utc)


def in_backfill_window(day: date, now: datetime) -> bool:
    """Whether `day` is complete and a day inside Loki's retention, so wholly held."""
    start = _start(day)
    return (
        now - timedelta(days=LOKI_KEEP_DAYS - 1) < start
        and start + timedelta(days=1) <= now
    )


def _counts_query(event: str, labels: Sequence[str], extra: str = "") -> str:
    return (
        f"sum by ({', '.join(labels)}) (count_over_time({STREAM} "
        f'| event_name="{event}"{extra} [1d]))'
    )


DECISION_COUNTS = _counts_query(
    "tool_decision", ("app_entrypoint", "tool_name", "source", "decision")
)
RESULT_COUNTS = _counts_query("tool_result", ("app_entrypoint", "tool_name", "success"))
USER_SESSIONS = _counts_query("tool_decision", ("session_id",), ' | source=~"user_.*"')


def fetch_counts(
    base_url: str, query: str, day: date, opener: Callable | None = None
) -> list[dict]:
    """An instant metric query over `day`, evaluated at the next midnight."""
    end = int((_start(day) + timedelta(days=1)).timestamp())
    params = urllib.parse.urlencode({"query": query, "time": end})
    kwargs = {"opener": opener} if opener else {}
    return get_json(f"{base_url}/loki/api/v1/query?{params}", **kwargs)["result"]


def events_query(sessions: Sequence[str]) -> str:
    """LogQL for the decisions and dialog openings of `sessions`, keeping only `KEPT`."""
    pattern = "|".join(sessions)
    return (
        f'{STREAM} | session_id=~"{pattern}" | event_name="tool_decision" or '
        f'(event_name="hook_execution_start" and hook_event="PermissionRequest") '
        f"| keep {', '.join(KEPT)}"
    )


def fetch_events(
    base_url: str,
    sessions: Sequence[str],
    day: date,
    opener: Callable | None = None,
    lead: timedelta = timedelta(minutes=10),
) -> list[dict]:
    """Every decision and dialog opening of `sessions` over `day`.

    Pages an hour at a time and halves any page that comes back full, so no page is read as
    complete while it may be cut short. Starts `lead` before midnight, so the first decision
    of the day can pair with a dialog or a previous decision just before it. Each event
    carries its Loki timestamp as `loki_ns`.

    Raises:
      RuntimeError: A page of `MIN_PAGE` still came back holding `PAGE_LIMIT` lines.
    """
    if not sessions:
        return []
    kwargs = {"opener": opener} if opener else {}
    query = events_query(sessions)

    def page(start: datetime, end: datetime) -> list[dict]:
        params = urllib.parse.urlencode(
            {
                "query": query,
                "start": int(start.timestamp() * 1e9),
                "end": int(end.timestamp() * 1e9),
                "limit": PAGE_LIMIT,
                "direction": "forward",
            }
        )
        streams = get_json(f"{base_url}/loki/api/v1/query_range?{params}", **kwargs)
        lines = [
            {**stream["stream"], "loki_ns": int(ns)}
            for stream in streams["result"]
            for ns, _ in stream["values"]
        ]
        if len(lines) < PAGE_LIMIT:
            return lines
        if end - start <= MIN_PAGE:
            raise RuntimeError(
                f"{start:%Y-%m-%d %H:%M:%S}: a {MIN_PAGE} page returned {len(lines)} "
                "lines, the limit"
            )
        middle = start + (end - start) / 2
        return page(start, middle) + page(middle, end)

    events = []
    hour = _start(day) - lead
    end = _start(day) + timedelta(days=1)
    while hour < end:
        until = min(hour + timedelta(hours=1), end)
        events += page(hour, until)
        hour = until
    return events


def _ms(event: Mapping) -> int:
    stamp = datetime.fromisoformat(event["event_timestamp"])
    return int(stamp.timestamp() * 1000)


def classify(events: Sequence[Mapping]) -> list[tuple[Mapping, str]]:
    """Each `user_*` decision in `events` with its outcome, per the module's four rules."""
    by_session: dict[str, list[Mapping]] = {}
    for event in events:
        by_session.setdefault(event["session_id"], []).append(event)
    classified = []
    for session in by_session.values():
        session.sort(key=lambda e: (_ms(e), int(e.get("event_sequence", 0))))
        dialog_ms = None
        previous_ms = None
        for event in session:
            if event["event_name"] == "hook_execution_start":
                dialog_ms = _ms(event)
                continue
            at = _ms(event)
            if event.get("source", "").startswith("user_"):
                classified.append((event, _outcome(event, at, dialog_ms, previous_ms)))
            dialog_ms = None
            previous_ms = at
    return classified


def _outcome(
    event: Mapping, at: int, dialog_ms: int | None, previous_ms: int | None
) -> str:
    if dialog_ms is not None:
        return "prompted" if at - dialog_ms >= HUMAN_ANSWER_MS else "machine"
    if (
        event["source"] == "user_temporary"
        and previous_ms is not None
        and at - previous_ms <= REMEMBERED_GRANT_MS
    ):
        return "remembered"
    return "prompted" if event.get("app_entrypoint") == "cli" else "unresolved"


def _key(labels: Mapping) -> tuple[str, str, str, str]:
    return (
        labels.get("app_entrypoint", ""),
        labels.get("tool_name", ""),
        labels.get("source", ""),
        labels.get("decision", ""),
    )


def decision_rows(
    day: date, counts: Sequence[Mapping], events: Sequence[Mapping]
) -> list[tuple]:
    """The day's `decision_daily` rows, from the metric counts and the classified events.

    Raises:
      RuntimeError: The classified `user_*` decisions inside the day do not add up to the
        metric counts for the same entrypoint, tool, source and decision.
    """
    start_ns = int(_start(day).timestamp() * 1e9)
    end_ns = start_ns + 86400 * 10**9
    in_day = [
        (event, outcome)
        for event, outcome in classify(events)
        if start_ns < event["loki_ns"] <= end_ns
    ]
    classified = Counter((*_key(event), outcome) for event, outcome in in_day)
    rows = []
    expected = Counter()
    for series in counts:
        key = _key(series["metric"])
        n = int(float(series["value"][1]))
        if key[2].startswith("user_"):
            expected[key] += n
        else:
            outcome = "hook" if key[2] == "hook" else "rule"
            rows.append((day, *key, outcome, n))
    got = Counter()
    for (*key, outcome), n in classified.items():
        got[tuple(key)] += n
        rows.append((day, *key, outcome, n))
    if got != expected:
        raise RuntimeError(
            f"{day}: classified user decisions {dict(got)} do not match the counts "
            f"{dict(expected)}"
        )
    return sorted(rows)


def result_rows(day: date, counts: Sequence[Mapping]) -> list[tuple]:
    """The day's `tool_result_daily` rows."""
    return sorted(
        (
            day,
            series["metric"].get("app_entrypoint", ""),
            series["metric"].get("tool_name", ""),
            series["metric"].get("success") == "true",
            int(float(series["value"][1])),
        )
        for series in counts
    )


def user_sessions(counts: Sequence[Mapping]) -> list[str]:
    """The session ids `USER_SESSIONS` returned."""
    return sorted(series["metric"]["session_id"] for series in counts)


def quality_problems(
    day: date, decisions: Sequence[Mapping], results: Sequence[Mapping]
) -> list[str]:
    """Every reason the day's rows are not trustworthy; empty when they are.

    A day with no Claude Code use is valid. Every tool call is decided before it runs, so a
    day with results and no decisions lost its decisions.
    """
    problems = []
    for row in decisions:
        if row["outcome"] not in OUTCOMES:
            problems.append(f"{day}: unknown outcome {row['outcome']!r}")
        if row["n"] <= 0:
            problems.append(f"{day}: a decision row counts {row['n']}")
    if results and not decisions:
        problems.append(f"{day}: tool results with no decisions")
    return problems
