"""`probe.py alerts`: reconstructing DOWN history from Loki.

Kuma keeps only current state, so an episode that has ended exists nowhere else. The reader
takes TWO streams — monitor-bridge's own log, and the `{job="syslog"}` status=down lines the
host crons emit, which push Kuma directly and so leave no other durable record. Reading only the
first left the whole backup/drift plane with no episode history anywhere.
"""

from datetime import UTC, datetime

from diagnostics.probe_lib import alerts
from diagnostics.probe_lib import metrics
from diagnostics.probe_lib.alerts import alert_episodes
import probe
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


def test_parse_down_line_extracts_name_and_strips_cycle_counter():
    line = "[2026-07-21T08:37:00] DOWN n8n - 1 active workflow(s) failed (2 cycles)"
    assert alerts.parse_down_line(line) == ("n8n", "1 active workflow(s) failed")


def test_parse_down_line_ignores_ok_and_malformed_lines():
    assert alerts.parse_down_line("[2026-07-21T08:37:00] OK   n8n - fine") is None
    assert alerts.parse_down_line("not a monitor-bridge line") is None


def test_alert_episodes_splits_on_a_silence_gap():
    minute = int(60 * 1e9)
    rows = [
        (0, "backup", "shrank"),
        (5 * minute, "backup", "shrank"),  # same episode (5m <= 30m gap)
        (60 * minute, "backup", "shrank again"),  # new episode (55m gap)
    ]
    eps = alerts.alert_episodes(rows, gap_s=1800)
    assert len(eps) == 2
    # newest episode first; its latest msg wins
    assert eps[0]["cycles"] == 1 and eps[0]["msg"] == "shrank again"
    assert eps[1]["cycles"] == 2 and eps[1]["first_ns"] == 0


def test_alert_episodes_keeps_distinct_checks_separate():
    rows = [(0, "backup", "a"), (0, "cpu", "b")]
    eps = alerts.alert_episodes(rows, gap_s=1800)
    assert {e["name"] for e in eps} == {"backup", "cpu"}


def test_format_alert_episodes_empty_is_all_clear():
    assert alerts.format_alert_episodes([], 7) == "no DOWN alerts in the last 7d"


def test_format_alert_episodes_renders_name_and_msg():
    eps = [{"name": "n8n", "first_ns": 0, "last_ns": 0, "cycles": 1, "msg": "boom"}]
    out = alerts.format_alert_episodes(eps, 7)
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
    # `run_alerts` passes direction=forward because episode reconstruction walks oldest-first.
    # Copying that here would make --limit return the OLDEST N; Loki's default `backward` is
    # what makes it return the newest N, which format_loki then sorts.
    seen = _capture_fetch(monkeypatch)
    ns = probe._build_parser().parse_args(
        ["loki-query", '{job="syslog"}', "--since", "6h"]
    )
    metrics.run_query(ns)
    assert "direction=" not in seen[0]


def test_run_query_serves_metric_which_has_no_since_flag(monkeypatch):
    # `metric`'s subparser declares no --since and run_query serves both commands, so a bare
    # `ns.since` on the shared path raises AttributeError and kills every `probe.py metric`.
    seen = _capture_fetch(monkeypatch)
    ns = probe._build_parser().parse_args(["metric", "up"])
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


def test_parse_syslog_down_line_reads_the_tag_and_the_message():
    # The rsyslog prefix ("<iso-ts> <host> ") is real and the bare "<tag>: status=down <msg>"
    # shape a reading of the cron scripts suggests never reaches Loki.
    assert alerts.parse_syslog_down_line(SYSLOG_DOWN) == (
        "longhorn-backup-health",
        "backed-up volumes stale or missing: homelab/tdarr-server (weekly-d1)",
    )


def test_parse_syslog_down_line_unwraps_a_failed_push():
    # A failed push is the case where syslog is the ONLY record — Kuma never learned — so the
    # prefix stays in the message rather than being discarded.
    name, msg = alerts.parse_syslog_down_line(SYSLOG_PUSH_FAILED)
    assert name == "claude-otel-health"
    assert msg == "push failed: loki 0/1 ready; prometheus not answering queries"


def test_parse_syslog_down_line_survives_rsyslog_truncation():
    name, msg = alerts.parse_syslog_down_line(SYSLOG_PUSH_FAILED_TRUNCATED)
    assert name == "longhorn-backup-health"
    assert msg.startswith("push failed: backups in Error state:")


