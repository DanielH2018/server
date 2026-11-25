"""`plan()` and `cert_stages()`: which request each streaming subcommand makes.

`plan()` decides which host a subcommand asks and which URL it builds, without making the
request, so these are the tests that catch a subcommand pointed at the wrong host or built
with the wrong query — the class of bug that returns a confident answer about the wrong thing.

Split out of test_probe.py when `plan`/`stream_pipeline` moved into `probe_lib/curl_pipeline.py`
and `cert_stages` into `probe_lib/cli_parser.py`.

Run: uv run pytest scripts/diagnostics/tests/test_probe_curl_pipeline.py
"""

from typing import Any, Callable, cast

import pytest

from diagnostics.probe_lib import cli_parser, core, curl_pipeline
from lib.exit_codes import USAGE_ERROR


def test_plan_metric_uses_cluster_prometheus_route(fake_k8s_endpoint):
    stages = curl_pipeline.plan(["metric", "up == 0"], fake_k8s_endpoint)
    assert stages == [
        core.curl_argv(
            "https://prometheus.example/api/v1/query?query=up+%3D%3D+0",
            resolve="prometheus.example:443:10.0.0.240",
        )
    ]


def test_plan_targets_uses_cluster_prometheus_route(fake_k8s_endpoint):
    stages = curl_pipeline.plan(["targets"], fake_k8s_endpoint)
    assert stages == [
        core.curl_argv(
            "https://prometheus.example/api/v1/targets",
            resolve="prometheus.example:443:10.0.0.240",
        )
    ]


def test_plan_loki_labels_uses_cluster_endpoint_with_vip_pin(fake_k8s_endpoint):
    stages = curl_pipeline.plan(["loki-labels"], fake_k8s_endpoint)
    assert stages == [
        core.curl_argv(
            "https://loki-homelab.example/loki/api/v1/labels",
            resolve="loki-homelab.example:443:10.0.0.240",
        )
    ]


def test_plan_loki_query_with_limit(fake_k8s_endpoint):
    stages = curl_pipeline.plan(
        ["loki-query", '{job="x"}', "--limit", "50"], fake_k8s_endpoint
    )
    assert stages == [
        core.curl_argv(
            core.loki_query_url("https://loki-homelab.example", '{job="x"}', 50),
            resolve="loki-homelab.example:443:10.0.0.240",
        )
    ]


def test_plan_loki_query_step_reaches_the_url(fake_k8s_endpoint):
    stages = curl_pipeline.plan(
        ["loki-query", 'count_over_time({job="x"}[5m])', "--step", "5m"],
        fake_k8s_endpoint,
    )
    assert "&step=5m" in stages[0][-1]


def test_plan_loki_query_until_and_timeout_reach_the_argv(fake_k8s_endpoint):
    stages = curl_pipeline.plan(
        ["loki-query", "up", "--since", "2d", "--until", "1d", "--timeout", "90"],
        fake_k8s_endpoint,
    )
    assert stages[0][stages[0].index("--max-time") + 1] == "90"
    assert "&end=" in stages[0][-1] and "&start=" in stages[0][-1]


def test_plan_loki_query_without_step_leaves_it_to_loki(fake_k8s_endpoint):
    stages = curl_pipeline.plan(
        ["loki-query", 'count_over_time({job="x"}[5m])'], fake_k8s_endpoint
    )
    assert "step=" not in stages[0][-1]


def test_plan_scrutiny_uses_cluster_endpoint_with_vip_pin(fake_k8s_endpoint):
    stages = curl_pipeline.plan(["scrutiny"], fake_k8s_endpoint)
    assert stages == [
        core.curl_argv(
            "https://scrutiny.example/api/summary",
            resolve="scrutiny.example:443:10.0.0.240",
        )
    ]


def test_plan_cert_defaults_port_and_sni_to_host():
    stages = curl_pipeline.plan(["cert", "homepage.daniel-hunter.com"])
    assert stages == cli_parser.cert_stages(
        "homepage.daniel-hunter.com", 443, "homepage.daniel-hunter.com"
    )


def test_plan_cert_explicit_port_and_sni():
    stages = curl_pipeline.plan(
        ["cert", "10.0.0.161:443", "--sni", "homepage.daniel-hunter.com"]
    )
    assert stages == cli_parser.cert_stages(
        "10.0.0.161", 443, "homepage.daniel-hunter.com"
    )


def test_cert_stages_is_a_two_stage_pipeline():
    s1, s2 = cli_parser.cert_stages("h", 443, "h")
    assert s1[:2] == ["openssl", "s_client"]
    assert "h:443" in s1
    assert s2[:2] == ["openssl", "x509"]


def test_no_cluster_route_carries_the_retired_k8s_suffix(fake_k8s_endpoint):
    """Assert on the hostnames plan() actually asks for, so a reintroduced `-k8s` suffix fails
    here first: every cluster subcommand would 404 against Traefik's no-Host-match while
    fixtures asserted the stale name."""
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
        curl_pipeline.plan(argv, record)

    assert asked, "expected plan() to route these subcommands through k8s_endpoint"
    assert not [h for h in asked if h.endswith("-k8s")]


def _fake_cluster_ip():
    return "10.43.0.99"


def test_plan_loki_query_default_store_is_homelab(fake_k8s_endpoint):
    stages = curl_pipeline.plan(
        ["loki-query", '{job="x"}'], fake_k8s_endpoint, _fake_cluster_ip
    )
    assert stages[0][-1].startswith(
        "https://loki-homelab.example/loki/api/v1/query_range?"
    )
    assert "--resolve" in stages[0]


