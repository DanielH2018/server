"""`probe.py alerts`: reconstructing DOWN history from Loki.

Kuma keeps only current state, so an episode that has ended exists nowhere else. The reader
takes TWO streams — monitor-bridge's own log, and the `{job="syslog"}` status=down lines the
host crons emit, which push Kuma directly and so leave no other durable record. Reading only the
first left the whole backup/drift plane with no episode history anywhere.
"""

import json
from datetime import UTC, datetime

from diagnostics.probe_lib import alerts
from diagnostics.probe_lib import cli_parser
from diagnostics.probe_lib import metrics
from diagnostics.probe_lib import core


def test_loki_query_url_with_range_adds_start_end_direction():
    url = core.loki_query_url(
        "10.0.0.2", '{job="x"}', 5000, start=1000, end=2000, direction="forward"
    )
    assert "start=1000" in url and "end=2000" in url and "direction=forward" in url


def test_rows_from_loki_flattens_and_sorts_streams():
    data = {
        "data": {
            "result": [
                {"values": [["20", "b"], ["10", "a"]]},
                {"values": [["30", "c"]]},
            ]
        }
    }
    assert core._rows_from_loki(data) == [(10, "a"), (20, "b"), (30, "c")]


def test_rows_from_loki_handles_empty_and_missing_keys():
    assert core._rows_from_loki({}) == []
    assert core._rows_from_loki({"data": {"result": []}}) == []
    assert core._rows_from_loki({"data": {"result": [{"values": None}]}}) == []


#
# These pin the TRANSPORT, deliberately, and the reason is recorded rather than assumed. Three
# assertions already covered `loki_query_url` output and `plan()` argv, and every one of them
# sits UPSTREAM of the defect they would have had to catch: `run_query` built its own URL and
# passed no window, so the formatted path inherited Loki's one-hour server-side default while
# `--dry-run`/`--json` honoured `--since`. Measured before the fix, `--since 3d` returned a
# 60-minute slice — and an empty slice prints "no logs", which reads as health. A fourth
# builder-level assertion would have missed it exactly as the first three did. So: capture the
# url `fetch` is actually called with.


def _capture_fetch(monkeypatch, body='{"data":{"result":[]}}'):
    """Patch out the network and return the list that collects each fetched url."""
    seen = []

    def fake_fetch(url, resolve=None):
        seen.append(url)
        return body

    monkeypatch.setattr(core, "fetch", fake_fetch)
    monkeypatch.setattr(core, "sops_extract", lambda key: "example.test")
    monkeypatch.setattr(core, "metallb_vip", lambda: "10.0.0.240")
    return seen


def _query_params(url):
    from urllib.parse import parse_qs, urlparse

    return parse_qs(urlparse(url).query)


def test_run_query_sends_the_since_window_to_loki(monkeypatch):
    seen = _capture_fetch(monkeypatch)
    ns = cli_parser._build_parser().parse_args(
        ["loki-query", '{job="syslog"}', "--since", "3d", "--limit", "5000"]
    )
    assert metrics.run_query(ns) == 0
    params = _query_params(seen[0])
    assert "start" in params and "end" in params
    # The span, not merely the presence of the key: a start pinned to the wrong clock or a
    # window silently clamped to an hour both satisfy a presence check.
    span_s = (int(params["end"][0]) - int(params["start"][0])) / 1e9
    assert abs(span_s - 3 * 86400) < 2


def test_run_query_without_since_sends_no_window():
    assert core.since_window_ns(None) == (None, None)
    assert core.since_window_ns("") == (None, None)


def test_since_window_ns_span_matches_the_requested_duration():
    start, end = core.since_window_ns("2d")
    assert abs((end - start) / 1e9 - 2 * 86400) < 2


