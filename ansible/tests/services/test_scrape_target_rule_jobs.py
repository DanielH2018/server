"""M6 batch A3's shadow rules cover every scrape job and mirror monitor-bridge's numbers.

`files/rules/scrape-targets.yaml` is plain YAML, so `ScrapeTargetDown` cannot loop over the
jobs in `prometheus.yml`. It is one rule per owning role, each with a literal `service`, and a
last rule that catches any job the others do not name (M1 convention 1 and 3). The first half
of this module holds that table to the rendered scrape config: every job is in exactly one
named rule, so the "unmapped" fallback pages a job nobody mapped and is never where a job
lives. Kuma's two tiles exempt no job, and neither may this rule.

The second half holds the numbers `log-pipeline.yaml` and `scrape-targets.yaml` mirror to the
bridge's `templates/config/env.yaml.j2` (M1 convention 1: a threshold that mirrors an
inventory value is a literal in the rule, pinned by a pytest).

Run: uv run pytest ansible/tests/services/test_scrape_target_rule_jobs.py
"""

import re

import pytest
from _helpers import K8S_ROLES
from _k8s_render import render_role_template
from lib import yaml_fast

RULES = K8S_ROLES / "observability" / "files" / "rules"
SELECTOR = re.compile(r'^up\{job(=~|=|!~)"([^"]+)"\}$')
# The jobs this module must find in the rule table, so a regex that matched nothing fails.
MUST_FIND = {
    "alertmanager": "observability",
    "blackbox-ingress": "blackbox-exporter",
    "loki-homelab": "loki-homelab",
}


def scrape_rules() -> list[dict]:
    """The `ScrapeTargetDown` rules, in file order."""
    [group] = yaml_fast.safe_load((RULES / "scrape-targets.yaml").read_text())["groups"]
    return [r for r in group["rules"] if r["alert"] == "ScrapeTargetDown"]


def table(rules: list[dict]) -> tuple[dict[str, list[str]], set[str]]:
    """The named rules' jobs by service, and the jobs the catch-all excludes.

    Raises:
        AssertionError: a rule's expression is not `up{job...} == 0` in a form this reads.
    """
    named: dict[str, list[str]] = {}
    excluded: set[str] = set()
    for rule in rules:
        selector, comparison = str(rule["expr"]).rsplit(" == ", 1)
        assert comparison == "0", rule["expr"]
        found = SELECTOR.match(selector)
        assert found, f"cannot read the selector {selector!r}"
        op, value = found.groups()
        if op == "!~":
            excluded |= set(value.split("|"))
        else:
            named.setdefault(rule["labels"]["service"], []).extend(value.split("|"))
    return named, excluded


def rendered_jobs() -> set[str]:
    """Every `job_name` in the rendered `prometheus.yml`."""
    [text] = [
        doc["data"]["prometheus.yml"]
        for doc in yaml_fast.safe_load_all(
            render_role_template("observability", "prometheus.yaml.j2")
        )
        if doc and "prometheus.yml" in (doc.get("data") or {})
    ]
    scrape = yaml_fast.safe_load(text)
    return {job["job_name"] for job in scrape["scrape_configs"]}


def job_problems(
    named: dict[str, list[str]], excluded: set[str], jobs: set[str]
) -> list[str]:
    """Why the rule table misses or doubles a rendered job, or nothing."""
    listed = [job for names in named.values() for job in names]
    problems = [
        f"{job}: no ScrapeTargetDown rule names it"
        for job in sorted(jobs - set(listed))
    ]
    problems += [
        f"{job}: more than one ScrapeTargetDown rule names it"
        for job in sorted({j for j in listed if listed.count(j) > 1})
    ]
    if excluded != set(listed):
        problems.append(
            "the catch-all excludes "
            f"{sorted(excluded ^ set(listed))} differently from the named rules"
        )
    return problems


def test_every_rendered_scrape_job_is_in_exactly_one_named_rule():
    named, excluded = table(scrape_rules())
    assert job_problems(named, excluded, rendered_jobs()) == []


