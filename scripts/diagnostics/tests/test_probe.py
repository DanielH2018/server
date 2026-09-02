"""probe.py's command surface: argv parsing, routing, URL building, output formatting.

`plan()` decides which request each subcommand makes without making it, so these are the tests
that catch a subcommand pointed at the wrong host or built with the wrong query — the class of
bug that returns a confident answer about the wrong thing.
"""

import re

import pytest

import probe_arr
import probe_health
import probe_metrics
import probe_monitors
import probe
import probe_core as core


def test_prom_query_url_encodes_promql():
    url = core.prom_query_url("https://prom.example", "up == 0")
    assert url == "https://prom.example/api/v1/query?query=up+%3D%3D+0"


def test_prom_targets_url():
    assert (
        core.prom_targets_url("https://prom.example")
        == "https://prom.example/api/v1/targets"
    )


def test_loki_labels_url():
    assert (
        core.loki_labels_url("https://loki.example")
        == "https://loki.example/loki/api/v1/labels"
    )


def test_loki_query_url_encodes_logql_and_limit():
    url = core.loki_query_url("https://loki.example", '{job="x"}', 50)
    assert (
        url
        == "https://loki.example/loki/api/v1/query_range?query=%7Bjob%3D%22x%22%7D&limit=50"
    )


def test_scrutiny_url():
    assert (
        core.scrutiny_url("https://scrutiny.example")
        == "https://scrutiny.example/api/summary"
    )


def test_pi_url():
    assert core.pi_url("fs") == "http://daniel-pi.lan:61208/api/4/fs"


def test_pi_ip_reads_real_inventory():
    # hosts.ini is plaintext, not a secret — same class of dead-path bug as
    # test_verify_automations_path_exists below: a wrong path or regex would only ever
    # be caught by opening the real file.
    ip = core.pi_ip()
    assert re.match(r"^\d+\.\d+\.\d+\.\d+$", ip), ip


def test_pi_resolve_pins_the_lan_ip(monkeypatch):
    monkeypatch.setattr(core, "pi_ip", lambda: "10.0.0.139")
    assert core.pi_resolve() == "daniel-pi.lan:61208:10.0.0.139"


def test_curl_argv():
    assert core.curl_argv("http://x") == [
        "curl",
        "-sS",
        "--max-time",
        "10",
        "http://x",
    ]


def test_inspect_ip_argv_targets_the_container():
    argv = probe_health.inspect_ip_argv("loki")
    assert argv[:3] == ["docker", "inspect", "-f"]
    assert argv[-1] == "loki"
    assert ".IPAddress" in argv[3]


def test_parse_ip_takes_first_nonempty_token():
    assert probe_health.parse_ip("172.19.0.12 172.18.0.5 \n") == "172.19.0.12"


def test_parse_ip_returns_none_when_no_ip():
    assert probe_health.parse_ip("   \n") is None


def test_k8s_service_ip_argv_targets_the_service():
    argv = probe_health.k8s_service_ip_argv("sonarr", "homelab")
    assert argv[:2] == ["k3s", "kubectl"]
    assert argv[-1] == "jsonpath={.spec.clusterIP}"
    assert "sonarr" in argv
    assert "homelab" in argv


def test_plan_metric_uses_cluster_prometheus_route(fake_resolve, fake_k8s_endpoint):
    # The Docker prometheus (resolve_ip target) retired 2026-08-14 with the drain.
    stages = probe.plan(["metric", "up == 0"], fake_resolve, fake_k8s_endpoint)
    assert stages == [
        core.curl_argv(
            "https://prometheus.example/api/v1/query?query=up+%3D%3D+0",
            resolve="prometheus.example:443:10.0.0.240",
        )
    ]


def test_plan_targets_uses_cluster_prometheus_route(fake_resolve, fake_k8s_endpoint):
    stages = probe.plan(["targets"], fake_resolve, fake_k8s_endpoint)
    assert stages == [
        core.curl_argv(
            "https://prometheus.example/api/v1/targets",
            resolve="prometheus.example:443:10.0.0.240",
        )
    ]


def test_plan_loki_labels_uses_cluster_endpoint_with_vip_pin(
    fake_resolve, fake_k8s_endpoint
):
    stages = probe.plan(["loki-labels"], fake_resolve, fake_k8s_endpoint)
    assert stages == [
        core.curl_argv(
            "https://loki-homelab.example/loki/api/v1/labels",
            resolve="loki-homelab.example:443:10.0.0.240",
        )
    ]


