"""What a triage or Renovate session can write must not open the next triage episode.

I6 of the guardrails plan (dotfiles#967): no routed alert reads a series or a log line a
session can write, so a session cannot start the run after its own.
"""

import re

import pytest
from _helpers import K8S_ROLES
from _k8s_render import render_role_template
from lib import yaml_fast
from test_claude_triage import pins

# A session can write syslog lines with any tag, so a routed alert must not count them.
PROMETHEUS_RULES = K8S_ROLES / "observability" / "files" / "rules"
LOKI_RULES = K8S_ROLES / "loki-homelab" / "files" / "rules"
# Prometheus series that count log lines or bytes: Alloy's `stage.metrics` counters and Loki's
# own ingest volume.
LOG_VOLUME_METRICS = (
    "loki_process_custom_",
    "loki_distributor_lines_received",
    "loki_distributor_bytes_received",
)
# The LogQL function applied to each range over a stream selector.
LOG_RANGE_FUNCTION = re.compile(r"(\w+)\s*\(\s*\{[^{}]*\}[^\[\]]*\[")


def alert_rules() -> list[tuple[str, str, str]]:
    """(alert, ruler, expr) for every alerting rule Prometheus or the Loki ruler evaluates."""
    found = []
    for ruler, directory in (("prometheus", PROMETHEUS_RULES), ("loki", LOKI_RULES)):
        for rule_file in sorted(directory.glob("*.yaml")):
            for group in yaml_fast.safe_load(rule_file.read_text())["groups"]:
                found += [
                    (r["alert"], ruler, r["expr"])
                    for r in group["rules"]
                    if "alert" in r
                ]
    return found


def session_can_raise(ruler: str, expr: str) -> bool:
    """Whether log lines a session writes can push the rule towards firing.

    Any count of lines can, whatever it filters on: a session adds lines with `logger`. Only
    `absent_over_time` is exempt, because an added line can only keep it from firing.
    """
    if ruler == "prometheus":
        return any(metric in expr for metric in LOG_VOLUME_METRICS)
    return any(
        function != "absent_over_time" for function in LOG_RANGE_FUNCTION.findall(expr)
    )


def routed_alerts() -> list[str]:
    return sorted({key.split(":", 1)[0] for key in pins()["poll_routes"]})


@pytest.mark.parametrize("alert", routed_alerts())
def test_no_routed_alert_counts_log_lines_a_session_can_write(alert) -> None:
    rules = [(ruler, expr) for name, ruler, expr in alert_rules() if name == alert]
    assert rules, f"{alert} is routed but no rule defines it"
    for ruler, expr in rules:
        assert not session_can_raise(ruler, expr), (
            f"{alert} is routed, and its {ruler} rule counts log lines: {expr}"
        )


def test_the_log_count_check_flags_the_rules_it_exists_for() -> None:
    """The red half: routing any of these would fail the test above."""
    flagged = {
        name for name, ruler, expr in alert_rules() if session_can_raise(ruler, expr)
    }
    assert {"HostLogErrors", "HostOutOfMemoryKill", "WorkloadLogErrors"} <= flagged
    assert "LokiHostLogsSilent" not in flagged
    assert session_can_raise(
        "loki", 'sum(count_over_time({job="syslog", machine="daniel-box"}[5m])) > 500'
    )
    assert session_can_raise(
        "prometheus", "increase(loki_process_custom_crowdsec_edge_probe_total[1h]) > 0"
    )
    assert session_can_raise(
        "prometheus", "rate(loki_distributor_lines_received_total[5m]) > 1000"
    )


# A session also reaches the loopback OTLP collector, which passes on any metric it is sent.
METRIC_NAME = re.compile(r"[a-zA-Z_:][a-zA-Z0-9_:]*")


@pytest.fixture(scope="module")
def collector_job() -> dict:
    [text] = [
        doc["data"]["prometheus.yml"]
        for doc in yaml_fast.safe_load_all(
            render_role_template("observability", "prometheus.yaml.j2")
        )
        if doc and "prometheus.yml" in (doc.get("data") or {})
    ]
    [job] = [
        job
        for job in yaml_fast.safe_load(text)["scrape_configs"]
        if job["job_name"] == "otel-collector"
    ]
    return job


def forgeable_names(expr: str, job: dict) -> list[str]:
    """The metric names in `expr` that a session could send through the collector `job` scrapes."""
    keeps = [
        rule["regex"]
        for rule in job.get("metric_relabel_configs") or []
        if rule.get("action") == "keep" and rule.get("source_labels") == ["__name__"]
    ]
    if not keeps:
        return ["any name: the job keeps every metric it scrapes"]
    return sorted(
        {
            name
            for name in METRIC_NAME.findall(expr)
            if all(re.fullmatch(regex, name) for regex in keeps)
        }
    )


@pytest.mark.parametrize("alert", routed_alerts())
def test_no_routed_alert_reads_a_series_a_session_can_send_is_clean(
    alert, collector_job
) -> None:
    # fact: hosts/components/claude-code/CLAUDE.md#Autonomous-role contract (`claude-triage@`, a session started by a monitor episode)
    # fact: k8s/roles/observability/CLAUDE.md#observability — the cluster metrics plane plus the Claude Code telemetry stack (k3s, daniel-box)
    for name, ruler, expr in alert_rules():
        if name == alert and ruler == "prometheus":
            assert forgeable_names(expr, collector_job) == [], (alert, expr)


def test_a_rule_reading_a_name_the_collector_job_keeps_is_flagged(
    collector_job,
) -> None:
    [zero_available] = [
        expr for name, _, expr in alert_rules() if name == "K8sDeploymentZeroAvailable"
    ]
    keeps_all = collector_job | {
        "metric_relabel_configs": [
            {"source_labels": ["__name__"], "regex": ".*", "action": "keep"}
        ]
    }
    assert {
        "kube_deployment_status_replicas_available",
        "kube_deployment_spec_replicas",
    } <= set(forgeable_names(zero_available, keeps_all))
    assert forgeable_names(
        "increase(claude_code_cost_usage_USD_total[1h]) > 10", collector_job
    ) == ["claude_code_cost_usage_USD_total"]
