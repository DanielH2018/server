"""kuma_notify_failures: a notification Kuma tried to send and dropped, and why (#1891, #1895).

The fixtures are the 2026-09-15 14:50 and 2026-09-17 17:00 lines as Loki returned them, byte
for byte: Kuma's colour logger wraps the stamp, the `[MONITOR]` tag and `ERROR:` in ANSI
escapes, the drop line ends at the notification name, and the reason is the NEXT line, also at
ERROR level — #1895 filed it as debug-only, and it is not.
"""

import bridge.net
import checks.logs
import gates
import registry
from verdicts.logs import (
    kuma_notify_failures,
    parse_notify_failure_line,
    parse_notify_reason_line,
)

_DROPPED = (
    "\x1b[36m2026-09-15T14:50:00Z\x1b[0m [\x1b[38;5;119mMONITOR\x1b[0m] "
    "\x1b[31mERROR:\x1b[0m Cannot send notification to Homelab Alerts"
)
_DROPPED_EMAIL = _DROPPED.replace("Homelab Alerts", "Homelab Alerts (Email)")
_SENT = (
    "\x1b[36m2026-09-15T14:50:00Z\x1b[0m [\x1b[38;5;119mMONITOR\x1b[0m] "
    "\x1b[32mINFO:\x1b[0m Sending notification to Homelab Alerts"
)
_REASON_429 = (
    "\x1b[36m2026-09-15T14:50:00Z\x1b[0m [\x1b[38;5;119mMONITOR\x1b[0m] "
    "\x1b[31mERROR:\x1b[0m Error: Request failed with status code 429 (code=ERR_BAD_REQUEST) "
    '(HTTP 429 Too Many Requests) {"message":"Service resource is being rate limited.",'
    '"retry_after":3,"global":false,"code":40062}'
)
_REASON_400 = (
    "\x1b[36m2026-09-17T17:00:04Z\x1b[0m [\x1b[38;5;119mMONITOR\x1b[0m] "
    "\x1b[31mERROR:\x1b[0m Error: Request failed with status code 400 (code=ERR_BAD_REQUEST) "
    '(HTTP 400 Bad Request) {"embeds":["0"]}'
)
# A non-HTTP provider's reason has no `(HTTP …)` to reduce to, and an axios message can carry
# the request URL — for Discord, the webhook secret itself.
_REASON_SMTP = (
    "\x1b[36m2026-09-15T14:50:00Z\x1b[0m [\x1b[38;5;119mMONITOR\x1b[0m] "
    "\x1b[31mERROR:\x1b[0m Error: Invalid login: 535 5.7.8 Username and Password not accepted "
    "https://discord.com/api/webhooks/1/secret"
)
# Kuma's own monitor probes log the same axios text at WARN, and must not count as a reason.
_PROBE_WARN = (
    "\x1b[36m2026-09-13T07:41:30Z\x1b[0m [\x1b[38;5;119mMONITOR\x1b[0m] "
    "\x1b[33mWARN:\x1b[0m Monitor #289 'k3s Scrutiny': Pending: Request failed with status "
    "code 500 | Max retries: 2 | Retry: 1 | Retry Interval: 60 seconds | Type: http"
)


def test_parse_reads_the_name_off_the_ansi_wrapped_line_and_rejects_a_sent_one():
    assert parse_notify_failure_line(_DROPPED) == "Homelab Alerts"
    assert parse_notify_failure_line(_DROPPED_EMAIL) == "Homelab Alerts (Email)"
    assert parse_notify_failure_line(_DROPPED + "\x1b[0m") == "Homelab Alerts"
    assert parse_notify_failure_line(_SENT) is None


def test_parse_reads_the_status_off_the_reason_line_and_rejects_a_probe_warning():
    assert parse_notify_reason_line(_REASON_429) == "HTTP 429 Too Many Requests"
    assert parse_notify_reason_line(_REASON_400) == "HTTP 400 Bad Request"
    assert parse_notify_reason_line(_PROBE_WARN) is None
    assert parse_notify_reason_line(_DROPPED) is None


