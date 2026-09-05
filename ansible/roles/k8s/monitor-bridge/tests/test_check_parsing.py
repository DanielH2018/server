"""Turning raw text into the values every check reads: env, timestamps, durations, alert text.

None of these touch the network. They are split out because a parsing bug is silent — a
timestamp that fails to parse makes a fresh reading look stale, and a sanitize() that lets
adversary-controlled alert text through reaches Discord verbatim.
"""

from dataclasses import replace

from datetime import datetime, timezone


import bridge.common
import bridge.parsing
from bridge.config import load_config
import bridge.net
import checks.service


# The `_FILE` indirection is exercised through the field it produces rather than through
# load_config's private reader, because the field is what every check actually consumes.


def test_env_file_reads_from_file_and_strips(tmp_path):
    f = tmp_path / "secret"
    # trailing newline from a rendered file must be stripped
    f.write_text("s3cret-token\n")
    env = {"HA_TOKEN_FILE": str(f), "HA_TOKEN": "inline-should-be-ignored"}
    assert load_config(env).HA_TOKEN == "s3cret-token"


def test_env_file_falls_back_to_plain_env():
    assert load_config({"HA_TOKEN": "inline-token"}).HA_TOKEN == "inline-token"


def test_env_file_default_when_neither_set():
    assert load_config({}).HA_TOKEN == ""


def test_env_file_missing_file_falls_back_to_env(tmp_path):
    # A *_FILE path that doesn't exist must degrade to the plain env var, not raise — an
    # unguarded open() would fail the whole config build and silence every monitor over one
    # missing file (2026-07-15 review L1).
    env = {
        "HA_TOKEN_FILE": str(tmp_path / "does-not-exist"),
        "HA_TOKEN": "inline-fallback",
    }
    assert load_config(env).HA_TOKEN == "inline-fallback"


def test_env_file_directory_path_falls_back_to_env(tmp_path):
    # The specific mount failure mode: an absent mount source is created as a directory, so
    # open() raises IsADirectoryError (an OSError subclass) — must still fall back to the env var.
    env = {"HA_TOKEN_FILE": str(tmp_path), "HA_TOKEN": "inline-fallback"}
    assert load_config(env).HA_TOKEN == "inline-fallback"


def test_nanosecond_precision_with_z():
    # Real Kopia value: 9 fractional digits + trailing Z
    dt = bridge.parsing.parse_rfc3339("2026-06-06T00:00:00.011699074Z")
    assert dt.tzinfo == timezone.utc
    assert dt.year == 2026
    assert dt.microsecond == 11699  # truncated from .011699074


def test_plain_z_no_fraction():
    dt = bridge.parsing.parse_rfc3339("2026-06-06T00:00:00Z")
    assert dt == datetime(2026, 6, 6, tzinfo=timezone.utc)


def test_offset_after_fraction():
    dt = bridge.parsing.parse_rfc3339("2026-06-06T01:00:00.123456789+01:00")
    assert dt.utcoffset().total_seconds() == 3600
    assert dt.microsecond == 123456


def test_parse_duration_units():
    assert bridge.parsing.parse_duration("900s") == 900
    assert bridge.parsing.parse_duration("15m") == 900
    assert bridge.parsing.parse_duration("1h") == 3600
    assert bridge.parsing.parse_duration("2d") == 172800
    assert bridge.parsing.parse_duration("300") == 300  # bare number = seconds


def test_sanitize_defuses_discord_mentions_and_markdown():
    # A poisoned release title / indexer name must not ping the channel or break formatting.
    out = bridge.common.sanitize("@everyone `rm -rf`\nsee @here")
    assert "@" not in out
    assert "`" not in out
    assert "\n" not in out


def test_sanitize_caps_length():
    assert len(bridge.common.sanitize("A" * 500)) <= 120


def test_sanitize_handles_none():
    assert bridge.common.sanitize(None) == "?"


def test_sanitize_collapses_whitespace():
    assert bridge.common.sanitize("a\t b\n\nc") == "a b c"


def test_arr_queue_msg_is_sanitized(monkeypatch, cfg):
    # An @everyone-laden release title reaches the alert msg defused, not as a live ping.
    cfg = replace(cfg, SONARR_API_KEY="k", RADARR_API_KEY="")
    queue = {
        "records": [
            {"title": "@everyone Free.Movie", "trackedDownloadStatus": "warning"}
        ]
    }
    monkeypatch.setattr(bridge.net, "_get_json", lambda *a, **k: queue)
    ok, msg = checks.service.check_arr_queue(cfg)
    assert ok is False
    assert "@everyone" not in msg
    assert "(at)everyone" in msg
