"""`plan()` and `cert_stages()`: which request each streaming subcommand makes.

`plan()` decides which host a subcommand asks and which URL it builds, without making the
request, so these are the tests that catch a subcommand pointed at the wrong host or built
with the wrong query — the class of bug that returns a confident answer about the wrong thing.

Split out of test_probe.py when `plan`/`stream_pipeline` moved into `probe_lib/curl_pipeline.py`
and `cert_stages` into `probe_lib/cli_parser.py`.

Run: uv run pytest scripts/diagnostics/tests/test_probe_curl_pipeline.py
"""

from diagnostics.probe_lib import cli_parser, core, curl_pipeline


def test_plan_metric_uses_cluster_prometheus_route(fake_resolve, fake_k8s_endpoint):
    # The Docker prometheus (resolve_ip target) retired 2026-08-14 with the drain.
    stages = curl_pipeline.plan(["metric", "up == 0"], fake_resolve, fake_k8s_endpoint)
    assert stages == [
        core.curl_argv(
            "https://prometheus.example/api/v1/query?query=up+%3D%3D+0",
            resolve="prometheus.example:443:10.0.0.240",
        )
    ]


def test_plan_targets_uses_cluster_prometheus_route(fake_resolve, fake_k8s_endpoint):
    stages = curl_pipeline.plan(["targets"], fake_resolve, fake_k8s_endpoint)
    assert stages == [
        core.curl_argv(
            "https://prometheus.example/api/v1/targets",
            resolve="prometheus.example:443:10.0.0.240",
        )
    ]


def test_plan_loki_labels_uses_cluster_endpoint_with_vip_pin(
    fake_resolve, fake_k8s_endpoint
):
    stages = curl_pipeline.plan(["loki-labels"], fake_resolve, fake_k8s_endpoint)
    assert stages == [
        core.curl_argv(
            "https://loki-homelab.example/loki/api/v1/labels",
            resolve="loki-homelab.example:443:10.0.0.240",
        )
    ]


def test_plan_loki_query_with_limit(fake_resolve, fake_k8s_endpoint):
    stages = curl_pipeline.plan(
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
    stages = curl_pipeline.plan(["scrutiny"], fake_resolve, fake_k8s_endpoint)
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

    stages = curl_pipeline.plan(
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
    stages = curl_pipeline.plan(
        ["pi", "fs"], None, pi_resolve=lambda: "daniel-pi.lan:61208:10.0.0.139"
    )
    assert "--resolve" in stages[0]
    assert "daniel-pi.lan:61208:10.0.0.139" in stages[0]


def test_plan_cert_defaults_port_and_sni_to_host(fake_resolve):
    stages = curl_pipeline.plan(["cert", "homepage.daniel-hunter.com"], fake_resolve)
    assert stages == cli_parser.cert_stages(
        "homepage.daniel-hunter.com", 443, "homepage.daniel-hunter.com"
    )


def test_plan_cert_explicit_port_and_sni(fake_resolve):
    stages = curl_pipeline.plan(
        ["cert", "10.0.0.161:443", "--sni", "homepage.daniel-hunter.com"], fake_resolve
    )
    assert stages == cli_parser.cert_stages(
        "10.0.0.161", 443, "homepage.daniel-hunter.com"
    )


def test_cert_stages_is_a_two_stage_pipeline():
    s1, s2 = cli_parser.cert_stages("h", 443, "h")
    assert s1[:2] == ["openssl", "s_client"]
    assert "h:443" in s1
    assert s2[:2] == ["openssl", "x509"]


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
        curl_pipeline.plan(argv, fake_resolve, record)

    assert asked, "expected plan() to route these subcommands through k8s_endpoint"
    assert not [h for h in asked if h.endswith("-k8s")]