def test_a_non_http_reason_keeps_its_message_with_any_url_redacted():
    reason = parse_notify_reason_line(_REASON_SMTP)
    assert reason is not None and reason.startswith("Invalid login: 535 5.7.8")
    assert "webhooks" not in reason and "secret" not in reason
    assert len(reason) <= 60


def test_one_dropped_send_is_flagged_by_name_and_reason():
    ok, msg = kuma_notify_failures(
        [(1, _DROPPED), (1, _REASON_429)], "3h", truncated=False
    )
    assert not ok
    assert "1 notification send(s)" in msg
    assert "Homelab Alerts x1" in msg
    assert "reasons: HTTP 429 Too Many Requests x1" in msg
    assert "does not retry" in msg


def test_reasons_are_counted_as_a_set_and_a_missing_one_is_named_not_assumed():
    # Four drops in two seconds on 2026-09-09 20:20 is why reasons are not joined to drops one
    # to one; a drop whose reason line fell outside the window says so rather than borrowing a
    # neighbour's.
    ok, msg = kuma_notify_failures(
        [
            (1, _DROPPED),
            (1, _REASON_429),
            (2, _DROPPED),
            (2, _REASON_400),
            (3, _DROPPED),
        ],
        "3h",
        truncated=False,
    )
    assert not ok
    assert "Homelab Alerts x3" in msg
    assert "HTTP 429 Too Many Requests x1" in msg
    assert "HTTP 400 Bad Request x1" in msg
    assert "reason not logged x1" in msg


def test_a_reason_line_with_no_drop_is_not_a_page():
    ok, _ = kuma_notify_failures([(1, _REASON_429)], "3h", truncated=False)
    assert ok


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


def test_the_logql_reads_kumas_own_container_for_the_drop_and_its_reason():
    logql = checks.logs.KUMA_NOTIFY_FAILURES_LOGQL
    assert '{container="uptime-kuma"}' in logql
    assert "Cannot send notification|ERROR:" in logql, (
        "the reason line is a second ERROR-level line; fetching only the drop line loses it"
    )


def test_the_logql_alternatives_match_the_live_lines_and_not_a_probe_warning():
    # The LogQL regex is RE2 and Python's re is a superset for this pattern, so the same text
    # is checked here against the fixtures the live filter must admit and reject.
    import re

    pattern = re.search(r'\|~ "([^"]+)"', checks.logs.KUMA_NOTIFY_FAILURES_LOGQL).group(
        1
    )
    assert re.search(pattern, _DROPPED)
    assert re.search(pattern, _REASON_429)
    assert not re.search(pattern, _PROBE_WARN)
    assert not re.search(pattern, _SENT)


def test_the_check_is_registered_and_loki_gated():
    names = {c.name for c in registry.build_checks()}
    assert "kuma_notify_failures" in names
    assert "kuma_notify_failures" in gates.LOKI_DEPENDENT


def test_the_check_pages_on_the_live_line_shape(monkeypatch, cfg):
    monkeypatch.setattr(
        bridge.net, "loki_lines", lambda *a, **k: [(1, _DROPPED), (1, _REASON_429)]
    )
    ok, msg = checks.logs.check_kuma_notify_failures(cfg)
    assert not ok
    assert "Homelab Alerts x1" in msg and "3h" in msg
    assert "HTTP 429 Too Many Requests x1" in msg


def test_a_fetch_error_fails_open_and_names_the_owner(monkeypatch, cfg):
    def _raise(*a, **k):
        raise RuntimeError("loki-homelab: timed out")

    monkeypatch.setattr(bridge.net, "loki_lines", _raise)
    ok, msg = checks.logs.check_kuma_notify_failures(cfg)
    assert ok
    assert "timed out" in msg and "Loki Reachable" in msg
