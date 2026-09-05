"""Turning raw text into the values every check reads: env, timestamps, durations, alert text.

None of these touch the network. They are split out because a parsing bug is silent — a
timestamp that fails to parse makes a fresh reading look stale, and a sanitize() that lets
adversary-controlled alert text through reaches Discord verbatim.
"""

from dataclasses import fields, replace

from datetime import datetime, timezone


import bridge.common
import bridge.parsing
from bridge.config import Config, load_config
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


# ── the config's own __repr__ must not carry a credential ───────────────────────────────────
#
# `Config` is a dataclass, so it gets an auto-generated __repr__ over EVERY field. A bare
# `print(cfg)`, an f-string in a log line, or a traceback rendering locals would otherwise put
# 16 secrets into the pod log — the HA long-lived token, both B2 keys, the Cloudflare analytics
# token, the speedtest token, the SMTP password, five Discord webhook URLs and five *arr API
# keys — and promtail ships that log to Loki. Each is marked `field(repr=False)` at its
# declaration in the domain module.
#
# Listed by name rather than derived from `fields()`: a census that asks the dataclass which
# fields are repr=False and then checks those are absent proves nothing, because a field that
# lost its marker would leave the census along with it. The test below anchors this list against
# `fields()` by EQUALITY instead, which is the non-vacuous form — it fails both when a marker is
# added without a sentinel and when a marker is dropped from a name still listed here.
_SECRET_ENV = {
    "HA_TOKEN": "sentinel-ha-token",
    "SPEEDTEST_TOKEN": "sentinel-speedtest-token",
    "B2_PROBE_KEY_ID": "sentinel-b2-key-id",
    "B2_PROBE_APPLICATION_KEY": "sentinel-b2-app-key",
    "CF_ANALYTICS_TOKEN": "sentinel-cf-token",
    "SMTP_PASSWORD": "sentinel-smtp-password",
    "DISCORD_WEBHOOK_URL": "https://discord.example/sentinel-kuma",
    "DISCORD_CROWDSEC_WEBHOOK_URL": "https://discord.example/sentinel-crowdsec",
    "DISCORD_GITOPS_WEBHOOK_URL": "https://discord.example/sentinel-gitops",
    "DISCORD_ARR_WEBHOOK_URL": "https://discord.example/sentinel-arr",
    "DISCORD_HEALTHCHECKS_WEBHOOK_URL": "https://discord.example/sentinel-hc",
    "N8N_API_KEY": "sentinel-n8n-key",
    "SONARR_API_KEY": "sentinel-sonarr-key",
    "RADARR_API_KEY": "sentinel-radarr-key",
    "BAZARR_API_KEY": "sentinel-bazarr-key",
    "PROWLARR_API_KEY": "sentinel-prowlarr-key",
}


def test_repr_hides_every_credential_but_not_ordinary_config():
    """The rejecting half and the accepting half, so a repr that hid EVERYTHING would fail too.

    `@dataclass(repr=False)` on the whole class would satisfy the secrecy assertion while making
    the object useless to debug; the KUMA_URL assertion is what rules that out.
    """
    # The non-vacuity anchor: this test is only as good as _SECRET_ENV is complete, and an
    # empty or shrunken list would still pass every assertion below.
    assert {f.name for f in fields(Config) if not f.repr} == set(_SECRET_ENV), (
        "the marked fields and the sentinel list have diverged — a field gained or lost "
        "`repr=False` without its sentinel following"
    )

    env = dict(_SECRET_ENV)
    env["KUMA_URL"] = "http://uptime-kuma.sentinel:3001"
    cfg = load_config(env)
    rendered = repr(cfg)

    leaked = sorted(n for n, v in _SECRET_ENV.items() if v in rendered)
    assert not leaked, (
        f"repr(Config) carries {leaked} — a print(cfg), an f-string in a log line or a "
        "traceback would ship these to Loki. Mark each `field(repr=False)`."
    )
    # The values really were loaded, so the assertion above is about the repr rather than about
    # a config that never held the secrets in the first place.
    assert cfg.HA_TOKEN == "sentinel-ha-token"
    assert cfg.SMTP_PASSWORD == "sentinel-smtp-password"
    # ...and ordinary configuration is still visible, which is what a repr is for.
    assert "http://uptime-kuma.sentinel:3001" in rendered
