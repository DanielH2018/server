"""`probe.py alerts`: reconstructing DOWN history from Loki.

Kuma keeps only current state, so an episode that has ended exists nowhere else. The reader
takes TWO streams — monitor-bridge's own log, and the `{job="syslog"}` status=down lines the
host crons emit, which push Kuma directly and so leave no other durable record. Reading only the
first left the whole backup/drift plane with no episode history anywhere.
"""

import probe
import probe_core as core


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
    assert probe._rows_from_loki(data) == [(10, "a"), (20, "b"), (30, "c")]


def test_rows_from_loki_handles_empty_and_missing_keys():
    assert probe._rows_from_loki({}) == []
    assert probe._rows_from_loki({"data": {"result": []}}) == []
    assert probe._rows_from_loki({"data": {"result": [{"values": None}]}}) == []


def test_parse_down_line_extracts_name_and_strips_cycle_counter():
    line = "[2026-07-21T08:37:00] DOWN n8n - 1 active workflow(s) failed (2 cycles)"
    assert probe.parse_down_line(line) == ("n8n", "1 active workflow(s) failed")


def test_parse_down_line_ignores_ok_and_malformed_lines():
    assert probe.parse_down_line("[2026-07-21T08:37:00] OK   n8n - fine") is None
    assert probe.parse_down_line("not a monitor-bridge line") is None


def test_alert_episodes_splits_on_a_silence_gap():
    minute = int(60 * 1e9)
    rows = [
        (0, "backup", "shrank"),
        (5 * minute, "backup", "shrank"),  # same episode (5m <= 30m gap)
        (60 * minute, "backup", "shrank again"),  # new episode (55m gap)
    ]
    eps = probe.alert_episodes(rows, gap_s=1800)
    assert len(eps) == 2
    # newest episode first; its latest msg wins
    assert eps[0]["cycles"] == 1 and eps[0]["msg"] == "shrank again"
    assert eps[1]["cycles"] == 2 and eps[1]["first_ns"] == 0


def test_alert_episodes_keeps_distinct_checks_separate():
    rows = [(0, "backup", "a"), (0, "cpu", "b")]
    eps = probe.alert_episodes(rows, gap_s=1800)
    assert {e["name"] for e in eps} == {"backup", "cpu"}


def test_format_alert_episodes_empty_is_all_clear():
    assert probe.format_alert_episodes([], 7) == "no DOWN alerts in the last 7d"


def test_format_alert_episodes_renders_name_and_msg():
    eps = [{"name": "n8n", "first_ns": 0, "last_ns": 0, "cycles": 1, "msg": "boom"}]
    out = probe.format_alert_episodes(eps, 7)
    assert "1 DOWN episode(s)" in out and "n8n" in out and "boom" in out


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
    ns = probe._build_parser().parse_args(
        ["loki-query", '{job="syslog"}', "--since", "3d", "--limit", "5000"]
    )
    assert probe.run_query(ns) == 0
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
    # `run_alerts` passes direction=forward because episode reconstruction walks oldest-first.
    # Copying that here would make --limit return the OLDEST N; Loki's default `backward` is
    # what makes it return the newest N, which format_loki then sorts.
    seen = _capture_fetch(monkeypatch)
    ns = probe._build_parser().parse_args(
        ["loki-query", '{job="syslog"}', "--since", "6h"]
    )
    probe.run_query(ns)
    assert "direction=" not in seen[0]


def test_run_query_serves_metric_which_has_no_since_flag(monkeypatch):
    # `metric`'s subparser declares no --since and run_query serves both commands, so a bare
    # `ns.since` on the shared path raises AttributeError and kills every `probe.py metric`.
    seen = _capture_fetch(monkeypatch)
    ns = probe._build_parser().parse_args(["metric", "up"])
    assert not hasattr(ns, "since")
    assert probe.run_query(ns) == 0
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


def test_parse_syslog_down_line_reads_the_tag_and_the_message():
    # The rsyslog prefix ("<iso-ts> <host> ") is real and the bare "<tag>: status=down <msg>"
    # shape a reading of the cron scripts suggests never reaches Loki.
    assert probe.parse_syslog_down_line(SYSLOG_DOWN) == (
        "longhorn-backup-health",
        "backed-up volumes stale or missing: homelab/tdarr-server (weekly-d1)",
    )