def test_plan_loki_query_with_limit(fake_resolve, fake_k8s_endpoint):
    stages = probe.plan(
        ["loki-query", '{job="x"}', "--limit", "50"], fake_resolve, fake_k8s_endpoint
    )
    assert stages == [
        core.curl_argv(
            core.loki_query_url("https://loki-homelab.example", '{job="x"}', 50),
            resolve="loki-homelab.example:443:10.0.0.240",
        )
    ]


def test_plan_scrutiny_uses_cluster_endpoint_with_vip_pin(
    fake_resolve, fake_k8s_endpoint
):
    stages = probe.plan(["scrutiny"], fake_resolve, fake_k8s_endpoint)
    assert stages == [
        core.curl_argv(
            "https://scrutiny.example/api/summary",
            resolve="scrutiny.example:443:10.0.0.240",
        )
    ]


def test_plan_pi_does_not_resolve_docker():
    # Pi glances is reached by hostname, so the container resolver must NOT be consulted.
    def boom(_):
        raise AssertionError("pi must not resolve a container IP")

    stages = probe.plan(
        ["pi", "fs"], boom, pi_resolve=lambda: "daniel-pi.lan:61208:10.0.0.139"
    )
    assert stages == [
        core.curl_argv(
            "http://daniel-pi.lan:61208/api/4/fs",
            resolve="daniel-pi.lan:61208:10.0.0.139",
        )
    ]


def test_plan_pi_resolves_to_a_reachable_pin_not_dns():
    # Regression guard: this host's resolver has no answer for daniel-pi.lan (a
    # Pi-hole-only LAN name), so plan() must always carry a --resolve pin here — a bare
    # curl_argv() with no pin is the pre-fix shape that died `Could not resolve host`.
    stages = probe.plan(
        ["pi", "fs"], None, pi_resolve=lambda: "daniel-pi.lan:61208:10.0.0.139"
    )
    assert "--resolve" in stages[0]
    assert "daniel-pi.lan:61208:10.0.0.139" in stages[0]


def test_plan_cert_defaults_port_and_sni_to_host(fake_resolve):
    stages = probe.plan(["cert", "homepage.daniel-hunter.com"], fake_resolve)
    assert stages == probe.cert_stages(
        "homepage.daniel-hunter.com", 443, "homepage.daniel-hunter.com"
    )


def test_plan_cert_explicit_port_and_sni(fake_resolve):
    stages = probe.plan(
        ["cert", "10.0.0.161:443", "--sni", "homepage.daniel-hunter.com"], fake_resolve
    )
    assert stages == probe.cert_stages("10.0.0.161", 443, "homepage.daniel-hunter.com")


def test_cert_stages_is_a_two_stage_pipeline():
    s1, s2 = probe.cert_stages("h", 443, "h")
    assert s1[:2] == ["openssl", "s_client"]
    assert "h:443" in s1
    assert s2[:2] == ["openssl", "x509"]


# These replace the `probe.py metric … | python3 -c "…reshape JSON…"` one-liners
# that kept prompting: the reshape now lives in the allow-listed script instead.


def test_format_metric_vector_prints_labels_and_value_per_series():
    data = {
        "data": {
            "resultType": "vector",
            "result": [
                {
                    "metric": {"__name__": "monitor_status", "monitor_name": "Pi"},
                    "value": [1720000000, "1"],
                },
                {
                    "metric": {"__name__": "monitor_status", "monitor_name": "Loki"},
                    "value": [1720000000, "0"],
                },
            ],
        }
    }
    out = probe_metrics.format_metric(data)
    assert "monitor_name=Pi = 1" in out
    assert "monitor_name=Loki = 0" in out
    # __name__ is dropped — it's the metric name, redundant here.
    assert "__name__" not in out


def test_format_metric_single_unlabeled_series_prints_bare_value():
    # e.g. predict_linear(...)/1e9 strips all labels -> just the scalar.
    data = {
        "data": {
            "resultType": "vector",
            "result": [{"metric": {}, "value": [0, "6.47"]}],
        }
    }
    assert probe_metrics.format_metric(data) == "6.47"


