"""speedtest-tracker's newest result row.

`speedtest_verdict` judges one row of /api/v1/results. The rows here are trimmed copies of real
ones (ids 780 and 745, fetched 2026-08-24) — including `created_at`'s bare, offset-less UTC
serialization, which is the detail the age arm turns on.
"""

from datetime import datetime, timezone
from pathlib import Path


import bridge_config
import bridge_io
import checks_host

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
    ok, msg = checks_host.speedtest_verdict(_st_row(), 100.0, 8.0, now=ST_NOW)
    assert ok
    assert "910.0 Mbps" in msg
    assert "x99.cloud" in msg


def test_speedtest_below_floor_pages_and_names_the_server():
    ok, msg = checks_host.speedtest_verdict(
        _st_row(
            download_bits=13_800_312,
            data={"server": {"id": 70277, "name": "SUMOFIBER"}},
        ),
        100.0,
        8.0,
        now=ST_NOW,
    )
    assert not ok
    assert "13.8 Mbps" in msg
    assert "SUMOFIBER" in msg


def test_speedtest_floor_is_exclusive_at_the_boundary():
    # Strict `<`, like ups_health: a run exactly at the floor is still ok.
    assert checks_host.speedtest_verdict(
        _st_row(download_bits=100_000_000), 100.0, 8.0, now=ST_NOW
    )[0]
    assert not checks_host.speedtest_verdict(
        _st_row(download_bits=99_999_999), 100.0, 8.0, now=ST_NOW
    )[0]


def test_speedtest_failed_run_pages_with_the_cli_message():
    # download_bits is null on a failed row — the status arm must run BEFORE the floor arm,
    # or this compares None against a float.
    ok, msg = checks_host.speedtest_verdict(
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
    ok, msg = checks_host.speedtest_verdict(
        _st_row(created_at="2026-08-23 11:00:00"), 100.0, 8.0, now=ST_NOW
    )
    assert not ok
    assert "25.0h ago" in msg


def test_speedtest_bare_timestamp_is_read_as_utc_not_local():
    # The regression this guards: /api/speedtest/latest serializes row 780 as
    # 2026-08-24T06:00:00-05:00 and /api/v1/results serializes it as "2026-08-24 11:00:00".
    # Reading the bare form as Central would make this row 6h old against an 8h ceiling here,
    # and would mask a genuinely stale one by five hours.
    ok, msg = checks_host.speedtest_verdict(
        _st_row(created_at="2026-08-24 11:00:00"), 100.0, 8.0, now=ST_NOW
    )
    assert ok
    assert "1.0h ago" in msg


def test_speedtest_offset_aware_timestamp_still_parses():
    # Belt and braces: if the API ever grows an offset, the parse must not double-apply UTC.
    ok, msg = checks_host.speedtest_verdict(
        _st_row(created_at="2026-08-24T06:00:00.000000-05:00"), 100.0, 8.0, now=ST_NOW
    )
    assert ok
    assert "1.0h ago" in msg


def test_speedtest_no_rows_at_all_pages():
    ok, msg = checks_host.speedtest_verdict(None, 100.0, 8.0, now=ST_NOW)
    assert not ok
    assert "no results" in msg


def test_speedtest_completed_row_without_a_download_figure_pages():
    ok, msg = checks_host.speedtest_verdict(
        _st_row(download_bits=None), 100.0, 8.0, now=ST_NOW
    )
    assert not ok
    assert "no download figure" in msg


def test_speedtest_disabled_without_url_or_token(monkeypatch):
    monkeypatch.setattr(bridge_config, "SPEEDTEST_URL", "")
    monkeypatch.setattr(bridge_config, "SPEEDTEST_TOKEN", "")
    ok, msg = checks_host.check_speedtest()
    assert ok
    assert "disabled" in msg


def test_speedtest_fetch_failure_rides_the_streak_but_a_bad_row_does_not(monkeypatch):
    # The app runs every 6h and this loop every 5 min, so hysteresis on the VERDICT would
    # re-read one row up to 72 times. Only the fetch gets a streak.
    monkeypatch.setattr(bridge_config, "SPEEDTEST_URL", "http://speedtest")
    monkeypatch.setattr(bridge_config, "SPEEDTEST_TOKEN", "t")
    monkeypatch.setattr(bridge_config, "SPEEDTEST_CONSECUTIVE", 2)

    def _boom(*a, **k):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(bridge_io, "_get_json", _boom)
    assert checks_host.check_speedtest()[0]  # first failure is held by the streak
    assert not checks_host.check_speedtest()[0]  # second pages

    monkeypatch.setattr(
        bridge_io,
        "_get_json",
        lambda *a, **k: {"data": [_st_row(download_bits=13_800_312)]},
    )
    assert not checks_host.check_speedtest()[0]  # a bad row pages on the FIRST cycle


def test_speedtest_requests_the_newest_row_not_the_oldest(monkeypatch):
    # The API defaults to ASCENDING order, so an unsorted request returns the oldest row in
    # the 30-day window — permanently stale, and stale in a way that looks like a real verdict.
    monkeypatch.setattr(bridge_config, "SPEEDTEST_URL", "http://speedtest")
    monkeypatch.setattr(bridge_config, "SPEEDTEST_TOKEN", "t")
    seen = {}

    def _capture(url, headers=None):
        seen["url"] = url
        seen["headers"] = headers
        return {"data": [_st_row()]}

    monkeypatch.setattr(bridge_io, "_get_json", _capture)
    checks_host.check_speedtest()
    assert "sort=-created_at" in seen["url"]
    assert seen["headers"]["Authorization"] == "Bearer t"


# --- a held BROAD apply needs a different remediation than a held service deploy ----------
