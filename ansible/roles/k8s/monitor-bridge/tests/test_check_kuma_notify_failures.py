"""kuma_notify_failures: a notification Kuma tried to send and dropped (#1891).

The positive fixture is the 2026-09-15 14:50 line as Loki returned it, byte for byte: Kuma's
colour logger wraps the stamp, the `[MONITOR]` tag and `ERROR:` in ANSI escapes, and the line
ends at the notification name with no reason code — the 429 in the issue is from Kuma's debug
log, which Loki does not carry.
"""

import bridge.net
import checks.logs
import gates
import registry
from verdicts.logs import kuma_notify_failures, parse_notify_failure_line

_DROPPED = (
    "\x1b[36m2026-09-15T14:50:00Z\x1b[0m [\x1b[38;5;119mMONITOR\x1b[0m] "
    "\x1b[31mERROR:\x1b[0m Cannot send notification to Homelab Alerts"
)
_DROPPED_EMAIL = _DROPPED.replace("Homelab Alerts", "Homelab Alerts (Email)")
_SENT = (
    "\x1b[36m2026-09-15T14:50:00Z\x1b[0m [\x1b[38;5;119mMONITOR\x1b[0m] "
    "\x1b[32mINFO:\x1b[0m Sending notification to Homelab Alerts"
)


def test_parse_reads_the_name_off_the_ansi_wrapped_line_and_rejects_a_sent_one():
    assert parse_notify_failure_line(_DROPPED) == "Homelab Alerts"
    assert parse_notify_failure_line(_DROPPED_EMAIL) == "Homelab Alerts (Email)"
    assert parse_notify_failure_line(_DROPPED + "\x1b[0m") == "Homelab Alerts"
    assert parse_notify_failure_line(_SENT) is None


def test_one_dropped_send_is_flagged_by_name():
    ok, msg = kuma_notify_failures([(1, _DROPPED)], "3h", truncated=False)
    assert not ok
    assert "1 notification send(s)" in msg
    assert "Homelab Alerts x1" in msg
    assert "does not retry" in msg


def test_drops_are_counted_per_notification():
    ok, msg = kuma_notify_failures(
        [(1, _DROPPED), (2, _DROPPED_EMAIL), (3, _DROPPED)], "3h", truncated=False
    )
    assert not ok
    assert "3 notification send(s)" in msg
    assert "Homelab Alerts (Email) x1" in msg and "Homelab Alerts x2" in msg


def test_a_window_with_no_drops_is_clean():
    ok, msg = kuma_notify_failures([], "3h", truncated=False)
    assert ok
    assert "no dropped Kuma notifications in 3h" == msg


def test_a_line_that_is_not_a_drop_is_ignored():
    ok, _ = kuma_notify_failures([(1, _SENT)], "3h", truncated=False)
    assert ok


def test_a_capped_fetch_says_so_rather_than_reading_clean():
    ok, msg = kuma_notify_failures([], "3h", truncated=True)
    assert ok
    assert "line cap" in msg


def test_the_logql_reads_kumas_own_container_for_the_fixed_prefix():
    assert '{container="uptime-kuma"}' in checks.logs.KUMA_NOTIFY_FAILURES_LOGQL
    assert '|= "Cannot send notification"' in checks.logs.KUMA_NOTIFY_FAILURES_LOGQL


def test_the_check_is_registered_and_loki_gated():
    names = {c.name for c in registry.build_checks()}
    assert "kuma_notify_failures" in names
    assert "kuma_notify_failures" in gates.LOKI_DEPENDENT


def test_the_check_pages_on_the_live_line_shape(monkeypatch, cfg):
    monkeypatch.setattr(bridge.net, "loki_lines", lambda *a, **k: [(1, _DROPPED)])
    ok, msg = checks.logs.check_kuma_notify_failures(cfg)
    assert not ok
    assert "Homelab Alerts x1" in msg and "3h" in msg


def test_a_fetch_error_fails_open_and_names_the_owner(monkeypatch, cfg):
    def _raise(*a, **k):
        raise RuntimeError("loki-homelab: timed out")

    monkeypatch.setattr(bridge.net, "loki_lines", _raise)
    ok, msg = checks.logs.check_kuma_notify_failures(cfg)
    assert ok
    assert "timed out" in msg and "Loki Reachable" in msg
