"""The permission-decision rollup's four traps, its halves check and its retention window.

`fixtures/claude-decisions-2026-11-16.json` holds, for 2026-11-16 UTC, the raw decision and
dialog events of every session with a `user_*` decision, reduced to `KEPT` with each session
id replaced, and the day's two metric-query results.
"""

import io
import itertools
import json
import re
import urllib.parse
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from dags.homelab_warehouse import claude_decisions as cd

DAY = date(2026, 11, 16)
FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures/claude-decisions-2026-11-16.json").read_text()
)
OBSERVABILITY_DEFAULTS = Path(__file__).parents[2] / "observability/defaults/main.yml"
T0 = datetime(2026, 11, 16, 12, tzinfo=timezone.utc)


def event(
    kind: str, seconds: float, source: str = "", entrypoint: str = "sdk-cli"
) -> dict:
    at = T0 + timedelta(seconds=seconds)
    base = {
        "session_id": "s",
        "event_timestamp": at.isoformat().replace("+00:00", "Z"),
        "app_entrypoint": entrypoint,
        "loki_ns": int(at.timestamp() * 1e9),
    }
    if kind == "dialog":
        return {
            **base,
            "event_name": "hook_execution_start",
            "hook_event": "PermissionRequest",
        }
    return {
        **base,
        "event_name": "tool_decision",
        "source": source,
        "decision": "accept",
        "tool_name": "Bash",
    }


def outcomes(*events: dict) -> list[str]:
    return [outcome for _, outcome in cd.classify(events)]


def test_a_recorded_day_classifies_every_user_decision_and_its_halves_agree():
    rows = cd.decision_rows(DAY, FIXTURE["decision_counts"], FIXTURE["events"])
    by_outcome = {}
    for row in rows:
        by_outcome[row[5]] = by_outcome.get(row[5], 0) + row[6]

    assert by_outcome == {"rule": 290, "hook": 123, "remembered": 1, "machine": 2}
    assert sum(by_outcome.values()) == sum(
        int(series["value"][1]) for series in FIXTURE["decision_counts"]
    )


def test_a_user_count_the_events_do_not_explain_is_refused():
    counts = json.loads(json.dumps(FIXTURE["decision_counts"]))
    user = next(c for c in counts if c["metric"]["source"].startswith("user_"))
    user["value"][1] = str(int(user["value"][1]) + 1)

    with pytest.raises(RuntimeError, match="do not match the counts"):
        cd.decision_rows(DAY, counts, FIXTURE["events"])


def test_a_dialogless_user_temporary_right_after_the_previous_decision_is_a_remembered_grant():
    previous = event("decision", 0, "config")

    assert outcomes(previous, event("decision", 2, "user_temporary")) == ["remembered"]
    assert outcomes(previous, event("decision", 9, "user_temporary")) == ["unresolved"]


def test_a_decision_pairs_with_the_dialog_since_the_previous_decision_not_an_earlier_one():
    dialog_then_answer = [event("dialog", 0), event("decision", 5, "user_temporary")]
    later = event("decision", 60, "user_permanent")

    assert outcomes(*dialog_then_answer, later) == ["prompted", "unresolved"]


def test_an_sdk_dialog_answered_at_machine_speed_is_no_prompt():
    assert outcomes(event("dialog", 0), event("decision", 0.05, "user_reject")) == [
        "machine"
    ]
    assert outcomes(event("dialog", 0), event("decision", 2, "user_reject")) == [
        "prompted"
    ]


def test_a_dialogless_user_decision_is_a_prompt_only_in_the_cli():
    lone = [
        event("decision", 0, "config"),
        event("decision", 30, "user_permanent", "cli"),
    ]

    assert outcomes(*lone) == ["prompted"]
    assert outcomes(event("decision", 30, "user_permanent")) == ["unresolved"]


class FakeLoki:
    """Answers a range query with `lines(start_ns, end_ns)` events, one stream each."""

    def __init__(self, lines):
        self.lines = lines
        self.windows = []
        self.pages = []

    def __call__(self, url, timeout):
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        start, end = int(query["start"][0]), int(query["end"][0])
        self.windows.append(end - start)
        stream = {"stream": {"event_name": "tool_decision"}, "values": []}
        count = self.lines(start, end)
        self.pages.append((start, end, count))
        stream["values"] = [[str(start + i), ""] for i in range(count)]
        body = {"status": "success", "data": {"result": [stream]}}
        return io.BytesIO(json.dumps(body).encode())


def test_a_full_page_is_halved_and_fetched_again():
    hour_ns = 3600 * 10**9
    loki = FakeLoki(lambda s, e: cd.PAGE_LIMIT if e - s >= hour_ns else 10)

    events = cd.fetch_events("http://loki", ["s"], DAY, opener=loki, lead=timedelta(0))

    kept = sorted((s, e) for s, e, n in loki.pages if n < cd.PAGE_LIMIT)
    day_start = int(datetime(2026, 11, 16, tzinfo=timezone.utc).timestamp() * 10**9)

    assert len(events) == 24 * 2 * 10
    assert kept[0][0] == day_start and kept[-1][1] == day_start + 24 * hour_ns
    assert all(a[1] == b[0] for a, b in itertools.pairwise(kept))


def test_a_page_still_full_at_the_narrowest_width_is_refused():
    loki = FakeLoki(lambda s, e: cd.PAGE_LIMIT)

    with pytest.raises(RuntimeError, match="the limit"):
        cd.fetch_events("http://loki", ["s"], DAY, opener=loki, lead=timedelta(0))


def test_the_backfill_window_stays_a_day_inside_lokis_retention():
    configured = re.search(
        r"^observability_loki_retention:\s*(\d+)h",
        OBSERVABILITY_DEFAULTS.read_text(),
        re.M,
    )
    now = datetime(2026, 12, 20, 6, tzinfo=timezone.utc)

    assert configured is not None
    assert int(configured.group(1)) == cd.LOKI_KEEP_DAYS * 24
    assert cd.in_backfill_window(date(2026, 11, 21), now)
    assert not cd.in_backfill_window(date(2026, 11, 20), now)
    assert not cd.in_backfill_window(date(2026, 12, 20), now)


def test_an_unknown_outcome_and_results_without_decisions_fail_the_check():
    decisions = [{"outcome": "guessed", "n": 1}]
    results = [{"n": 3}]

    assert cd.quality_problems(DAY, decisions, results) == [
        f"{DAY}: unknown outcome 'guessed'"
    ]
    assert cd.quality_problems(DAY, [], results) == [
        f"{DAY}: tool results with no decisions"
    ]
