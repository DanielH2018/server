"""The GitHub rollup's paging, its run windows and its check, against a recorded day.

`fixtures/github-2026-11-19.json` holds the two search pages of PRs merged on 2026-11-19 UTC,
the Actions runs created from 12:00 to 13:00 that day, and the day's findings-issue counts,
each reduced to the fields the rollup reads.
"""

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from dags.homelab_warehouse import github_history as gh

DAY = date(2026, 11, 19)
NOON = datetime(2026, 11, 19, 12, tzinfo=timezone.utc)
FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures/github-2026-11-19.json").read_text()
)


class FakeGitHub:
    """Answers the three reads from the fixture: runs only for the recorded hour."""

    def __init__(self, runs_total=None):
        self.calls = []
        self.runs_total = runs_total

    def __call__(self, path, params):
        self.calls.append((path, dict(params)))
        if path == "search/issues":
            query = params["q"]
            if "is:pr" in query:
                return FIXTURE["pr_pages"][params["page"] - 1]
            qualifier = "created" if "created:" in query else "closed"
            return {"total_count": FIXTURE[f"issues_{qualifier}"], "items": []}
        lo = params["created"].split("..")[0]
        if self.runs_total is not None:
            return {
                "total_count": self.runs_total(params["created"]),
                "workflow_runs": [],
            }
        if lo == "2026-11-19T12:00:00Z":
            return FIXTURE["runs_12h"]
        return {"total_count": 0, "workflow_runs": []}


def rows_by_key(rows):
    return {(r[1], r[2], r[3]): (r[4], r[5]) for r in rows}


def test_both_pr_pages_count_per_author():
    rows = rows_by_key(gh.day_rows(DAY, FakeGitHub()))

    assert rows[("prs_merged", "DanielH2018", "")][0] == 160
    assert rows[("prs_merged", "renovate[bot]", "")][0] == 14


def test_runs_count_per_workflow_and_conclusion_without_a_run_number():
    rows = rows_by_key(gh.day_rows(DAY, FakeGitHub()))

    assert rows[("ci_runs", "session-milestone", "success")][0] == 19
    assert rows[("ci_runs", "CI", "skipped")][0] == 12
    assert ("ci_runs", "Graph Update: uv in /.", "success") in rows
    assert sum(n for (m, _, _), (n, _) in rows.items() if m == "ci_runs") == 77


def test_the_findings_issue_counts_are_kept():
    rows = rows_by_key(gh.day_rows(DAY, FakeGitHub()))

    assert rows[("issues_opened", "claude", "")] == (126, 0.0)
    assert rows[("issues_closed", "claude", "")] == (119, 0.0)


def test_runs_are_read_in_hourly_windows_that_tile_the_day():
    fake = FakeGitHub()

    gh.fetch_runs(fake, NOON - timedelta(hours=12), NOON + timedelta(hours=12))

    windows = [p["created"] for path, p in fake.calls if path.startswith("repos/")]
    assert len(windows) == 24
    assert windows[0] == "2026-11-19T00:00:00Z..2026-11-19T00:59:59Z"
    assert windows[-1] == "2026-11-19T23:00:00Z..2026-11-19T23:59:59Z"


def test_a_window_over_the_cap_is_halved():
    fake = FakeGitHub(
        runs_total=lambda created: (
            1500 if created.startswith("2026-11-19T12:00:00Z..2026-11-19T12:59") else 0
        )
    )

    gh.fetch_runs(fake, NOON, NOON + timedelta(hours=1))

    assert [p["created"] for _, p in fake.calls] == [
        "2026-11-19T12:00:00Z..2026-11-19T12:59:59Z",
        "2026-11-19T12:00:00Z..2026-11-19T12:29:59Z",
        "2026-11-19T12:30:00Z..2026-11-19T12:59:59Z",
    ]


def test_a_minute_still_over_the_cap_is_refused():
    fake = FakeGitHub(runs_total=lambda created: 1500)

    with pytest.raises(RuntimeError, match="over the API's cap"):
        gh.fetch_runs(fake, NOON, NOON + timedelta(minutes=1))


def test_a_day_without_runs_fails_the_check_and_a_recorded_day_passes():
    rows = [
        dict(zip(gh.COUNTS_COLUMNS, row, strict=True))
        for row in gh.day_rows(DAY, FakeGitHub())
    ]

    assert gh.quality_problems(DAY, rows) == []
    assert gh.quality_problems(DAY, []) == [f"{DAY}: no Actions run"]
