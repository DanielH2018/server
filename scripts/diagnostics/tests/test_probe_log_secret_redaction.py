"""`probe.py` redacts the cast `refresh_token` out of HA log text before printing it.

HA writes the Google Cast system user's `refresh_token` in plaintext whenever a cast fails in
`_handle_signal_show_view`. The record reaches HA's own `system_log` and `/api/error_log`, which
`probe.py ha get error_log` prints verbatim. The Alloy stage in `roles/k8s/loki-homelab` keeps new
records out of Loki; this seam keeps the token out of THIS tool's stdout, including for the records
Loki already holds until its 744h retention expires.

Every rule is a `..._is_redacted` / `..._is_printed` pair, so a rule that silently stopped matching
and one that started eating the whole record both fail their own test. What must survive is the
`_handle_signal_show_view` marker: `docs/platform.md` tells the operator to query for it, and it is
the only query that catches a cast timed out on a worker thread.

The last test patches three names, which the monkeypatch ratchet caps at 0 for a new module. The
seam it asks for would go in `probe_lib/ha.py`, and that module sits at exactly its 600-line cap on
`origin/master`, so adding one there fails the length ratchet instead. This module therefore carries
an entry in `ansible/tests/repo/monkeypatch_allowlist.txt`, which its header permits for a path
`origin/master` does not track. Shrink `ha.py`, then take the seam and drop the entry.
"""

import pytest

from diagnostics.probe_lib import core
from diagnostics.probe_lib import ha
from diagnostics.probe_lib import metrics

# The real token's shape (64 hex chars), with no entropy for gitleaks to read as a credential.
TOKEN = "f" * 64

RECORD = (
    "2026-09-29 14:24:06.552 ERROR (SyncWorker_23) [homeassistant.util.logging] Exception in "
    "_handle_signal_show_view when dispatching 'cast_show_view': "
    "({'hass_url': 'https://home-assistant.example', 'hass_uuid': 'abc', 'client_id': None, "
    f"'refresh_token': '{TOKEN}'}}, ChromecastInfo(...), 'lovelace', 0)"
)


def test_refresh_token_is_redacted():
    out = core.redact_log_secrets(RECORD)
    assert TOKEN not in out
    assert "'refresh_token': '<redacted>'" in out


def test_the_rest_of_the_record_is_printed():
    out = core.redact_log_secrets(RECORD)
    assert "_handle_signal_show_view" in out
    assert "cast_show_view" in out
    assert "homeassistant.util.logging" in out
    assert "2026-09-29 14:24:06.552" in out


def test_the_json_spelling_is_redacted():
    out = core.redact_log_secrets(f'{{"refresh_token": "{TOKEN}"}}')
    assert TOKEN not in out
    assert '"refresh_token": "<redacted>"' in out


def test_a_null_refresh_token_is_printed():
    """`None` is not a secret, and rewriting it would hide a real diagnostic."""
    line = "cast failed: {'client_id': None, 'refresh_token': None}"
    assert core.redact_log_secrets(line) == line


def test_an_ordinary_log_line_is_printed():
    line = "2026-09-29 14:23:36.520 WARNING (Thread-5) [pychromecast] Connection reset by peer"
    assert core.redact_log_secrets(line) == line


def test_ha_get_routes_its_body_through_the_redaction(monkeypatch, capsys):
    """The wiring, not the regex: `ha get error_log` must not print the raw body."""
    monkeypatch.setattr(core, "domain", lambda: "example.test")
    monkeypatch.setattr(ha, "ha_token", lambda: "t")
    monkeypatch.setattr(ha, "ha_get", lambda url, token, resolve=None: RECORD)
    ns = type("NS", (), {"ha_cmd": "get", "path": "error_log", "dry_run": False})()
    assert ha.run_ha(ns) == 0
    out = capsys.readouterr().out
    assert TOKEN not in out
    assert "_handle_signal_show_view" in out


def test_format_loki_routes_its_lines_through_the_redaction():
    """A `|= "_handle_signal_show_view"` query over records shipped before the Alloy stage."""
    data = {"data": {"result": [{"values": [["1759000000000000000", RECORD]]}]}}
    out = metrics.format_loki(data)
    assert TOKEN not in out
    assert "_handle_signal_show_view" in out


# Sonarr and Radarr log a failed Torznab call with its full URL, which carries Prowlarr's API key
# as `apikey=<32 hex>` (#6276). Loki keeps the lines shipped before the Alloy stage for 744h, so
# `probe.py loki-query` must redact them on the way out.
APIKEY = "f" * 32

TORZNAB_RECORD = (
    "[Warn] Torznab: Tokyo Toshokan (Prowlarr) server is currently unavailable. "
    f"http://prowlarr:9696/21/api?t=tvsearch&cat=5000&extended=1&apikey={APIKEY}"
    "&offset=0&limit=100"
)


def test_torznab_apikey_is_redacted():
    out = core.redact_log_secrets(TORZNAB_RECORD)
    assert APIKEY not in out
    assert "apikey=<redacted>" in out


def test_an_upper_case_apikey_is_redacted():
    key = "F" * 32
    out = core.redact_log_secrets(TORZNAB_RECORD.replace(APIKEY, key))
    assert key not in out


def test_the_rest_of_the_torznab_record_is_printed():
    out = core.redact_log_secrets(TORZNAB_RECORD)
    assert APIKEY not in out
    assert "Tokyo Toshokan (Prowlarr)" in out
    assert "t=tvsearch" in out
    assert "&offset=0&limit=100" in out


def test_format_loki_redacts_the_torznab_apikey():
    """The loki-query output path, over a Sonarr line shipped before the Alloy stage."""
    data = {"data": {"result": [{"values": [["1761350400000000000", TORZNAB_RECORD]]}]}}
    out = metrics.format_loki(data)
    assert APIKEY not in out
    assert "Torznab" in out


# The same Prowlarr download URL, logged by Radarr's grab path, names its key in other cases and
# lengths than the Torznab line above.
@pytest.mark.parametrize("param", ["apiKey", "token", "passkey", "api_key"])
def test_query_param_credential_in_a_log_line_is_flagged(param):
    line = f"Grab failed: http://prowlarr:9696/7/download?{param}=not-hex-1&link=TGluaw"
    out = core.redact_log_secrets(line)
    assert "not-hex-1" not in out
    assert f"{param}=<redacted>&link=TGluaw" in out


def test_raw_json_log_body_is_clean_past_the_key():
    # `loki-query --json` filters the raw body: the redaction must stop at the string's end.
    body = '["1", "GET /download?apikey=abc123"], ["2", "next line"]'
    assert core.redact_log_secrets(body) == (
        '["1", "GET /download?apikey=<redacted>"], ["2", "next line"]'
    )


def test_json_escaped_ampersand_apikey_is_flagged():
    body = '"GET /api?t=search\\u0026apikey=abc123\\u0026limit=1"'
    out = core.redact_log_secrets(body)
    assert "abc123" not in out
    assert "\\u0026limit=1" in out
