"""speedtest-tracker's newest result row.

`speedtest_verdict` judges one row of /api/v1/results. The rows here are trimmed copies of real
ones (ids 780 and 745, fetched 2026-08-24) — including `created_at`'s bare, offset-less UTC
serialization, which is the detail the age arm turns on.
"""

from dataclasses import replace

from datetime import datetime, timezone
from pathlib import Path


import bridge.config
import bridge.net
import checks.host_edge
import verdicts.host

_REPO = Path(__file__).resolve().parents[5]

# ── speedtest-tracker's newest result row ────────────────────────────────────────────────
# speedtest_verdict judges one row of /api/v1/results. The rows below are trimmed copies of
# real ones (ids 780 and 745, fetched 2026-08-24) — including `created_at`'s bare, offset-less
# UTC serialization, which is the detail the age arm turns on.
ST_NOW = datetime(2026, 8, 24, 12, 0, 0, tzinfo=timezone.utc)


def _st_row(**over):
    row = {
        "id": 780,
        "status": "completed",
        "created_at": "2026-08-24 11:00:00",
        "download_bits": 910_000_000,
        "data": {"type": "result", "server": {"id": 41671, "name": "x99.cloud"}},
    }
    row.update(over)
    return row


def test_speedtest_fast_completed_run_is_ok():
    ok, msg = verdicts.host.speedtest_verdict(_st_row(), 100.0, 8.0, now=ST_NOW)
    assert ok
    assert "910.0 Mbps" in msg
    assert "x99.cloud" in msg


def test_speedtest_below_floor_pages_and_names_the_server():
    # TWO sub-floor results, because one is a slow run rather than a slow link (#2785).
    ok, msg = verdicts.host.speedtest_verdict(
        _st_row(
            download_bits=13_800_312,
            data={"server": {"id": 70277, "name": "SUMOFIBER"}},
        ),
        100.0,
        8.0,
        now=ST_NOW,
        prev_rows=[_st_row(id=779, download_bits=20_000_000)],
    )
    assert not ok
    assert "13.8 Mbps" in msg
    assert "SUMOFIBER" in msg


def test_speedtest_floor_is_exclusive_at_the_boundary():
    # Strict `<`, like ups_health: a run exactly at the floor is still ok.
    assert verdicts.host.speedtest_verdict(
        _st_row(download_bits=100_000_000), 100.0, 8.0, now=ST_NOW
    )[0]
    assert not verdicts.host.speedtest_verdict(
        _st_row(download_bits=99_999_999),
        100.0,
        8.0,
        now=ST_NOW,
        prev_rows=[_st_row(id=779, download_bits=99_999_999)],
    )[0]


def test_speedtest_failed_run_pages_with_the_cli_message():
    # download_bits is null on a failed row — the status arm must run BEFORE the floor arm,
    # or this compares None against a float.
    ok, msg = verdicts.host.speedtest_verdict(
        _st_row(
            id=745,
            status="failed",
            download_bits=None,
            data={
                "server": {"id": None},
                "type": "log",
                "level": "error",
                "message": "An unexpected error occurred while running the Ookla CLI.",
            },
        ),
        100.0,
        8.0,
        now=ST_NOW,
    )
    assert not ok
    assert "failed" in msg
    assert "Ookla CLI" in msg


def test_speedtest_stale_run_pages_even_when_it_was_fast():
    # The scheduler dying has no other symptom: the pod still serves its UI and passes both
    # probes, and the last row it did write stays green on status and floor forever.
    ok, msg = verdicts.host.speedtest_verdict(
        _st_row(created_at="2026-08-23 11:00:00"), 100.0, 8.0, now=ST_NOW
    )
    assert not ok
    assert "25.0h ago" in msg


def test_speedtest_stale_message_names_both_causes_and_asserts_neither():
    """The staleness arm must not diagnose, because it cannot.

    It said "the 6-hourly schedule has stopped" until #1483. A failed Ookla run writes no row
    at all — 25 consecutive rows read `completed` on 2026-09-10, ids 825-849 — so a stopped
    scheduler and a failing run reach this function as the same absent row. Asserting either
    one sends the operator down the wrong path half the time.

    The discriminator is outside the API: the container logs one line per scheduled tick, so a
    tick with no row is a failing run and a missing tick is a stopped scheduler. It does not
    survive the pod — Loki held nothing from the pod running at the one real miss, 2026-09-09
    17:00 UTC, while holding other pods' logs from that minute.
    """
    ok, msg = verdicts.host.speedtest_verdict(
        _st_row(created_at="2026-08-23 11:00:00"), 100.0, 8.0, now=ST_NOW
    )
    assert not ok
    assert "25.0h ago" in msg
    # The claim it must no longer make.
    assert "schedule has stopped" not in msg
    # Both hypotheses, and where to go to settle it.
    assert "scheduler" in msg
    assert "failed" in msg
    assert "pod log" in msg


def test_speedtest_bare_timestamp_is_read_as_utc_not_local():
    # The regression this guards: /api/speedtest/latest serializes row 780 as
    # 2026-08-24T06:00:00-05:00 and /api/v1/results serializes it as "2026-08-24 11:00:00".
    # Reading the bare form as Central would make this row 6h old against an 8h ceiling here,
    # and would mask a genuinely stale one by five hours.
    ok, msg = verdicts.host.speedtest_verdict(
        _st_row(created_at="2026-08-24 11:00:00"), 100.0, 8.0, now=ST_NOW
    )
    assert ok
    assert "1.0h ago" in msg


def test_speedtest_offset_aware_timestamp_still_parses():
    # Belt and braces: if the API ever grows an offset, the parse must not double-apply UTC.
    ok, msg = verdicts.host.speedtest_verdict(
        _st_row(created_at="2026-08-24T06:00:00.000000-05:00"), 100.0, 8.0, now=ST_NOW
    )
    assert ok
    assert "1.0h ago" in msg