def test_parse_syslog_down_line_unwraps_a_failed_push():
    # A failed push is the case where syslog is the ONLY record — Kuma never learned — so the
    # prefix stays in the message rather than being discarded.
    name, msg = probe.parse_syslog_down_line(SYSLOG_PUSH_FAILED)
    assert name == "claude-otel-health"
    assert msg == "push failed: loki 0/1 ready; prometheus not answering queries"


def test_parse_syslog_down_line_survives_rsyslog_truncation():
    name, msg = probe.parse_syslog_down_line(SYSLOG_PUSH_FAILED_TRUNCATED)
    assert name == "longhorn-backup-health"
    assert msg.startswith("push failed: backups in Error state:")


def test_parse_syslog_down_line_ignores_up_and_unrelated_lines():
    assert probe.parse_syslog_down_line("not a syslog line") is None
    assert (
        probe.parse_syslog_down_line(
            "2026-08-20T12:40:03+00:00 daniel-box disk-health: status=up / at 22%"
        )
        is None
    )


def _fake_loki(lines):
    return {"data": {"result": [{"values": [[str(ts), line] for ts, line in lines]}]}}


def _route_alert_fetch(monkeypatch, per_query):
    """Serve each alert stream its own body, keyed by the LogQL in the url."""
    import json as _json

    seen = []

    def fake_fetch(url, resolve=None):
        seen.append(url)
        logql = _query_params(url)["query"][0]
        return _json.dumps(_fake_loki(per_query.get(logql, [])))

    monkeypatch.setattr(core, "fetch", fake_fetch)
    monkeypatch.setattr(core, "sops_extract", lambda key: "example.test")
    monkeypatch.setattr(core, "metallb_vip", lambda: "10.0.0.240")
    return seen


def test_alerts_queries_the_host_cron_stream_as_well_as_the_bridge(monkeypatch):
    seen = _route_alert_fetch(monkeypatch, {})
    ns = probe._build_parser().parse_args(["alerts", "--days", "3"])
    assert probe.run_alerts(ns) == 0
    queries = [_query_params(u)["query"][0] for u in seen]
    assert probe.ALERT_LOGQL in queries
    assert probe.SYSLOG_ALERT_LOGQL in queries


def test_alerts_surfaces_a_host_cron_episode_the_bridge_stream_cannot_see(
    monkeypatch, capsys
):
    """The acceptance case: monitor-bridge's stream is EMPTY and the episode still appears."""
    minute = int(60 * 1e9)
    _route_alert_fetch(
        monkeypatch,
        {
            probe.ALERT_LOGQL: [],
            probe.SYSLOG_ALERT_LOGQL: [
                (minute, SYSLOG_DOWN),
                (11 * minute, SYSLOG_DOWN),
            ],
        },
    )
    ns = probe._build_parser().parse_args(["alerts", "--days", "3"])
    assert probe.run_alerts(ns) == 0
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
            probe.ALERT_LOGQL: [
                (
                    minute,
                    "[2026-08-19T13:50:03] DOWN n8n - 1 workflow failed (2 cycles)",
                )
            ],
            probe.SYSLOG_ALERT_LOGQL: [(minute, SYSLOG_DOWN)],
        },
    )
    ns = probe._build_parser().parse_args(
        ["alerts", "--days", "3", "--check", "longhorn"]
    )
    assert probe.run_alerts(ns) == 0
    out = capsys.readouterr().out
    assert "longhorn-backup-health" in out
    assert "n8n" not in out


def test_alerts_dry_run_prints_a_command_per_stream(monkeypatch, capsys):
    monkeypatch.setattr(core, "sops_extract", lambda key: "example.test")
    monkeypatch.setattr(core, "metallb_vip", lambda: "10.0.0.240")
    ns = probe._build_parser().parse_args(["--dry-run", "alerts", "--days", "3"])
    assert probe.run_alerts(ns) == 0
    out = capsys.readouterr().out
    assert out.count("query_range") == len(probe.ALERT_SOURCES)