def test_run_query_omits_direction_so_limit_keeps_the_newest_lines(monkeypatch):
    # Loki's default `backward` is what makes --limit return the newest N, which format_loki
    # then sorts. `run_alerts` asks for that same end by name; it passed `forward` until #1782,
    # on a rationale (episode reconstruction walks oldest-first) that alert_episodes' own sort
    # already covers.
    seen = _capture_fetch(monkeypatch)
    ns = cli_parser._build_parser().parse_args(
        ["loki-query", '{job="syslog"}', "--since", "6h"]
    )
    metrics.run_query(ns)
    assert "direction=" not in seen[0]


def test_run_query_serves_metric_which_has_no_since_flag(monkeypatch):
    # `metric`'s subparser declares no --since and run_query serves both commands, so a bare
    # `ns.since` on the shared path raises AttributeError and kills every `probe.py metric`.
    seen = _capture_fetch(monkeypatch)
    ns = cli_parser._build_parser().parse_args(["metric", "up"])
    assert not hasattr(ns, "since")
    assert metrics.run_query(ns) == 0
    assert "/api/v1/query?" in seen[0]


#
# monitor-bridge polls no Kuma state, so its container log says nothing about the host crons
# that push Kuma directly. Reading only that log left the backup plane's sole DOWN signal
# unrecorded: measured 2026-08-22, 465 `longhorn-backup-health: status=down` lines over 7 days
# appeared in no episode list, while `alerts --check manifest` printed "no DOWN alerts" with
# `monitor_status{monitor_name="Manifest Prune Drift"}` reading 0.

SYSLOG_DOWN = (
    "2026-08-19T13:50:03.382504+00:00 daniel-box longhorn-backup-health: "
    "status=down backed-up volumes stale or missing: homelab/tdarr-server (weekly-d1)"
)
SYSLOG_PUSH_FAILED = (
    "2026-08-16T07:40:04.188815+00:00 daniel-box claude-otel-health: "
    "push failed (status=down: loki 0/1 ready; prometheus not answering queries)"
)
SYSLOG_PUSH_FAILED_TRUNCATED = (
    "2026-08-16T10:36:02.000000+00:00 daniel-box longhorn-backup-health: "
    "push failed (status=down: backups in Error state: backup-4a471c15 backup-9818b9cc"
)


def _fake_loki(lines):
    return {"data": {"result": [{"values": [[str(ts), line] for ts, line in lines]}]}}


def _route_alert_fetch(monkeypatch, per_query, respect_limit=False):
    """Serve each alert stream its own body, keyed by the LogQL in the url.

    `respect_limit` makes the fake behave the way Loki does under a cap: it keeps only the lines
    inside the requested window, then `limit` of them from the end `direction` names. Left off,
    every line is served whatever the url asked for — which is right for a test about filtering
    and wrong for a test about truncation, since a fake that ignores the limit passes whichever
    end the real query would have cut.
    """
    import json as _json

    seen = []

    def fake_fetch(url, resolve=None):
        seen.append(url)
        params = _query_params(url)
        lines = per_query.get(params["query"][0], [])
        if respect_limit:
            start, end = int(params["start"][0]), int(params["end"][0])
            limit = int(params["limit"][0])
            window = [(ts, line) for ts, line in lines if start <= ts <= end]
            backward = params.get("direction", ["backward"])[0] == "backward"
            lines = window[-limit:] if backward else window[:limit]
        return _json.dumps(_fake_loki(lines))

    monkeypatch.setattr(core, "fetch", fake_fetch)
    monkeypatch.setattr(core, "sops_extract", lambda key: "example.test")
    monkeypatch.setattr(core, "metallb_vip", lambda: "10.0.0.240")
    return seen


def test_alerts_queries_the_host_cron_stream_as_well_as_the_bridge(monkeypatch):
    seen = _route_alert_fetch(monkeypatch, {})
    ns = cli_parser._build_parser().parse_args(["alerts", "--days", "3"])
    assert alerts.run_alerts(ns) == 0
    queries = [_query_params(u)["query"][0] for u in seen]
    assert alerts.ALERT_LOGQL in queries
    assert alerts.SYSLOG_ALERT_LOGQL in queries