def test_format_metric_matrix_uses_latest_point():
    data = {
        "data": {
            "resultType": "matrix",
            "result": [
                {
                    "metric": {"mountpoint": "/"},
                    "values": [[1, "10"], [2, "12"], [3, "15"]],
                }
            ],
        }
    }
    assert probe_metrics.format_metric(data) == "mountpoint=/ = 15"


def test_format_metric_scalar_result_prints_value():
    data = {"data": {"resultType": "scalar", "result": [1720000000, "42"]}}
    assert probe_metrics.format_metric(data) == "42"


def test_format_metric_empty_is_no_data():
    assert (
        probe_metrics.format_metric({"data": {"resultType": "vector", "result": []}})
        == "no data"
    )


def _monitor_series(name, status):
    return {"metric": {"monitor_name": name}, "value": [1720000000, status]}


def test_format_monitor_status_all_up():
    data = {
        "data": {
            "result": [_monitor_series("sonarr", "1"), _monitor_series("radarr", "1")]
        }
    }
    text, code = probe_monitors.format_monitor_status(data)
    assert text == "2/2 monitors up"
    assert code == 0


def test_format_monitor_status_lists_down_monitors_and_fails():
    data = {
        "data": {
            "result": [
                _monitor_series("sonarr", "1"),
                _monitor_series("terraria (game port)", "0"),
            ]
        }
    }
    text, code = probe_monitors.format_monitor_status(data)
    assert text == "1/2 monitors up\n  terraria (game port): DOWN"
    assert code == 1


def test_format_monitor_status_labels_pending_and_maintenance_as_not_up():
    data = {
        "data": {
            "result": [_monitor_series("a", "2"), _monitor_series("b", "3")],
        }
    }
    text, code = probe_monitors.format_monitor_status(data)
    assert "a: PENDING" in text
    assert "b: MAINTENANCE" in text
    assert code == 1


def test_format_monitor_status_empty_result_fails():
    text, code = probe_monitors.format_monitor_status({"data": {"result": []}})
    assert code == 1
    assert "no monitor_status series" in text


def test_monitors_subcommand_parses():
    p = probe._build_parser()
    ns = p.parse_args(["monitors"])
    assert ns.cmd == "monitors"


def test_format_loki_prints_lines_oldest_first_across_streams():
    data = {
        "data": {
            "resultType": "streams",
            "result": [
                {
                    "stream": {"container": "traefik"},
                    "values": [["30", "newest"], ["10", "oldest"]],
                },
                {"stream": {"container": "traefik"}, "values": [["20", "middle"]]},
            ],
        }
    }
    # Sorted by nanosecond timestamp so the newest line sits nearest the prompt.
    assert probe_metrics.format_loki(data) == "oldest\nmiddle\nnewest"


def test_format_loki_empty_is_no_logs():
    assert probe_metrics.format_loki({"data": {"result": []}}) == "no logs"


def test_metric_defaults_to_formatted_with_json_escape_hatch():
    p = probe._build_parser()
    assert p.parse_args(["metric", "up"]).json is False
    assert p.parse_args(["metric", "up", "--json"]).json is True


def test_loki_query_defaults_to_formatted_with_json_escape_hatch():
    p = probe._build_parser()
    assert p.parse_args(["loki-query", '{job="x"}']).json is False
    assert p.parse_args(["loki-query", '{job="x"}', "--json"]).json is True


# Replaces `docker exec <arr> curl -H "X-Api-Key: <hex>" …/api/… | python3`,
# which both prompted AND leaked the key into argv / shell history / the log.


def test_arr_url_sonarr_defaults_to_api_v3_and_port_8989():
    assert (
        probe_arr.arr_url("10.0.0.5", "sonarr", "health")
        == "http://10.0.0.5:8989/api/v3/health"
    )


def test_arr_url_radarr_port_7878_api_v3():
    assert (
        probe_arr.arr_url("10.0.0.6", "radarr", "queue")
        == "http://10.0.0.6:7878/api/v3/queue"
    )


def test_arr_url_prowlarr_port_9696_api_v1():
    assert (
        probe_arr.arr_url("10.0.0.7", "prowlarr", "indexerstatus")
        == "http://10.0.0.7:9696/api/v1/indexerstatus"
    )


def test_arr_url_normalizes_leading_slash_api_and_version_prefix():
    # bare, /-prefixed, api/-prefixed, and version-prefixed all mean the same endpoint.
    for path in ("health", "/health", "api/v3/health", "v3/health", "/api/v3/health"):
        assert probe_arr.arr_url("h", "sonarr", path) == "http://h:8989/api/v3/health"


