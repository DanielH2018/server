"""Turning raw text into the values every check reads: env, timestamps, durations, alert text.

None of these touch the network. They are split out because a parsing bug is silent — a
timestamp that fails to parse makes a fresh reading look stale, and a sanitize() that lets
adversary-controlled alert text through reaches Discord verbatim.
"""

from datetime import datetime, timezone


import check


def test_env_file_reads_from_file_and_strips(monkeypatch, tmp_path):
    f = tmp_path / "secret"
    # trailing newline from a rendered file must be stripped
    f.write_text("s3cret-token\n")
    monkeypatch.setenv("HA_TOKEN_FILE", str(f))
    monkeypatch.setenv("HA_TOKEN", "inline-should-be-ignored")
    assert check._env_file("HA_TOKEN", "") == "s3cret-token"


def test_env_file_falls_back_to_plain_env(monkeypatch):
    monkeypatch.delenv("HA_TOKEN_FILE", raising=False)
    monkeypatch.setenv("HA_TOKEN", "inline-token")
    assert check._env_file("HA_TOKEN", "") == "inline-token"


def test_env_file_default_when_neither_set(monkeypatch):
    monkeypatch.delenv("HA_TOKEN_FILE", raising=False)
    monkeypatch.delenv("HA_TOKEN", raising=False)
    assert check._env_file("HA_TOKEN", "") == ""


def test_env_file_missing_file_falls_back_to_env(monkeypatch, tmp_path):
    # A *_FILE path that doesn't exist must degrade to the plain env var, not raise — _env_file runs
    # at import for HA_TOKEN, so an unguarded open() would crash the whole loop and silence every
    # monitor over one missing file (2026-07-15 review L1).
    monkeypatch.setenv("HA_TOKEN_FILE", str(tmp_path / "does-not-exist"))
    monkeypatch.setenv("HA_TOKEN", "inline-fallback")
    assert check._env_file("HA_TOKEN", "") == "inline-fallback"


def test_env_file_directory_path_falls_back_to_env(monkeypatch, tmp_path):
    # The specific Docker failure mode: an absent bind-mount source is created as a directory, so
    # open() raises IsADirectoryError (an OSError subclass) — must still fall back to the env var.
    monkeypatch.setenv("HA_TOKEN_FILE", str(tmp_path))  # tmp_path is a directory
    monkeypatch.setenv("HA_TOKEN", "inline-fallback")
    assert check._env_file("HA_TOKEN", "") == "inline-fallback"


def test_nanosecond_precision_with_z():
    # Real Kopia value: 9 fractional digits + trailing Z
    dt = check.parse_rfc3339("2026-06-06T00:00:00.011699074Z")
    assert dt.tzinfo == timezone.utc
    assert dt.year == 2026
    assert dt.microsecond == 11699  # truncated from .011699074


def test_plain_z_no_fraction():
    dt = check.parse_rfc3339("2026-06-06T00:00:00Z")
    assert dt == datetime(2026, 6, 6, tzinfo=timezone.utc)


def test_offset_after_fraction():
    dt = check.parse_rfc3339("2026-06-06T01:00:00.123456789+01:00")
    assert dt.utcoffset().total_seconds() == 3600
    assert dt.microsecond == 123456


def test_parse_duration_units():
    assert check.parse_duration("900s") == 900
    assert check.parse_duration("15m") == 900
    assert check.parse_duration("1h") == 3600
    assert check.parse_duration("2d") == 172800
    assert check.parse_duration("300") == 300  # bare number = seconds


def test_sanitize_defuses_discord_mentions_and_markdown():
    # A poisoned release title / indexer name must not ping the channel or break formatting.
    out = check.sanitize("@everyone `rm -rf`\nsee @here")
    assert "@" not in out
    assert "`" not in out
    assert "\n" not in out


def test_sanitize_caps_length():
    assert len(check.sanitize("A" * 500)) <= 120


def test_sanitize_handles_none():
    assert check.sanitize(None) == "?"


def test_sanitize_collapses_whitespace():
    assert check.sanitize("a\t b\n\nc") == "a b c"


def test_arr_queue_msg_is_sanitized(monkeypatch):
    # An @everyone-laden release title reaches the alert msg defused, not as a live ping.
    monkeypatch.setattr(check, "SONARR_API_KEY", "k")
    monkeypatch.setattr(check, "RADARR_API_KEY", "")
    queue = {
        "records": [
            {"title": "@everyone Free.Movie", "trackedDownloadStatus": "warning"}
        ]
    }
    monkeypatch.setattr(check, "_get_json", lambda *a, **k: queue)
    ok, msg = check.check_arr_queue()
    assert ok is False
    assert "@everyone" not in msg
    assert "(at)everyone" in msg