def test_alerts_surfaces_a_host_cron_episode_the_bridge_stream_cannot_see(
    monkeypatch, capsys
):
    """The acceptance case: monitor-bridge's stream is EMPTY and the episode still appears."""
    minute = int(60 * 1e9)
    _route_alert_fetch(
        monkeypatch,
        {
            alerts.ALERT_LOGQL: [],
            alerts.SYSLOG_ALERT_LOGQL: [
                (minute, SYSLOG_DOWN),
                (11 * minute, SYSLOG_DOWN),
            ],
        },
    )
    ns = cli_parser._build_parser().parse_args(["alerts", "--days", "3"])
    assert alerts.run_alerts(ns) == 0
    out = capsys.readouterr().out
    assert "1 DOWN episode(s)" in out
    assert "longhorn-backup-health" in out
    assert "backed-up volumes stale or missing" in out


def test_alerts_check_filter_matches_a_host_cron_tag(monkeypatch, capsys):
    # `--check` has to keep working across both streams, which it does only because the syslog
    # tag is a machine name like monitor-bridge's own check names. Kuma's monitor_name is a
    # DISPLAY name ("Manifest Prune Drift"), so an episode set keyed on it would silently
    # break `--check manifest-prune-check` for every caller.
    minute = int(60 * 1e9)
    _route_alert_fetch(
        monkeypatch,
        {
            alerts.ALERT_LOGQL: [
                (
                    minute,
                    "[2026-08-19T13:50:03] DOWN n8n - 1 workflow failed (2 cycles)",
                )
            ],
            alerts.SYSLOG_ALERT_LOGQL: [(minute, SYSLOG_DOWN)],
        },
    )
    ns = cli_parser._build_parser().parse_args(
        ["alerts", "--days", "3", "--check", "longhorn"]
    )
    assert alerts.run_alerts(ns) == 0
    out = capsys.readouterr().out
    assert "longhorn-backup-health" in out
    assert "n8n" not in out


#
# `alerts --pi`: attributing an alert row to daniel-pi. The syslog stream carries the host's
# own hostname (verified live against the Pi's health.log, not assumed — `hostname` there
# prints "daniel-pi"); the monitor-bridge stream carries no host field at all, so its one
# check that watches the Pi remotely (pi_pressure) is matched by name instead.

PI_SYSLOG_LINE = (
    "2026-09-02T19:24:00+00:00 daniel-pi pi-recovery-health: "
    "status=down not running: autoheal; restarted: autoheal"
)
NON_PI_SYSLOG_LINE = SYSLOG_DOWN  # host is daniel-box


def test_is_pi_alert_accepts_the_pi_syslog_host_token():
    assert alerts.is_pi_alert(
        alerts.SYSLOG_ALERT_LOGQL, PI_SYSLOG_LINE, "pi-recovery-health"
    )


def test_is_pi_alert_rejects_a_non_pi_syslog_host_token():
    assert not alerts.is_pi_alert(
        alerts.SYSLOG_ALERT_LOGQL, NON_PI_SYSLOG_LINE, "longhorn-backup-health"
    )


def test_is_pi_alert_accepts_the_pi_pressure_check_on_the_bridge_stream():
    line = "[2026-09-02T19:05:00] DOWN pi_pressure - load5 2.40/core (5 cycles)"
    assert alerts.is_pi_alert(alerts.ALERT_LOGQL, line, "pi_pressure")


def test_is_pi_alert_rejects_a_different_bridge_check():
    line = "[2026-08-19T13:50:03] DOWN n8n - 1 workflow failed (2 cycles)"
    assert not alerts.is_pi_alert(alerts.ALERT_LOGQL, line, "n8n")


