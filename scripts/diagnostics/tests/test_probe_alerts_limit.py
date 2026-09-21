"""`alerts --limit` against Loki's `max_entries_limit_per_query` (#1790)."""

import json

import pytest

from _alert_fixtures import (
    LOKI_OVER_CAP_BODY,
    NOW,
    _query_params,
    _route_alert_fetch,
    _two_day_log,
)
from diagnostics.probe_lib import alerts, cli_parser


def test_a_limit_over_lokis_cap_is_clamped_and_named_rather_than_a_traceback(
    monkeypatch, capsys
):
    # The issue's verify-by (#1790). Loki answers a limit above max_entries_limit_per_query
    # with a plaintext 400, which json.loads used to turn into a JSONDecodeError naming the
    # probe. The cap comes from that body, not from a constant here.
    seen = _route_alert_fetch(
        monkeypatch, {alerts.ALERT_LOGQL: _two_day_log()}, respect_limit=True, cap=10
    )
    ns = cli_parser._build_parser().parse_args(
        ["alerts", "--days", "2", "--limit", "20000"]
    )
    assert alerts.run_alerts(ns, now=NOW) == 0
    out = capsys.readouterr().out
    assert "--limit 20000 is above Loki's max_entries_limit_per_query of 10" in out
    # Every stream was re-fetched at the cap, not just the one that was refused.
    assert {
        _query_params(u)["limit"][0] for u in seen[-len(alerts.ALERT_SOURCES) :]
    } == {"10"}


def test_a_clamped_fetch_still_reports_truncation_at_the_clamped_limit(
    monkeypatch, capsys
):
    # Clamping the URL alone would fetch `cap` rows, compare them against 20000 and print an
    # all-clear. The truncation notice must quote the limit the server applied, and must not
    # send the operator back to `--limit`, which is what just got refused.
    _route_alert_fetch(
        monkeypatch, {alerts.ALERT_LOGQL: _two_day_log()}, respect_limit=True, cap=10
    )
    ns = cli_parser._build_parser().parse_args(
        ["alerts", "--days", "2", "--limit", "20000", "--check", "nothing_matches"]
    )
    assert alerts.run_alerts(ns, now=NOW) == 0
    out = capsys.readouterr().out
    assert "hit --limit 10 log lines" in out
    assert "Narrow --days." in out
    assert "Raise --limit" not in out
    assert out.index("hit --limit") < out.index("no DOWN alerts")


def test_a_limit_under_lokis_cap_is_sent_unchanged(monkeypatch, capsys):
    # The accepting half: the clamp fires only on the rejection, so a limit the server takes
    # reaches it verbatim and the notice stays silent.
    seen = _route_alert_fetch(monkeypatch, {}, respect_limit=True, cap=10)
    ns = cli_parser._build_parser().parse_args(
        ["alerts", "--days", "2", "--limit", "7"]
    )
    assert alerts.run_alerts(ns) == 0
    assert "max_entries_limit_per_query" not in capsys.readouterr().out
    assert {_query_params(u)["limit"][0] for u in seen} == {"7"}


def test_a_clamp_notice_stays_off_stdout_under_json(monkeypatch, capsys):
    _route_alert_fetch(
        monkeypatch, {alerts.ALERT_LOGQL: _two_day_log()}, respect_limit=True, cap=10
    )
    ns = cli_parser._build_parser().parse_args(
        ["alerts", "--days", "2", "--limit", "20000", "--json"]
    )
    assert alerts.run_alerts(ns, now=NOW) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out)
    assert "max_entries_limit_per_query of 10" in captured.err


def test_a_non_json_body_that_is_not_the_cap_exits_with_the_servers_words(monkeypatch):
    # The other half of #1790: any other error body surfaces as the HTTP error it is — the
    # server's text, a clean exit — never a JSONDecodeError. A truncated JSON body counts too.
    _route_alert_fetch(monkeypatch, {}, body="too many outstanding requests")
    ns = cli_parser._build_parser().parse_args(["alerts", "--days", "1"])
    with pytest.raises(SystemExit) as exc:
        alerts.run_alerts(ns)
    assert "too many outstanding requests" in str(exc.value)
    assert exc.value.code != 0


def test_loki_entries_cap_reads_the_cap_and_ignores_other_bodies():
    assert (
        alerts.loki_entries_cap(LOKI_OVER_CAP_BODY.format(limit=20000, cap=5000))
        == 5000
    )
    assert alerts.loki_entries_cap("too many outstanding requests") is None
    assert alerts.loki_entries_cap("") is None