def test_parse_syslog_down_line_ignores_up_and_unrelated_lines():
    assert alerts.parse_syslog_down_line("not a syslog line") is None
    assert (
        alerts.parse_syslog_down_line(
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
    ns = probe._build_parser().parse_args(["alerts", "--days", "3"])
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
    ns = probe._build_parser().parse_args(
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
    ns = probe._build_parser().parse_args(["alerts", "--days", "3", "--pi"])
    assert alerts.run_alerts(ns) == 0
    out = capsys.readouterr().out
    assert "pi_pressure" in out
    assert "pi-recovery-health" in out
    assert "n8n" not in out
    assert "longhorn-backup-health" not in out


def test_alerts_dry_run_prints_a_command_per_stream(monkeypatch, capsys):
    monkeypatch.setattr(core, "sops_extract", lambda key: "example.test")
    monkeypatch.setattr(core, "metallb_vip", lambda: "10.0.0.240")
    ns = probe._build_parser().parse_args(["--dry-run", "alerts", "--days", "3"])
    assert alerts.run_alerts(ns) == 0
    out = capsys.readouterr().out
    assert out.count("query_range") == len(alerts.ALERT_SOURCES)


#
# Episode splitting and timestamp rendering. Both halves of the 2026-09-04 misdating (#1104):
# a `*/30` cron's ticks landed exactly on a fixed 30-minute splitting gap, so one 13.5-hour
# outage rendered as 16 episodes and the newest-first list put a mid-incident fragment on top;
# and every row was stamped America/Chicago with no marker, five hours off the `journalctl
# --utc` output beside it. Each rule below is an accept/reject pair — a splitter that merges
# everything and one that merges nothing are indistinguishable from the passing side alone.

_MIN_NS = int(60 * 1e9)


def _cron_run(period_min, count, name="release-staleness-check", start=0, jitter=1):
    """One check's DOWN samples at a fixed period, with a second of cron jitter each tick."""
    return [
        (start + i * period_min * _MIN_NS + i * jitter * int(1e9), name, "stale")
        for i in range(count)
    ]


def test_alert_episodes_merges_a_run_of_ticks_at_the_checks_own_period():
    # The accept half: 28 consecutive `*/30` ticks are ONE outage. A fixed 30-minute gap made
    # this 16 episodes, because 1800s of jitter-free period is not < 1800s.
    eps = alert_episodes(_cron_run(30, 28))
    assert len(eps) == 1
    assert eps[0]["cycles"] == 28
    assert eps[0]["first_ns"] == 0


def test_alert_episodes_splits_when_the_check_recovered_between_runs():
    # The reject half: the same cadence with one UP tick in the middle (a 60-minute silence)
    # is two outages, and an adaptive gap must still say so.
    rows = _cron_run(30, 4) + _cron_run(30, 4, start=5 * 30 * _MIN_NS)
    eps = alert_episodes(rows)
    assert len(eps) == 2


def test_episode_gap_s_derives_the_threshold_from_the_sample_cadence():
    half_hourly = [i * 30 * _MIN_NS for i in range(6)]
    assert alerts.episode_gap_s(half_hourly) == 30 * 60 * 1.5


def test_episode_gap_s_clamps_a_sparse_check_to_the_ceiling():
    # A check that fires once a day has a median delta of a day. Unclamped, that would
    # swallow a week of separate incidents into one episode.
    daily = [i * 24 * 60 * _MIN_NS for i in range(4)]
    assert alerts.episode_gap_s(daily) == alerts._GAP_CEILING_S
    assert len(alert_episodes([(ns, "backup", "gone") for ns in daily])) == 4


def test_episode_gap_s_floors_a_burst_of_near_simultaneous_samples():
    assert alerts.episode_gap_s([0, int(1e9)]) == alerts._GAP_FLOOR_S


def test_episode_gap_s_honours_an_explicit_gap_over_the_cadence():
    assert (
        alerts.episode_gap_s([i * 30 * _MIN_NS for i in range(6)], gap_s=1800) == 1800
    )


def test_format_alert_episodes_stamps_utc_and_carries_the_episode_end():
    # 2026-09-04 00:30 -> 14:00 UTC is the real release-staleness-check outage from #1104,
    # which the Chicago-stamped view rendered as 2026-09-03 19:30.
    first = int(datetime(2026, 9, 4, 0, 30, tzinfo=UTC).timestamp() * 1e9)
    last = int(datetime(2026, 9, 4, 14, 0, tzinfo=UTC).timestamp() * 1e9)
    out = alerts.format_alert_episodes(
        [
            {
                "name": "release-staleness-check",
                "first_ns": first,
                "last_ns": last,
                "cycles": 28,
                "msg": "registry: changed since applied",
            }
        ],
        2,
    )
    assert "times UTC" in out
    assert "2026-09-04 00:30 -> 14:00" in out
    assert "2026-09-03" not in out


def test_format_alert_episodes_keeps_the_date_on_an_end_in_another_day():
    first = int(datetime(2026, 9, 3, 23, 30, tzinfo=UTC).timestamp() * 1e9)
    last = int(datetime(2026, 9, 4, 0, 30, tzinfo=UTC).timestamp() * 1e9)
    out = alerts.format_alert_episodes(
        [
            {
                "name": "backup",
                "first_ns": first,
                "last_ns": last,
                "cycles": 2,
                "msg": "x",
            }
        ],
        2,
    )
    assert "2026-09-03 23:30 -> 2026-09-04 00:30" in out
