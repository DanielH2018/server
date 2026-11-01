"""`probe.py alerts`, firing history from `ALERTS`, and the Loki transport `loki-query` uses."""

import pytest

from diagnostics.probe_lib import alerts
from diagnostics.probe_lib import cli_parser
from diagnostics.probe_lib import metrics
from diagnostics.probe_lib import core
from diagnostics.probe_lib import pi_plane
from lib.exit_codes import PROBE_UNREADABLE
from urllib.parse import parse_qs, urlparse


def _query_params(url):
    return parse_qs(urlparse(url).query)


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


# These pin the TRANSPORT, deliberately, and the reason is recorded rather than assumed. Three
# assertions already covered `loki_query_url` output and `plan()` argv, and every one of them
# sits UPSTREAM of the defect they would have had to catch: `run_query` built its own URL and
# passed no window, so the formatted path inherited Loki's one-hour server-side default while
# `--dry-run`/`--json` honoured `--since`. Without the explicit window, `--since 3d` returns a
# 60-minute slice — and an empty slice prints "no logs", which reads as health. A fourth
# builder-level assertion would have missed it exactly as the first three did. So: capture the
# url `fetch` is actually called with.


class _Seen(list):
    """The fetched urls, with the extra keyword arguments of each call beside them."""

    def __init__(self):
        super().__init__()
        self.kwargs = []


def _capture_fetch(monkeypatch, body='{"data":{"result":[]}}'):
    """Patch out the network and return the list that collects each fetched url."""
    seen = _Seen()

    def fake_fetch(url, resolve=None, **kwargs):
        seen.append(url)
        seen.kwargs.append(kwargs)
        return body

    monkeypatch.setattr(core, "fetch", fake_fetch)
    monkeypatch.setattr(core, "domain", lambda: "example.test")
    monkeypatch.setattr(core, "metallb_vip", lambda: "10.0.0.240")
    return seen


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


def test_run_query_sends_the_until_window_to_loki(monkeypatch):
    # --until moves the END back from now; the span is --since minus --until, and the end sits
    # --until before the clock, not at it.
    seen = _capture_fetch(monkeypatch)
    ns = cli_parser._build_parser().parse_args(
        ["loki-query", "up", "--since", "8d", "--until", "3d"]
    )
    assert metrics.run_query(ns) == 0
    params = _query_params(seen[0])
    start, end = int(params["start"][0]), int(params["end"][0])
    assert abs((end - start) / 1e9 - 5 * 86400) < 2
    assert abs(core.since_window_ns("3d")[0] - end) < 2e9


def test_since_not_further_back_than_until_is_a_usage_error():
    with pytest.raises(core.UsageError):
        core.since_window_ns("1d", "2d")


def test_run_query_sends_timeout_only_when_asked(monkeypatch):
    # The formatted path must hand --timeout down to fetch; unset leaves fetch its default.
    seen = _capture_fetch(monkeypatch)
    p = cli_parser._build_parser()
    metrics.run_query(p.parse_args(["loki-query", "up", "--timeout", "120"]))
    metrics.run_query(p.parse_args(["loki-query", "up"]))
    assert seen.kwargs == [{"timeout": 120}, {}]


def test_run_query_sends_the_step_to_loki(monkeypatch):
    # The formatted path builds its own URL, apart from plan(), so a --step that reached only
    # plan() would leave the default view at Loki's range/250 step with no warning.
    seen = _capture_fetch(monkeypatch)
    ns = cli_parser._build_parser().parse_args(
        [
            "loki-query",
            'count_over_time({job="k8s"}[5m])',
            "--since",
            "7d",
            "--step",
            "5m",
        ]
    )
    assert metrics.run_query(ns) == 0
    assert _query_params(seen[0])["step"] == ["5m"]


def test_run_query_without_since_sends_no_window():
    assert core.since_window_ns(None) == (None, None)
    assert core.since_window_ns("") == (None, None)


def test_since_window_ns_span_matches_the_requested_duration():
    start, end = core.since_window_ns("2d")
    assert abs((end - start) / 1e9 - 2 * 86400) < 2


def test_run_query_omits_direction_so_limit_keeps_the_newest_lines(monkeypatch):
    # Loki's default `backward` is what makes --limit return the newest N, which format_loki
    # then sorts. `run_alerts` asks for that same end by name, since `alert_episodes`' own sort
    # already covers oldest-first episode reconstruction.
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


# ── `probe.py alerts`: firing history from ALERTS ─────────────────────────────────────────

NOW = 1_780_000_000.0


def _series(name, service, times, severity="critical", **extra):
    labels = {"alertname": name, "service": service, "severity": severity, **extra}
    return {"metric": labels, "values": [[t, "1"] for t in times]}