def test_alerts_pi_scopes_to_pi_attributed_rows_across_both_streams(
    monkeypatch, capsys
):
    minute = int(60 * 1e9)
    _route_alert_fetch(
        monkeypatch,
        {
            alerts.ALERT_LOGQL: [
                (
                    minute,
                    "[2026-08-19T13:50:03] DOWN n8n - 1 workflow failed (2 cycles)",
                ),
                (
                    2 * minute,
                    "[2026-09-02T19:05:00] DOWN pi_pressure - load5 2.40/core (5 cycles)",
                ),
            ],
            alerts.SYSLOG_ALERT_LOGQL: [
                (minute, NON_PI_SYSLOG_LINE),
                (2 * minute, PI_SYSLOG_LINE),
            ],
        },
    )
    ns = cli_parser._build_parser().parse_args(["alerts", "--days", "3", "--pi"])
    assert alerts.run_alerts(ns) == 0
    out = capsys.readouterr().out
    assert "pi_pressure" in out
    assert "pi-recovery-health" in out
    assert "n8n" not in out
    assert "longhorn-backup-health" not in out


def test_alerts_dry_run_prints_a_command_per_stream(monkeypatch, capsys):
    monkeypatch.setattr(core, "sops_extract", lambda key: "example.test")
    monkeypatch.setattr(core, "metallb_vip", lambda: "10.0.0.240")
    ns = cli_parser._build_parser().parse_args(["--dry-run", "alerts", "--days", "3"])
    assert alerts.run_alerts(ns) == 0
    out = capsys.readouterr().out
    assert out.count("query_range") == len(alerts.ALERT_SOURCES)


#
# #1782: which fetched lines reach the view, and which end a hit `--limit` cuts. monitor-bridge
# dropped the bracketed stamp from its log lines on 2026-09-04 and `_DOWN_RE` still required it,
# so `alerts --days 2 --check traefik` printed "no DOWN alerts" over a window whose `--raw` view
# held 21 traefik_latency DOWN lines. The same run exposed two more: `--check` and `--pi` did not
# filter `--raw` at all, and a hit `--limit` threw away the NEWEST lines, so a wider window
# listed fewer recent episodes than a narrower one. The parse half is pinned against the real
# emitter in ansible/tests/services/test_monitor_bridge_down_line_shape.py.


def test_keep_alert_row_admits_an_unparsed_line_only_when_unfiltered():
    # The reject half of the None branch: an unreadable line is how `--raw` shows a shape the
    # parsers stopped matching, but it cannot be attributed to a check or a host.
    assert alerts.keep_alert_row(None, False, alerts.ALERT_LOGQL, "gibberish", None)
    assert not alerts.keep_alert_row(
        "traefik", False, alerts.ALERT_LOGQL, "gibberish", None
    )
    assert not alerts.keep_alert_row(None, True, alerts.ALERT_LOGQL, "gibberish", None)


def test_alerts_check_filters_the_raw_view_as_well_as_the_episode_view(
    monkeypatch, capsys
):
    minute = int(60 * 1e9)
    _route_alert_fetch(
        monkeypatch,
        {
            alerts.ALERT_LOGQL: [
                (minute, "DOWN traefik_latency - headlamp 16% of 0.24 rps"),
                (2 * minute, "DOWN arr_queue - 3 stalled grabs"),
            ],
            alerts.SYSLOG_ALERT_LOGQL: [(minute, SYSLOG_DOWN)],
        },
    )
    ns = cli_parser._build_parser().parse_args(
        ["alerts", "--days", "2", "--check", "traefik_latency", "--raw"]
    )
    assert alerts.run_alerts(ns) == 0
    out = capsys.readouterr().out
    assert "traefik_latency" in out
    assert "arr_queue" not in out
    assert "longhorn-backup-health" not in out


def test_alerts_pi_filters_the_raw_view_as_well_as_the_episode_view(
    monkeypatch, capsys
):
    minute = int(60 * 1e9)
    _route_alert_fetch(
        monkeypatch,
        {
            alerts.ALERT_LOGQL: [(minute, "DOWN arr_queue - 3 stalled grabs")],
            alerts.SYSLOG_ALERT_LOGQL: [
                (minute, PI_SYSLOG_LINE),
                (2 * minute, NON_PI_SYSLOG_LINE),
            ],
        },
    )
    ns = cli_parser._build_parser().parse_args(
        ["alerts", "--days", "2", "--pi", "--raw"]
    )
    assert alerts.run_alerts(ns) == 0
    out = capsys.readouterr().out
    assert "pi-recovery-health" in out
    assert "arr_queue" not in out
    assert "longhorn-backup-health" not in out