def test_plan_loki_query_observability_store_uses_cluster_ip_without_pin(
    fake_k8s_endpoint,
):
    stages = curl_pipeline.plan(
        ["loki-query", '{service_name="claude-code"}', "--loki", "observability"],
        fake_k8s_endpoint,
        _fake_cluster_ip,
    )
    assert stages == [
        core.curl_argv(
            core.loki_query_url(
                "http://10.43.0.99:3100", '{service_name="claude-code"}', 100
            )
        )
    ]


def test_plan_loki_labels_observability_store_uses_cluster_ip_without_pin(
    fake_k8s_endpoint,
):
    stages = curl_pipeline.plan(
        ["loki-labels", "--loki", "observability"],
        fake_k8s_endpoint,
        _fake_cluster_ip,
    )
    assert stages == [core.curl_argv("http://10.43.0.99:3100/loki/api/v1/labels")]


def test_plan_routes_bare_claude_code_selector_to_observability(
    fake_k8s_endpoint, capsys
):
    # The issue's own verify-by command, with no --loki: it must return lines, not a refusal.
    stages = curl_pipeline.plan(
        ["loki-query", '{service_name="claude-code"} | event_name="tool_decision"'],
        fake_k8s_endpoint,
        _fake_cluster_ip,
    )
    assert stages[0][-1].startswith("http://10.43.0.99:3100/loki/api/v1/query_range?")
    assert "observability" in capsys.readouterr().err


def test_plan_refuses_claude_code_selector_at_explicit_homelab_store(fake_k8s_endpoint):
    with pytest.raises(SystemExit, match="--loki observability"):
        curl_pipeline.plan(
            ["loki-query", '{service_name="claude-code"}', "--loki", "homelab"],
            fake_k8s_endpoint,
            _fake_cluster_ip,
        )


@pytest.mark.parametrize(
    "logql",
    [
        '{service_name="claude-code"}',
        '{service_name = "claude-code"}',
        '{service_name=~"claude-code"}',
    ],
)
def test_pick_loki_store_is_flagged(logql):
    assert core.pick_loki_store(logql, None)[0] == "observability"
    # A contradiction in the arguments: a retry can never clear it, so it exits 64.
    with pytest.raises(core.UsageError) as exc:
        core.pick_loki_store(logql, "homelab")
    assert exc.value.code == USAGE_ERROR


@pytest.mark.parametrize(
    "logql",
    [
        '{job="syslog"}',
        # loki-homelab carries its own `service_name` label (k8s workload names).
        '{service_name="crowdsec"}',
        '{service_name=~".+"}',
    ],
)
def test_pick_loki_store_is_clean(logql):
    assert core.pick_loki_store(logql, None) == ("homelab", None)
    assert core.pick_loki_store(logql, "homelab") == ("homelab", None)
    assert core.pick_loki_store(logql, "observability") == ("observability", None)


def test_observability_loki_ip_reads_the_role_default():
    ip = core.observability_loki_ip()
    assert ip.startswith("10.43."), ip


_plan = cast(Callable[..., Any], curl_pipeline.plan)


def test_plan_needs_nothing_beyond_args_for_a_lookup_free_subcommand():
    # `cert` consults neither endpoint lookup, so its args alone must be enough to plan it.
    host = "homepage.daniel-hunter.com"
    try:
        stages = _plan(["cert", host])
    except TypeError as exc:
        pytest.fail(f"plan() still requires an argument beyond args: {exc}")
    assert stages == cli_parser.cert_stages(host, 443, host)


def test_plan_routes_when_given_only_its_endpoint_lookups(fake_k8s_endpoint):
    try:
        stages = _plan(["scrutiny"], k8s_endpoint=fake_k8s_endpoint)
    except TypeError as exc:
        pytest.fail(f"plan() still requires an argument beyond its lookups: {exc}")
    assert stages == [
        core.curl_argv(
            "https://scrutiny.example/api/summary",
            resolve="scrutiny.example:443:10.0.0.240",
        )
    ]


_TORZNAB_JSON = (
    '{"values": [["1", "Torznab failed: http://prowlarr:9696/21/api?apikey='
    + "f" * 32
    + '&t=search"]]}'
)


def test_stream_pipeline_redacts_the_loki_query_body(capfd):
    rc = curl_pipeline.stream_pipeline(
        [["printf", "%s", _TORZNAB_JSON]],
        redact=curl_pipeline.output_redaction("loki-query"),
    )
    out = capfd.readouterr().out
    assert rc == 0
    assert "f" * 32 not in out
    assert "apikey=<redacted>&t=search" in out


def test_only_loki_query_output_is_redacted():
    assert curl_pipeline.output_redaction("loki-query") is core.redact_log_secrets
    assert curl_pipeline.output_redaction("metric") is None


def test_a_t2_session_reads_a_route_as_plain_http_through_its_proxy(monkeypatch):
    """A T2 session's squid admits the in-cluster reads only as plain HTTP, with no VIP pin."""
    host = f"prometheus.local.{core.domain()}"
    monkeypatch.delenv(core.VIA_PROXY_ENV, raising=False)
    assert core.k8s_endpoint("prometheus") == (
        f"https://{host}",
        f"{host}:443:{core.metallb_vip()}",
    )
    monkeypatch.setenv(core.VIA_PROXY_ENV, "1")
    assert core.k8s_endpoint("prometheus") == (f"http://{host}", None)