def test_the_table_finds_the_jobs_the_plan_names_with_their_service():
    named, _ = table(scrape_rules())
    found = {job: service for service, jobs in named.items() for job in jobs}
    assert {job: found.get(job) for job in MUST_FIND} == MUST_FIND


def test_a_job_no_rule_names_is_flagged():
    named, excluded = table(scrape_rules())
    assert job_problems(named, excluded, rendered_jobs() | {"new-exporter"}) == [
        "new-exporter: no ScrapeTargetDown rule names it"
    ]


def test_a_job_two_rules_name_is_flagged():
    named, excluded = table(scrape_rules())
    named["flux"] = [*named["flux"], "alertmanager"]
    assert job_problems(named, excluded, rendered_jobs()) == [
        "alertmanager: more than one ScrapeTargetDown rule names it"
    ]


def test_a_catch_all_that_forgot_a_named_job_is_flagged():
    named, excluded = table(scrape_rules())
    assert job_problems(named, excluded - {"flux"}, rendered_jobs()) == [
        "the catch-all excludes ['flux'] differently from the named rules"
    ]


def bridge_env(key: str) -> str:
    """The value of `key` in the bridge's env template, unquoted."""
    [value] = re.findall(
        rf'^{key}: "?([^"\n]+)"?$',
        render_role_template("monitor-bridge", "config/env.yaml.j2"),
        re.M,
    )
    return value


def rule_expr(stem: str, alert: str, service: str | None = None) -> str:
    """The expression of the one rule named `alert` (with `service`) in `stem`.yaml."""
    [group] = yaml_fast.safe_load((RULES / f"{stem}.yaml").read_text())["groups"]
    [expr] = [
        r["expr"]
        for r in group["rules"]
        if r["alert"] == alert and service in (None, r["labels"]["service"])
    ]
    return str(expr)


def test_the_floors_equal_the_bridges():
    expr = rule_expr("scrape-targets", "ScrapeTargetsMissing")
    assert f"< {bridge_env('CLUSTER_TARGETS_MIN')}" in expr
    assert f"< {bridge_env('TARGETS_MIN')}" in expr


@pytest.mark.parametrize(
    ("alert", "service", "key", "literal"),
    [
        ("LogShipperDroppingEntries", None, "SHIPPER_DROPPED_MAX", "> {}"),
        ("LokiDiscardingEntries", "loki-homelab", "SHIPPER_DROPPED_MAX", "> {}"),
        ("OtelCollectorExportFailing", None, "OTELCOL_SEND_FAILED_MAX", "> {}"),
        ("TempoBlocklistPollFailing", None, "TEMPO_POLL_ERRORS_MAX", "> {}"),
        ("LogShipperDroppingEntries", None, "SHIPPER_DROPPED_WINDOW", "[{}]"),
        ("OtelCollectorExportFailing", None, "OTELCOL_SEND_FAILED_WINDOW", "[{}]"),
        ("TempoBlocklistPollFailing", None, "TEMPO_POLL_ERRORS_WINDOW", "[{}]"),
    ],
)
def test_a_rule_threshold_or_window_equals_the_bridges(alert, service, key, literal):
    expr = rule_expr("log-pipeline", alert, service)
    assert literal.format(bridge_env(key)) in expr


def test_the_boot_graces_equal_the_bridges():
    """The settle hold is BOOT_SETTLE_S plus the 1h range, and the backlog hold is its own."""
    window = bridge_env("SHIPPER_DROPPED_WINDOW")
    assert window == "1h"
    settle = int(bridge_env("BOOT_SETTLE_S")) + 3600
    backlog = int(bridge_env("SHIPPER_BACKLOG_GRACE_S"))
    client = rule_expr("log-pipeline", "LogShipperDroppingEntries")
    server = rule_expr("log-pipeline", "LokiDiscardingEntries", "loki-homelab")
    assert f"< {settle})" in client
    assert f"< {settle})" in server
    assert f"< {backlog})" in server
