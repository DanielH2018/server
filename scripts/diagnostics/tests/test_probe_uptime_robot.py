"""Tests for `probe.py uptime-robot`, which fails when a documented monitor id is missing or paused.

The payloads follow Uptime Robot's v2 `getMonitors` response: `stat`, and `monitors[]` whose
`id` is an integer and whose `status` 0 means paused.
"""

import argparse
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from diagnostics.probe_lib import core
from diagnostics.probe_lib import uptime_robot as ur

_DOCUMENTED = [
    {"name": "Auth Health", "id": "803868101"},
    {"name": "Jellyfin Health", "id": "803868270"},
]
_UP = 2


def _payload(*monitors):
    return {
        "stat": "ok",
        "monitors": [{"id": i, "status": s} for i, s in monitors],
    }


def test_every_documented_monitor_running_is_clean():
    text, code = ur.format_uptime_robot(
        _DOCUMENTED, _payload((803868101, _UP), (803868270, 9))
    )
    assert code == 0
    assert text.splitlines()[0].startswith("2/2 ")


def test_a_deleted_monitor_is_flagged():
    text, code = ur.format_uptime_robot(_DOCUMENTED, _payload((803868101, _UP)))
    assert code == 1
    assert "missing  803868270  Jellyfin Health" in text


def test_a_paused_monitor_is_flagged():
    text, code = ur.format_uptime_robot(
        _DOCUMENTED, _payload((803868101, ur.PAUSED), (803868270, _UP))
    )
    assert code == 1
    assert "paused   803868101  Auth Health" in text


def test_a_refused_api_call_is_unreadable_not_a_verdict():
    with pytest.raises(core.Unreadable):
        ur.format_uptime_robot(
            _DOCUMENTED, {"stat": "fail", "error": {"type": "invalid_parameter"}}
        )


def test_the_doc_table_yields_the_known_monitors_with_ids():
    rows = ur.documented_monitors()
    assert {r["name"] for r in rows} >= ur.KNOWN_MONITORS
    assert all(r["id"].isdigit() for r in rows)


def test_a_reworded_doc_table_is_unreadable():
    with pytest.raises(core.Unreadable):
        ur.documented_monitors("| Auth Health | 803868101 | no backticks |\n")


def test_the_key_reaches_curl_on_stdin_not_argv():
    seen = {}

    def post(url, config_body):
        seen["url"], seen["config"] = url, config_body
        return '{"stat": "ok", "monitors": []}'

    code = ur.run_uptime_robot(
        argparse.Namespace(dry_run=False), read_key=lambda name: f"<{name}>", post=post
    )
    assert code == 1
    assert "<uptimerobot_api_key>" in seen["config"]
    assert "monitors=803868101-803868270" in seen["config"]
    assert "<uptimerobot_api_key>" not in seen["url"]
