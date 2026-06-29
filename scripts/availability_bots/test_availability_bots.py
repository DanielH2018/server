#!/usr/bin/env python3
"""Tests for the availability-bot shared helpers + the osteria parser.

Covers the pure `parse_availability` (offered party sizes only when the wanted time is on
offer) and the two cross-cutting `common.py` behaviors that fail SILENTLY in production:
the Discord POST must carry a User-Agent (Cloudflare 1010-blocks a UA-less urllib/requests
POST and the bot swallows the error), and a failed run must hit the monitor's `/fail`
endpoint (else the healthcheck stays green through a broken run).

Run: uv run pytest scripts/availability_bots/test_availability_bots.py
"""

import importlib.util
import logging
import os

import common

_BOT_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "osteria-francescana-bot.py"
)
_spec = importlib.util.spec_from_file_location("osteria_bot", _BOT_PATH)
osteria = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(osteria)

_LOG = logging.getLogger("test")


class _Resp:
    def raise_for_status(self):
        pass


# --- parse_availability (pure) ----------------------------------------------


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


# --- Discord notification carries the homelab UA (Cloudflare 1010 guard) -----


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


# --- ping_healthcheck /fail routing -----------------------------------------


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
