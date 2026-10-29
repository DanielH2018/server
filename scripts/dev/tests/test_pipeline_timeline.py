"""The joins and gaps `pipeline_timeline.py` derives from each store's own line or record shape."""

import json
import subprocess

from pipeline_timeline import (
    ci_by_branch,
    concurrency,
    job_times,
    join,
    load_reviews,
    parse_holds,
    parse_landings,
    pull_reviews,
    rates,
    reverts,
    waited_hours,
)

GAPS_EMPTY = dict.fromkeys(
    ("issue_to_batch", "batch_to_pr", "pr_to_land", "land_to_merge", "merge_to_done")
)

# Two attempts on one PR, copied in shape from daniel-box's `landing-annotation` journal.
LEDGER = [
    "1000.5 daniel-box landing-annotation[11]: event=landing pr=5598 sha= "
    'verdict=pr-ci-red cause= exit=1 total=100 tags=none lock=0 holder="" kick=',
    "5000.0 daniel-box landing-annotation[12]: event=landing pr=5598 sha=10e3878b "
    'verdict=settled cause= exit=0 total=600 tags=media-volume lock=0 holder="" kick=',
    "6000.0 daniel-box landing-annotation[13]: event=landing pr=unknown sha= "
    'verdict=aborted cause= exit=1 total=3 tags=none lock=0 holder="" kick=',
    "-- No entries --",
]

LANDER = [
    "0.0 daniel-box uv[1]: #5586 batch 5575: ineligible, it changes the lander's own "
    "surface: pyproject.toml",
    "36000.0 daniel-box uv[2]: #5586 batch 5575: ineligible, it changes the lander's own "
    "surface: pyproject.toml",
    "36000.0 daniel-box uv[2]: #5586 batch 5575: ineligible, the head moved since the "
    "review (abc -> def)",
    "36000.0 daniel-box uv[2]: 40 PRs in the last records, 2 still open",
]


def test_landings_take_the_first_start_and_the_last_end():
    rows = parse_landings(LEDGER)

    assert rows == {
        5598: {
            "land_start": 900.5,
            "land_end": 5000.0,
            "attempts": 2,
            "verdicts": ["pr-ci-red", "settled"],
        }
    }


def test_holds_keep_the_longest_reason_and_its_span():
    assert parse_holds(LANDER) == {
        5586: {"held_by": "it changes the lander's own surface", "held_h": 10.0}
    }


def test_review_start_is_the_save_time_minus_the_phases(tmp_path):
    record = {
        "pr": "https://github.com/DanielH2018/server/pull/5633",
        "durations": {"implement": 600, "review": 120.0, "note": "not a number"},
        "findings": [{"title": "a"}, {"title": "b"}],
    }
    (tmp_path / "5610-20261018T173246Z.json").write_text(json.dumps(record))
    (tmp_path / "5611-20261018T173246Z.json").write_text(json.dumps({"pr": ""}))
    (tmp_path / "5612-20261018T173246Z.json").write_text("{not json")

    reviews = load_reviews(tmp_path)

    saved = 1792344766.0  # 2026-10-18T17:32:46Z
    assert reviews == {
        5633: {
            "issue": 5610,
            "batch_start": saved - 720,
            "phases_min": {"implement": 10.0, "review": 2.0},
            "review_findings": 2,
            "fix_rounds": 0,
        }
    }


def test_join_derives_each_gap_and_leaves_a_missing_one_none():
    prs = [
        {
            "number": 7,
            "title": "Hand x to Flux",
            "createdAt": "1970-01-01T02:00:00Z",
            "mergedAt": "1970-01-01T05:00:00Z",
        },
        {
            "number": 8,
            "title": "Lock file maintenance",
            "createdAt": "1970-01-01T00:00:00Z",
            "mergedAt": "1970-01-01T01:00:00Z",
        },
    ]
    issues = [{"number": 3, "createdAt": "1970-01-01T00:00:00Z"}]
    landings = {
        7: {
            "land_start": 4 * 3600,
            "land_end": 6 * 3600,
            "attempts": 1,
            "verdicts": ["settled"],
        }
    }
    reviews = {
        7: {"issue": 3, "batch_start": 1 * 3600, "phases_min": {"implement": 9.0}}
    }
    holds = {7: {"held_by": "it changes the lander's own surface", "held_h": 1.5}}

    flip, renovate = join(prs, issues, landings, holds, reviews)

    assert {g: flip[g] for g in ("issue_to_batch", "batch_to_pr", "pr_to_land")} == {
        "issue_to_batch": 1.0,
        "batch_to_pr": 1.0,
        "pr_to_land": 2.0,
    }
    assert (flip["land_to_merge"], flip["merge_to_done"]) == (1.0, 1.0)
    assert flip["held_by"] == "it changes the lander's own surface"
    assert renovate["issue"] is None
    assert renovate["pr_to_land"] is None
    assert renovate["held_by"] == ""


def test_concurrency_counts_overlapping_batches_and_skips_non_fanout_rows():
    def row(start, end):
        return {"batch_start": start, "land_end": end, "merged": end}

    rows = [row(0, 10), row(5, 20), {"batch_start": None, "land_end": 3, "merged": 3}]

    assert concurrency(rows) == {0: 0.0, 1: 0.75, 2: 0.25}


def _run(n, branch, conclusion, hour):
    return {
        "databaseId": n,
        "headBranch": branch,
        "conclusion": conclusion,
        "createdAt": f"2026-10-18T{hour:02d}:00:00Z",
    }