def test_speedtest_no_rows_at_all_pages():
    ok, msg = verdicts.host.speedtest_verdict(None, 100.0, 8.0, now=ST_NOW)
    assert not ok
    assert "no results" in msg


def test_speedtest_completed_row_without_a_download_figure_pages():
    ok, msg = verdicts.host.speedtest_verdict(
        _st_row(download_bits=None), 100.0, 8.0, now=ST_NOW
    )
    assert not ok
    assert "no download figure" in msg


def test_speedtest_disabled_without_url_or_token(monkeypatch, cfg):
    cfg = replace(cfg, SPEEDTEST_URL="", SPEEDTEST_TOKEN="")
    ok, msg = checks.host_edge.check_speedtest(cfg)
    assert ok
    assert "disabled" in msg


def test_speedtest_fetch_failure_rides_the_streak_but_a_bad_row_does_not(
    monkeypatch, cfg
):
    # The app runs every 6h and this loop every 5 min, so hysteresis on the VERDICT would
    # re-read one row up to 72 times. Only the fetch gets a streak.
    cfg = replace(
        cfg,
        SPEEDTEST_URL="http://speedtest",
        SPEEDTEST_TOKEN="t",
        SPEEDTEST_CONSECUTIVE=2,
    )

    def _boom(*a, **k):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(bridge.net, "_get_json", _boom)
    assert checks.host_edge.check_speedtest(cfg)[
        0
    ]  # first failure is held by the streak
    assert not checks.host_edge.check_speedtest(cfg)[0]  # second pages

    monkeypatch.setattr(
        bridge.net,
        "_get_json",
        lambda *a, **k: {
            "data": [
                _st_row(download_bits=13_800_312),
                _st_row(id=779, download_bits=20_000_000),
            ]
        },
    )
    assert not checks.host_edge.check_speedtest(cfg)[
        0
    ]  # two sub-floor rows page on the FIRST cycle — no cycle streak


def test_speedtest_requests_the_newest_rows_not_the_oldest(monkeypatch, cfg):
    # The API defaults to ASCENDING order, so an unsorted request returns the oldest row in
    # the 30-day window — permanently stale, and stale in a way that looks like a real verdict.
    # The page size is asserted here rather than in a test of its own: the floor arm's history
    # comes from THIS request, so a page of one row could never show a run (#2785).
    cfg = replace(
        cfg,
        SPEEDTEST_URL="http://speedtest",
        SPEEDTEST_TOKEN="t",
        SPEEDTEST_FLOOR_CONSECUTIVE=3,
    )
    seen = {}

    def _capture(url, headers=None):
        seen["url"] = url
        seen["headers"] = headers
        return {"data": [_st_row()]}

    monkeypatch.setattr(bridge.net, "_get_json", _capture)
    checks.host_edge.check_speedtest(cfg)
    assert "sort=-created_at" in seen["url"]
    assert "page%5Bsize%5D=3" in seen["url"]
    assert seen["headers"]["Authorization"] == "Bearer t"


# --- a held BROAD apply needs a different remediation than a held service deploy ----------


# ── the floor arm's run of RESULTS (#2785) ───────────────────────────────────────────────


def test_speedtest_one_sub_floor_result_holds_up_with_a_streak_note():
    # A test runs every 6h, so paging on one result held the tile red for ~6h — three times in
    # the 14 days to 2026-09-27. The note has to say a fault is accumulating.
    ok, msg = verdicts.host.speedtest_verdict(
        _st_row(download_bits=81_500_000),
        100.0,
        8.0,
        now=ST_NOW,
        prev_rows=[_st_row(id=779, download_bits=910_000_000)],
    )
    assert ok
    assert "81.5 Mbps" in msg
    assert "1/2 sub-floor results" in msg


def test_speedtest_single_result_in_the_window_cannot_confirm_a_run():
    # A fresh app, or a single row in the window: no history means no run to see, so hold.
    ok, msg = verdicts.host.speedtest_verdict(
        _st_row(download_bits=81_500_000), 100.0, 8.0, now=ST_NOW, prev_rows=[]
    )
    assert ok
    assert "1/2 sub-floor results" in msg


def test_speedtest_failed_previous_row_is_not_a_sub_floor_result():
    # A failed row records no figure, so it is not evidence of a slow link. The status arm
    # judges a failed row when it is the NEWEST one; here it is only history.
    ok, msg = verdicts.host.speedtest_verdict(
        _st_row(download_bits=81_500_000),
        100.0,
        8.0,
        now=ST_NOW,
        prev_rows=[_st_row(id=779, status="failed", download_bits=None)],
    )
    assert ok
    assert "1/2 sub-floor results" in msg


def test_speedtest_floor_consecutive_one_pages_on_sight():
    ok, msg = verdicts.host.speedtest_verdict(
        _st_row(download_bits=81_500_000),
        100.0,
        8.0,
        now=ST_NOW,
        prev_rows=[],
        floor_consecutive=1,
    )
    assert not ok
    assert "1/1 consecutive results under the floor" in msg


def test_speedtest_stale_newest_row_pages_regardless_of_the_floor_run():
    # The age arm reads the newest row alone — the floor knob must not buy a dead scheduler
    # any grace.
    ok, msg = verdicts.host.speedtest_verdict(
        _st_row(created_at="2026-08-22 11:00:00", download_bits=81_500_000),
        100.0,
        8.0,
        now=ST_NOW,
        prev_rows=[_st_row(id=779, download_bits=910_000_000)],
    )
    assert not ok
    assert "no row written since" in msg
