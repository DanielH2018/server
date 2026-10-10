"""`plan()` and `cert_stages()`: which request each streaming subcommand makes.

`plan()` decides which host a subcommand asks and which URL it builds, without making the
request, so these are the tests that catch a subcommand pointed at the wrong host or built
with the wrong query — the class of bug that returns a confident answer about the wrong thing.

Split out of test_probe.py when `plan`/`stream_pipeline` moved into `probe_lib/curl_pipeline.py`
and `cert_stages` into `probe_lib/cli_parser.py`.

Run: uv run pytest scripts/diagnostics/tests/test_probe_curl_pipeline.py
"""

import ast
import os
from typing import Any, Callable, cast

import pytest

from diagnostics.probe_lib import cli_parser, core, curl_pipeline
from lib.proc_testing import run


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


# --- Two Loki stores ------------------------------------------------------------------
#
# `{service_name="claude-code"}` lives only in observability's Loki; `loki-homelab` returned a
# well-formed empty result for it that read as "the OTEL stream is gone". The default store
# stays `homelab` so every existing call is unchanged; `--loki observability` reaches the other
# by its pinned ClusterIP, and the one selector with a known home is refused at the wrong one.


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
    with pytest.raises(SystemExit):
        core.pick_loki_store(logql, "homelab")


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
    # The role template pins `clusterIP: {{ observability_loki_cluster_ip }}`; a parse that
    # silently returned nothing would build `http://:3100`.
    ip = core.observability_loki_ip()
    assert ip.startswith("10.43."), ip


# #4332: `plan()` reaches every service through `k8s_endpoint` or `observability_loki_ip`, so
# the Docker `resolve_ip` lookup it used to take is a parameter no branch calls. The calls below
# omit it on purpose, so they go through an untyped alias the type checker does not hold to the
# old signature.
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


def _resolve_ip_names(tree):
    """Every identifier in `tree` that names `resolve_ip`: a name, a parameter, an import."""
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id == "resolve_ip":
            found.append(node.lineno)
        elif isinstance(node, ast.arg) and node.arg == "resolve_ip":
            found.append(node.lineno)
        elif isinstance(node, ast.alias) and node.name == "resolve_ip":
            found.append(node.lineno)
    return found


def test_no_plan_caller_passes_a_resolve_lookup():
    """Census: neither probe.py nor any tracked diagnostics test hands plan() a resolver."""
    root = run(
        ["git", "rev-parse", "--show-toplevel"],
        check=True,
        cwd=os.path.dirname(os.path.abspath(__file__)),
    ).stdout.strip()
    tracked = run(
        [
            "git",
            "ls-files",
            "scripts/diagnostics/probe.py",
            "scripts/diagnostics/tests/*.py",
        ],
        check=True,
        cwd=root,
    ).stdout.split()
    # The census must at least read probe.py and this file, or it checks nothing.
    assert "scripts/diagnostics/probe.py" in tracked
    assert "scripts/diagnostics/tests/test_probe_curl_pipeline.py" in tracked

    offenders = []
    for rel in tracked:
        with open(os.path.join(root, rel), encoding="utf-8") as fh:
            tree = ast.parse(fh.read(), filename=rel)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = (
                func.attr
                if isinstance(func, ast.Attribute)
                else getattr(func, "id", None)
            )
            if name != "plan":
                continue
            for arg in [*node.args, *(kw.value for kw in node.keywords)]:
                if isinstance(arg, ast.Name) and "resolve" in arg.id:
                    offenders.append(f"{rel}:{node.lineno} passes {arg.id}")
        for node in ast.walk(tree):
            # A stand-in for plan() that still accepts the resolver keeps the old contract.
            if isinstance(node, ast.FunctionDef) and node.name.endswith("plan"):
                offenders += [
                    f"{rel}:{node.lineno} {node.name} accepts {a.arg}"
                    for a in node.args.args
                    if "resolve" in a.arg
                ]
        if rel == "scripts/diagnostics/probe.py":
            offenders += [
                f"{rel}:{n} names resolve_ip" for n in _resolve_ip_names(tree)
            ]
    assert offenders == []


def test_curl_pipeline_module_names_no_resolve_ip():
    """plan()'s signature and docstring both drop the lookup, so the module never names it."""
    with open(curl_pipeline.__file__, encoding="utf-8") as fh:
        source = fh.read()
    assert _resolve_ip_names(ast.parse(source)) == []
    assert "resolve_ip" not in (curl_pipeline.plan.__doc__ or "")
