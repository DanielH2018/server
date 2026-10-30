"""M6 batch A3's shadow rules cover every scrape job and mirror monitor-bridge's numbers.

`files/rules/scrape-targets.yaml` is plain YAML, so `ScrapeTargetDown` cannot loop over the
jobs in `prometheus.yml`. It is one rule per owning role, each with a literal `service`, and a
last rule that catches any job the others do not name (M1 convention 1 and 3). The first half
of this module holds that table to the rendered scrape config: every job is in exactly one
named rule, so the "unmapped" fallback pages a job nobody mapped and is never where a job
lives. Kuma's two tiles exempt no job, and neither may this rule.

The second half holds the numbers `log-pipeline.yaml` and `scrape-targets.yaml` ported from
monitor-bridge's two deleted checks. Cutover-a2 removed their env keys, so the literals below
are the record of what the bridge carried (M1 convention 1).

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


def rule_expr(stem: str, alert: str, service: str | None = None) -> str:
    """The expression of the one rule named `alert` (with `service`) in `stem`.yaml."""
    [group] = yaml_fast.safe_load((RULES / f"{stem}.yaml").read_text())["groups"]
    [expr] = [
        r["expr"]
        for r in group["rules"]
        if r["alert"] == alert and service in (None, r["labels"]["service"])
    ]
    return str(expr)


# What the bridge's two checks carried when Cutover-a2 deleted them with their env keys
# (TARGETS_MIN, CLUSTER_TARGETS_MIN, SHIPPER_DROPPED_*, OTELCOL_SEND_FAILED_*,
# TEMPO_POLL_ERRORS_*, BOOT_SETTLE_S and SHIPPER_BACKLOG_GRACE_S in the bridge's
# config/env.yaml.j2). The rules hold them as literals, so these literals are the only record.
# DECIDED: the 3000-entry threshold of LogShipperDroppingEntries and LokiDiscardingEntries came
# from the gap between churn and bursts, not from a streak. Measured 2026-09-11 over the 14 days
# Prometheus retains, at 1h resolution: the client-side counter is non-zero in 26 of ~336 hours,
# 13 of those clear 1000, and the ONLY samples between 1000 and 6261 are the two that held the
# old monitor red for 51 minutes on 2026-09-03 at 1020. Every other real loss starts at 6261 and
# runs to 90,724 client-side and 216,545 server-side. Churn tops out at ~1020 and the smallest
# genuine burst is 6261, so 3000 sits 2.9x above the first and 2.1x below the second: a derived
# midpoint, not a number fitted to the episode. A 2-3 cycle streak was rejected on its own
# evidence: that episode ran 11 cycles, so a streak would have removed 3 of them. The marker
# moved here from the bridge's `bridge/config_io.py` (SHIPPER_DROPPED_MAX) when Cutover-a2
# deleted that check.
BRIDGE_TARGET_FLOORS = ("3", "1")  # CLUSTER_TARGETS_MIN, TARGETS_MIN
BRIDGE_SETTLE_HOLD_S = 1200 + 3600  # BOOT_SETTLE_S plus the 1h range
BRIDGE_BACKLOG_GRACE_S = 21600  # SHIPPER_BACKLOG_GRACE_S


def test_the_floors_equal_the_deleted_bridge_checks():
    expr = rule_expr("scrape-targets", "ScrapeTargetsMissing")
    for floor in BRIDGE_TARGET_FLOORS:
        assert f"< {floor}" in expr


@pytest.mark.parametrize(
    ("alert", "service", "literal"),
    [
        ("LogShipperDroppingEntries", None, "> 3000"),
        ("LokiDiscardingEntries", "loki-homelab", "> 3000"),
        ("OtelCollectorExportFailing", None, "> 0"),
        ("TempoBlocklistPollFailing", None, "> 0"),
        ("LogShipperDroppingEntries", None, "[1h]"),
        ("OtelCollectorExportFailing", None, "[15m]"),
        ("TempoBlocklistPollFailing", None, "[15m]"),
    ],
)
def test_a_rule_threshold_or_window_equals_the_deleted_bridge_check(
    alert, service, literal
):
    assert literal in rule_expr("log-pipeline", alert, service)


def test_the_boot_graces_equal_the_deleted_bridge_check():
    """The settle hold is BOOT_SETTLE_S plus the 1h range, and the backlog hold is its own."""
    client = rule_expr("log-pipeline", "LogShipperDroppingEntries")
    server = rule_expr("log-pipeline", "LokiDiscardingEntries", "loki-homelab")
    assert f"< {BRIDGE_SETTLE_HOLD_S})" in client
    assert f"< {BRIDGE_SETTLE_HOLD_S})" in server
    assert f"< {BRIDGE_BACKLOG_GRACE_S})" in server
