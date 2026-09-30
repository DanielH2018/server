"""`probe.py` redacts the cast `refresh_token` out of HA log text before printing it.

HA writes the Google Cast system user's `refresh_token` in plaintext whenever a cast fails in
`_handle_signal_show_view` (issue #3015). The record reaches HA's own `system_log` and
`/api/error_log`, which `probe.py ha get error_log` prints verbatim — measured 2026-09-30, when it
landed in an agent transcript. The Alloy stage in `roles/k8s/loki-homelab` keeps new records out of
Loki; this seam keeps the token out of THIS tool's stdout, including for the records Loki already
holds until its 744h retention expires.

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
    monkeypatch.setattr(core, "sops_extract", lambda key: "example.test")
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
