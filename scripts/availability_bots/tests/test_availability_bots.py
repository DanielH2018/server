#!/usr/bin/env python3
"""Tests for the availability-bot shared helpers + the osteria parser.

Covers the pure `parse_availability` (offered party sizes only when the wanted time is on
offer) and the two cross-cutting `common.py` behaviors that fail SILENTLY in production:
the Discord POST must carry a User-Agent (Cloudflare 1010-blocks a UA-less urllib/requests
POST and the bot swallows the error), and a failed run must hit the monitor's `/fail`
endpoint (else the healthcheck stays green through a broken run).

Run: uv run pytest scripts/availability_bots/tests/test_availability_bots.py
"""

import importlib.util
import logging
import os
import sys

# scripts/availability_bots isn't on pyproject's `pythonpath` (only its parents are), so pytest's
# own directory-on-sys.path only reaches this file's `tests/` dir. Add the bots' own directory
# so the bare `import common` below and the by-path bot loads still resolve after the split.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import common

_BOT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "osteria-francescana-bot.py",
)
_spec = importlib.util.spec_from_file_location("osteria_bot", _BOT_PATH)
osteria = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(osteria)

# Both bots are loaded by path rather than imported: their filenames carry a hyphen, so no
# `import` statement can name them. That is also why the scripts reference page reports them
# untested — it credits a test by import or by a `scripts/...` path mention, and this file
# does neither.
_GLENSTONE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "glenstone-bot.py"
)
_gspec = importlib.util.spec_from_file_location("glenstone_bot", _GLENSTONE_PATH)
glenstone = importlib.util.module_from_spec(_gspec)
_gspec.loader.exec_module(glenstone)

_LOG = logging.getLogger("test")


class _Resp:
    def raise_for_status(self):
        pass


# The glenstone bot's own parser. Its failure mode is the opposite of a crash: it decides
# whether to alert AT ALL, so a wrong answer here is a bot that runs green and stays silent
# through the opening it exists to catch.


class _Session:
    """Enough of requests.Session for find_available_dates — .get() returning a payload."""

    def __init__(self, entries):
        self._entries = entries

    def get(self, url, timeout=None):
        class Resp:
            @staticmethod
            def raise_for_status():
                pass

            @staticmethod
            def json():
                return {"calendar": {"_data": self._entries}}

        return Resp()


def test_glenstone_reports_a_watched_date_that_is_not_sold_out():
    session = _Session([{"date": glenstone.TARGET_DATES[0], "status": "available"}])
    assert glenstone.find_available_dates(session) == glenstone.TARGET_DATES[:1]


def test_glenstone_stays_silent_on_a_sold_out_date():
    session = _Session([{"date": glenstone.TARGET_DATES[0], "status": "sold_out"}])
    assert glenstone.find_available_dates(session) == []


def test_glenstone_ignores_an_available_date_nobody_is_watching():
    """The calendar carries every date; alerting on one not in TARGET_DATES is a false alarm."""
    session = _Session([{"date": "1999-01-01", "status": "available"}])
    assert glenstone.find_available_dates(session) == []


def test_glenstone_treats_a_missing_status_as_unavailable():
    """`status` absent means the API changed shape. Failing closed keeps a shape change from
    reading as an opening — the bot alerts on a real offer, not on a parse it did not make."""
    session = _Session([{"date": glenstone.TARGET_DATES[0]}])
    assert glenstone.find_available_dates(session) == []


def test_parse_availability_returns_party_sizes_when_time_offered():
    payload = {
        "people_box": "Table for 2 people or 4 people",
        "hour_box": "12:30 13:00",
    }
    assert osteria.parse_availability(payload, "12:30") == ["2", "4"]


def test_parse_availability_empty_when_wanted_time_absent():
    payload = {"people_box": "2 people", "hour_box": "19:00 20:00"}
    assert osteria.parse_availability(payload, "12:30") == []


def test_parse_availability_empty_when_no_people_offered():
    payload = {"people_box": "", "hour_box": "12:30"}
    assert osteria.parse_availability(payload, "12:30") == []


def test_parse_availability_tolerates_missing_keys():
    assert osteria.parse_availability({}, "12:30") == []


def test_discord_notification_sets_user_agent(monkeypatch):
    captured = {}

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured["headers"] = kwargs.get("headers")
        return _Resp()

    monkeypatch.setattr(common.requests, "post", fake_post)
    common.send_discord_notification("http://example/webhook", "hi", _LOG)
    assert captured["headers"]["User-Agent"] == common.DISCORD_USER_AGENT


def test_discord_notification_never_raises_on_failure(monkeypatch):
    def boom(url, **kwargs):
        raise common.requests.RequestException("network down")

    monkeypatch.setattr(common.requests, "post", boom)
    # must not raise — the caller has already found availability by this point
    common.send_discord_notification("http://example/webhook", "hi", _LOG)


def test_ping_healthcheck_success_hits_base_url(monkeypatch):
    seen = {}

    def fake_get(url, **kwargs):
        seen["url"] = url
        return _Resp()

    monkeypatch.setattr(common.requests, "get", fake_get)
    common.ping_healthcheck("http://hc/uuid", _LOG)
    assert seen["url"] == "http://hc/uuid"


def test_ping_healthcheck_failure_appends_fail(monkeypatch):
    seen = {}

    def fake_get(url, **kwargs):
        seen["url"] = url
        return _Resp()

    monkeypatch.setattr(common.requests, "get", fake_get)
    common.ping_healthcheck("http://hc/uuid/", _LOG, success=False)
    assert seen["url"] == "http://hc/uuid/fail"