def test_first_ci_is_the_first_run_that_reached_a_verdict():
    """A draft's `skipped` run and a `cancelled` one judged nothing, so neither is first."""
    table = ci_by_branch(
        [
            _run(4, "b", "success", 4),
            _run(1, "b", "skipped", 1),
            _run(3, "b", "failure", 3),
            _run(2, "b", "cancelled", 2),
            _run(5, "c", "skipped", 1),
        ]
    )
    assert table["b"] == {"first_ci": "failure", "ci_runs": 2, "last_run": 4}
    assert table["c"] == {"first_ci": None, "ci_runs": 0, "last_run": 5}


def _job(created, started, completed):
    return {
        "created_at": f"2026-10-18T19:{created}Z",
        "started_at": started and f"2026-10-18T19:{started}Z",
        "completed_at": completed and f"2026-10-18T19:{completed}Z",
    }


def test_the_queue_is_the_longest_wait_any_job_had_for_a_runner():
    jobs = [
        _job("29:37", "29:40", "32:37"),
        _job("29:37", "35:37", "38:37"),
        _job("29:37", None, None),
    ]
    assert job_times(jobs) == {"ci_queue_min": 6.0, "ci_wall_min": 9.0}
    assert job_times([]) == {"ci_queue_min": None, "ci_wall_min": None}


def test_a_revert_names_the_pr_it_reverted_and_a_mention_does_not():
    prs = [
        {"number": 5664, "title": 'Revert "Point the probe"', "body": "Reverts DanielH2018/server#5650"},
        {
            "number": 5390,
            "title": "Revert the images job until n8n-runners builds again",
            "body": "Promote run 38992066819, the first after #5378 merged, failed in `images`.",
        },
        {"number": 5403, "title": "Land the images job again", "body": "Relands #5378 after #5392."},
    ]  # fmt: skip
    assert reverts(prs) == {5650: 5664, 5378: 5390}


def test_join_carries_the_ci_and_revert_fields_to_the_reverted_pr():
    prs = [
        {"number": 5378, "title": "D5.4a2", "createdAt": None, "mergedAt": None,
         "headRefName": "worktree-ci-images", "body": ""},
        {"number": 5390, "title": "Revert the images job", "createdAt": None, "mergedAt": None,
         "headRefName": "revert-5378", "body": "Reverts DanielH2018/server#5378"},
    ]  # fmt: skip
    ci = ci_by_branch([_run(9, "worktree-ci-images", "failure", 1)])
    jobs = {9: {"ci_queue_min": 6.0, "ci_wall_min": 9.0}}
    original, revert = join(prs, [], {}, {}, {}, ci, jobs)
    assert original["reverted_by"] == 5390
    assert (original["first_ci"], original["ci_queue_min"]) == ("failure", 6.0)
    assert revert["reverted_by"] is None
    assert revert["first_ci"] is None


def test_the_summary_prints_the_change_failure_and_first_time_green_rates():
    def row(first_ci, reverted_by, queue, wall):
        return {
            **GAPS_EMPTY,
            "pr": 5378 if reverted_by else 1,
            "first_ci": first_ci,
            "reverted_by": reverted_by,
            "ci_queue_min": queue,
            "ci_wall_min": wall,
        }

    lines = rates(
        [
            row("success", None, 6.0, 9.0),
            row("failure", 5390, 2.0, 5.0),
            row(None, None, None, None),
            row("success", None, None, None),
        ]
    )
    assert lines[0] == "first-time green: 2 of 3 judged (67%)"
    assert lines[1] == "reverted: 1 of 4 (25.0%), #5378 by #5390"
    assert lines[2] == "CI queue min (slowest job, final head): median 4.0, p90 6.0"


def test_waited_hours_sums_each_reason_and_ends_an_open_wait_at_the_merge():
    stamps = [
        {"pr": 7, "reason": "operator-step", "since": 0, "until": 3600},
        {"pr": 7, "reason": "operator-step", "since": 7200, "until": 10800},
        {"pr": 7, "reason": "needs-input", "since": 14400},
        {"pr": 7, "reason": "findings-remain", "since": 20000, "until": 30000},
    ]
    assert waited_hours(stamps, merged=18000) == {
        "operator-step": 2.0,
        "needs-input": 1.0,
    }
    assert waited_hours(stamps[:1], merged=None) == {"operator-step": 1.0}
    assert waited_hours(stamps[2:3], merged=None) == {}


def test_join_puts_each_prs_waits_on_its_own_row():
    prs = [
        {"number": 7, "title": "a", "createdAt": None, "mergedAt": "1970-01-01T05:00:00Z"},
        {"number": 8, "title": "b", "createdAt": None, "mergedAt": "1970-01-01T05:00:00Z"},
    ]  # fmt: skip
    waits = [{"pr": 7, "reason": "operator-step", "since": 3600, "until": 10800}]
    seven, eight = join(prs, [], {}, {}, {}, waits=waits)
    assert seven["waited_h"] == {"operator-step": 2.0}
    assert eight["waited_h"] == {}


def test_pull_reviews_reads_the_host_into_a_local_directory(tmp_path, monkeypatch):
    seen = []

    def fake_run(cmd, **_):
        seen.append(cmd)
        return subprocess.CompletedProcess(cmd, 23, "", "rsync: connection refused\n")

    monkeypatch.setattr(subprocess, "run", fake_run)
    dest = tmp_path / "reviews-daniel-server"

    why = pull_reviews("daniel-server", dest)

    assert why == "exit 23: rsync: connection refused"
    assert dest.is_dir()
    assert seen[0][0] == "rsync"
    assert seen[0][-2:] == ["daniel-server:.local/state/fanout-review/", f"{dest}/"]
    assert not any(
        a.startswith("--delete") or a == "--remove-source-files" for a in seen[0]
    )