def test_the_step_widens_past_a_week_to_stay_under_the_point_cap():
    assert alerts.step_seconds(1) == alerts.MIN_STEP_S
    assert alerts.step_seconds(30) * alerts.MAX_POINTS >= 30 * 86400


def test_the_range_url_reads_alerts_over_the_whole_window():
    params = _query_params(alerts.range_url("https://p", 2, NOW))
    assert params["query"] == [alerts.QUERY]
    assert float(params["end"][0]) - float(params["start"][0]) == 2 * 86400
    assert params["step"] == ["60s"]


def test_samples_one_step_apart_are_one_episode():
    times = [NOW, NOW + 60, NOW + 120]
    (episode,) = alerts.alert_episodes([_series("A", "x", times)], 60)
    assert (episode["first_s"], episode["last_s"], episode["samples"]) == (
        NOW,
        NOW + 120,
        3,
    )


def test_a_gap_longer_than_two_steps_splits_the_episode():
    times = [NOW, NOW + 60, NOW + 3600]
    episodes = alerts.alert_episodes([_series("A", "x", times)], 60)
    assert [e["first_s"] for e in episodes] == [NOW + 3600, NOW]


def test_a_pinned_gap_merges_what_the_step_gap_would_split():
    times = [NOW, NOW + 3600]
    assert len(alerts.alert_episodes([_series("A", "x", times)], 60, gap_s=3600)) == 1


def test_series_differing_only_in_unshown_labels_are_one_episode():
    """A per-cgroup alert fires as several series; printed apart they read as duplicates."""
    a = _series("ClaudeCgroupMemoryEvent", "daniel-server", [NOW], cgroup="a")
    b = _series("ClaudeCgroupMemoryEvent", "daniel-server", [NOW, NOW + 60], cgroup="b")
    (episode,) = alerts.alert_episodes([a, b], 60)
    assert episode["samples"] == 2


def test_check_matches_the_service_as_well_as_the_alert_name():
    labels = {"alertname": "IngressProbeFailed", "service": "jellyfin"}
    assert alerts.keep_series(labels, "jelly", None)
    assert not alerts.keep_series(labels, "sonarr", None)


def test_pi_keeps_a_pi_job_or_a_pi_service_and_drops_the_rest():
    jobs = {"node-pi", "blackbox-pi-ports"}
    assert alerts.keep_series(
        {"job": "blackbox-pi-ports", "service": "wg-easy"}, None, jobs
    )
    assert alerts.keep_series({"service": "daniel-pi"}, None, jobs)
    assert not alerts.keep_series({"job": "node", "service": "daniel-box"}, None, jobs)


def test_the_pi_jobs_include_the_published_port_probes():
    """PiPortDead's job targets the Pi through `hostvars`, not `k8s_pi_client_ip`."""
    jobs = alerts.pi_alert_jobs(pi_plane.PROMETHEUS_TEMPLATE_PATH.read_text())
    assert {"node-pi", "alloy-pi", "blackbox-pi-ports"} <= jobs
    assert "node" not in jobs


def test_no_episode_renders_an_all_clear_naming_the_window():
    assert alerts.format_alert_episodes([], 3, 60) == "no firing alerts in the last 3d"


def test_a_shadow_episode_is_marked():
    series = _series("EvalSweepFailed", "daniel-box", [NOW], "warning", shadow="true")
    text = alerts.format_alert_episodes(alerts.alert_episodes([series], 60), 1, 60)
    assert text.splitlines()[-1].endswith("daniel-box/EvalSweepFailed  warning shadow")


def _fetch_returns(monkeypatch, reply):
    def fetch_parsed(url, resolve=None):
        if isinstance(reply, BaseException):
            raise reply
        return reply

    monkeypatch.setattr(core, "fetch_parsed", fetch_parsed)


def test_alerts_prints_the_episodes_it_read(monkeypatch, capsys):
    body = {
        "status": "success",
        "data": {"result": [_series("PiHostDown", "daniel-pi", [NOW])]},
    }
    _fetch_returns(monkeypatch, body)
    ns = cli_parser._build_parser().parse_args(["alerts", "--days", "1"])
    assert alerts.run_alerts(ns, now=NOW) == 0
    assert "daniel-pi/PiHostDown" in capsys.readouterr().out


def test_alerts_that_cannot_read_prometheus_is_unreadable_not_an_all_clear(
    monkeypatch, capsys
):
    _fetch_returns(monkeypatch, core.Unreadable("curl: (7) Failed to connect"))
    ns = cli_parser._build_parser().parse_args(["alerts"])
    assert alerts.run_alerts(ns, now=NOW) == PROBE_UNREADABLE
    assert "no firing alerts" not in capsys.readouterr().out