def test_arr_url_keeps_multi_segment_path():
    assert (
        probe_arr.arr_url("h", "prowlarr", "indexer/testall")
        == "http://h:9696/api/v1/indexer/testall"
    )


def test_arr_curl_config_uses_x_api_key_header():
    assert 'header = "X-Api-Key: SECRET_KEY"' in probe_arr.arr_curl_config("SECRET_KEY")


def test_arr_curl_config_is_not_bearer():
    cfg = probe_arr.arr_curl_config("SECRET_KEY")
    assert "Bearer" not in cfg and "Authorization" not in cfg


def test_arr_request_never_puts_key_in_argv():
    # Regression guard mirroring the ha token: the key travels via stdin --config, never argv.
    argv = core.ha_curl_argv(probe_arr.arr_url("h", "sonarr", "health"))
    assert "--config" in argv
    assert not any("Api-Key" in a or "SECRET" in a for a in argv)


def test_resolve_arr_ip_uses_kubectl_not_docker(monkeypatch):
    # Regression guard for the dead command: sonarr/radarr/prowlarr have run as k8s
    # Deployments since 2026-08-07 and have no Docker container to `docker inspect` an IP
    # from — resolve_arr_ip must reach the app's ClusterIP via kubectl instead.
    monkeypatch.setattr(core, "k8s_namespace", lambda: "homelab")

    class FakeResult:
        returncode = 0
        stdout = "10.43.114.186"
        stderr = ""

    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return FakeResult()

    monkeypatch.setattr(probe.subprocess, "run", fake_run)
    assert probe_arr.resolve_arr_ip("sonarr") == "10.43.114.186"
    assert calls == [probe_health.k8s_service_ip_argv("sonarr", "homelab")]
    assert "docker" not in calls[0]


def test_resolve_arr_ip_raises_on_kubectl_failure(monkeypatch):
    monkeypatch.setattr(core, "k8s_namespace", lambda: "homelab")

    class FakeResult:
        returncode = 1
        stdout = ""
        stderr = 'services "sonarr" not found'

    monkeypatch.setattr(probe.subprocess, "run", lambda argv, **kwargs: FakeResult())
    try:
        probe_arr.resolve_arr_ip("sonarr")
        raise AssertionError("expected SystemExit")
    except SystemExit as e:
        assert "sonarr" in str(e)


def test_resolve_arr_ip_raises_on_empty_cluster_ip(monkeypatch):
    monkeypatch.setattr(core, "k8s_namespace", lambda: "homelab")

    class FakeResult:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(probe.subprocess, "run", lambda argv, **kwargs: FakeResult())
    try:
        probe_arr.resolve_arr_ip("sonarr")
        raise AssertionError("expected SystemExit")
    except SystemExit as e:
        assert "ClusterIP" in str(e)


def test_arr_subcommand_parses_app_path_and_json_flag():
    p = probe._build_parser()
    ns = p.parse_args(["arr", "sonarr", "health"])
    assert (
        ns.cmd == "arr"
        and ns.app == "sonarr"
        and ns.path == "health"
        and ns.json is False
    )
    assert p.parse_args(["arr", "prowlarr", "indexerstatus", "--json"]).json is True


def test_arr_subcommand_rejects_unknown_app():

    with pytest.raises(SystemExit):
        probe._build_parser().parse_args(["arr", "lidarr", "health"])


def test_no_cluster_route_carries_the_retired_k8s_suffix(
    fake_resolve, fake_k8s_endpoint
):
    """The `-k8s` suffix retired 2026-08-15 (870723e8), but probe.py kept building it for
    another five hours: every cluster subcommand 404'd against Traefik's no-Host-match while
    the fixtures below asserted the stale name, so CI ratified the break. Assert on the
    hostnames plan() actually asks for, so a reintroduced suffix fails here first."""
    asked = []

    def record(hostname):
        asked.append(hostname)
        return fake_k8s_endpoint(hostname)

    for argv in (
        ["metric", "up"],
        ["targets"],
        ["loki-labels"],
        ["loki-query", '{job="x"}'],
        ["scrutiny"],
    ):
        probe.plan(argv, fake_resolve, record)

    assert asked, "expected plan() to route these subcommands through k8s_endpoint"
    assert not [h for h in asked if h.endswith("-k8s")]
