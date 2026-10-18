"""The joins and gaps `pipeline_timeline.py` derives from each store's own line or record shape."""

import json

from pipeline_timeline import (
    concurrency,
    join,
    load_reviews,
    parse_holds,
    parse_landings,
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