def test_alerts_raw_without_a_filter_still_prints_a_line_no_parser_reads(
    monkeypatch, capsys
):
    _route_alert_fetch(
        monkeypatch,
        {alerts.ALERT_LOGQL: [(int(60 * 1e9), "DOWN-ish line in some future shape")]},
    )
    ns = cli_parser._build_parser().parse_args(["alerts", "--days", "2", "--raw"])
    assert alerts.run_alerts(ns) == 0
    assert "some future shape" in capsys.readouterr().out


def _two_day_log():
    """40 hourly DOWN lines ending an hour ago: `old_check` for a day, then `new_check`."""
    now_ns = int(datetime.now(UTC).timestamp() * 1e9)
    hour = int(3600 * 1e9)
    return sorted(
        [
            (now_ns - (i + 1) * hour, f"DOWN old_check - stale for {i}h")
            for i in range(20, 40)
        ]
        + [
            (now_ns - (i + 1) * hour, f"DOWN new_check - stale for {i}h")
            for i in range(20)
        ]
    )


def test_a_wider_window_lists_every_episode_the_narrower_one_shows(monkeypatch, capsys):
    # The issue's verify-by. With direction=forward the 2-day window spent its whole limit on the
    # oldest lines, so `new_check` — which the 1-day window listed — vanished from the wider one.
    lines = {alerts.ALERT_LOGQL: _two_day_log()}
    seen = []
    for days in ("1", "2"):
        _route_alert_fetch(monkeypatch, lines, respect_limit=True)
        ns = cli_parser._build_parser().parse_args(
            ["alerts", "--days", days, "--limit", "10"]
        )
        assert alerts.run_alerts(ns) == 0
        seen.append(capsys.readouterr().out)
    narrow, wide = seen
    assert "new_check" in narrow
    assert "new_check" in wide


def test_a_truncated_window_says_so_before_it_reports_an_all_clear(monkeypatch, capsys):
    # Every fetched line is filtered out, so the episode view is empty while the fetch was
    # capped — the run that used to read as a clean bill of health.
    _route_alert_fetch(
        monkeypatch, {alerts.ALERT_LOGQL: _two_day_log()}, respect_limit=True
    )
    ns = cli_parser._build_parser().parse_args(
        ["alerts", "--days", "2", "--limit", "10", "--check", "nothing_matches"]
    )
    assert alerts.run_alerts(ns) == 0
    out = capsys.readouterr().out
    assert "hit --limit 10 log lines" in out
    assert "OLDEST end" in out
    assert out.index("hit --limit") < out.index("no DOWN alerts")


def test_a_truncation_notice_stays_off_stdout_under_json(monkeypatch, capsys):
    # `--json` exists to be piped, so the notice goes to stderr there rather than ahead of the
    # document. It still has to be SAID: a silent cap under --json is the same all-clear.
    _route_alert_fetch(
        monkeypatch, {alerts.ALERT_LOGQL: _two_day_log()}, respect_limit=True
    )
    ns = cli_parser._build_parser().parse_args(
        ["alerts", "--days", "2", "--limit", "10", "--json"]
    )
    assert alerts.run_alerts(ns) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out)
    assert "hit --limit 10" in captured.err


def test_a_zero_limit_reports_no_episodes_rather_than_crashing(monkeypatch, capsys):
    # argparse accepts `--limit 0`, and an empty stream satisfies `0 >= 0`.
    _route_alert_fetch(monkeypatch, {}, respect_limit=True)
    ns = cli_parser._build_parser().parse_args(
        ["alerts", "--days", "2", "--limit", "0"]
    )
    assert alerts.run_alerts(ns) == 0
    assert "no DOWN alerts" in capsys.readouterr().out
