#!/usr/bin/env python3
"""A ported dashboard querying labels this cluster never exports renders "No data" forever.

Two ports drifted from what the cluster actually emits, and neither failure is visible from a
green deploy — Grafana provisions the board, the panels build a query, and the query matches
nothing.

  Security/  The four CrowdSec boards came from an upstream that labels each agent `machine`.
             This cluster's Prometheus labels the node-agent DaemonSet `node`
             (`job_name: crowdsec-node-agents` in claude-otel/templates/prometheus.yaml.j2) and
             labels the central LAPI pod with neither. It also acquires from FILES and appsec
             only (crowdsec/templates/node-agent-acquis.yaml.j2, config-secret.yaml.j2), so the
             journald, syslog and CloudWatch acquisition counters can never exist.
             `node` is not CrowdSec's own label either: alloy's DaemonSet carries it, so a
             selector naming `node` alone answers with alloy's series alongside CrowdSec's --
             a WRONG answer rather than an empty one, which no dead-panel audit can see.
  AI/        The OTEL collector ships Claude Code logs to the stack-native Loki
             (`endpoint: http://loki:3100/otlp`, claude-otel/templates/collector.yaml.j2), which
             Grafana provisions as uid `loki`. The `loki-homelab` uid holds the fleet's syslog —
             the Deploys ANNOTATION belongs there, and every log PANEL does not.

Paired, per the repo's red-proof rule: each rule has one input it must accept and one it must
reject, plus a non-vacuity assertion, because all three rules walk a glob and an empty glob
passes an `all()`.

Run: uv run pytest ansible/tests/services/test_dashboard_queries_match_this_clusters_labels.py
"""

import json
import re
from pathlib import Path

from _helpers import ANSIBLE

DASHBOARDS = ANSIBLE / "roles" / "k8s" / "claude-otel" / "files" / "dashboards"
SECURITY = DASHBOARDS / "Security"

# The homelab-syslog Loki. Correct for the Deploys annotation, wrong for a Claude Code log panel.
LOKI_HOMELAB_UID = "bf4q19tuivta8e"

# The two boards whose log panels read Claude Code's own OTLP logs.
CLAUDE_BOARDS = frozenset(
    {
        "claude-code-usage-observability.json",
        "claude-code-pipeline-health-cost-by-host.json",
    }
)

# Named rather than counted: a census that globs for its own subject returns an empty set the
# moment the tree moves, and every assertion below then passes on nothing.
KNOWN_CROWDSEC_BOARDS = frozenset(
    {
        "crowdsec-overview.json",
        "crowdsec-insight.json",
        "crowdsec-details-per-machine.json",
        "lapi-metrics.json",
    }
)

# Acquisition counters this deployment cannot emit, whatever its activity. Distinct from the
# bucket counters (cs_buckets, cs_bucket_created_total, ...), which are absent only while nothing
# has overflowed — those panels are correct and stay.
UNEMITTED_METRICS = frozenset(
    {
        "cs_journalctlsource_hits_total",
        "cs_syslogsource_hits_total",
        "cs_cloudwatch_stream_hits_total",
    }
)

# `machine="..."`, `machine=~"..."`, `by (machine)` or a `{{machine}}` legend.
MACHINE_LABEL = re.compile(r"\bmachine\b")

# The job every per-node CrowdSec selector must pin alongside `node`.
NODE_AGENT_JOB = 'job="crowdsec-node-agents"'

# A `{...}` selector naming the node variable.
NODE_SELECTOR = re.compile(r"\{[^{}]*node=\"\$node\"[^{}]*\}")

# Metrics only the central LAPI pod emits. Prometheus scrapes it as `job: crowdsec` with no
# `node` label at all (claude-otel/templates/prometheus.yaml.j2), so a per-node selector on any
# of these matches nothing however the rename went.
LAPI_ONLY_METRICS = frozenset(
    {"cs_alerts", "cs_active_decisions", "cs_lapi_request_duration_seconds_bucket"}
)


def _panels(doc):
    """Every panel in a board, flattened through row children."""
    for panel in doc.get("panels", []):
        yield panel
        yield from panel.get("panels", [])


def _exprs(doc):
    for panel in _panels(doc):
        for target in panel.get("targets", []):
            if "expr" in target:
                yield target["expr"]


def _prometheus_exprs(doc):
    """PromQL only — the Deploys annotation and the log panels are LogQL."""
    return [e for e in _exprs(doc) if not e.lstrip().startswith("{job=")]


def exprs_naming_a_machine_label(board: Path) -> list[str]:
    doc = json.loads(board.read_text())
    named = [e for e in _prometheus_exprs(doc) if MACHINE_LABEL.search(e)]
    named += [
        v.get("query", {}).get("query", v.get("query", ""))
        if isinstance(v.get("query"), dict)
        else v.get("query", "")
        for v in doc.get("templating", {}).get("list", [])
        if MACHINE_LABEL.search(json.dumps(v))
    ]
    return named


def exprs_naming_an_unemitted_metric(board: Path) -> list[str]:
    doc = json.loads(board.read_text())
    return [e for e in _prometheus_exprs(doc) if any(m in e for m in UNEMITTED_METRICS)]


def panels_reading_the_wrong_loki(board: Path) -> list[str]:
    doc = json.loads(board.read_text())
    wrong = []
    for panel in _panels(doc):
        sources = [panel.get("datasource")] + [
            t.get("datasource") for t in panel.get("targets", [])
        ]
        if any(
            isinstance(d, dict) and d.get("uid") == LOKI_HOMELAB_UID for d in sources
        ):
            wrong.append(panel.get("title", "<untitled>"))
    return wrong


def test_no_crowdsec_board_filters_on_a_machine_label():
    """The accepting half, against the real tree."""
    offenders = {
        b.name: exprs_naming_a_machine_label(b) for b in SECURITY.glob("*.json")
    }
    assert {k: v for k, v in offenders.items() if v} == {}


def selectors_naming_node_without_its_job(board: Path) -> list[str]:
    """Every `node="$node"` selector that does not also pin the node-agent job."""
    doc = json.loads(board.read_text())
    queries = list(_prometheus_exprs(doc))
    for v in doc.get("templating", {}).get("list", []):
        q = v.get("query")
        queries.append(q["query"] if isinstance(q, dict) else q or "")
    return [
        m.group(0)
        for q in queries
        for m in NODE_SELECTOR.finditer(q)
        if NODE_AGENT_JOB not in m.group(0)
    ]


def test_every_node_selector_pins_the_crowdsec_node_agent_job():
    """Otherwise the panel silently mixes in alloy's series for the same node."""
    offenders = {
        b.name: selectors_naming_node_without_its_job(b)
        for b in SECURITY.glob("*.json")
    }
    assert {k: v for k, v in offenders.items() if v} == {}


def lapi_metrics_filtered_by_node(board: Path) -> list[str]:
    doc = json.loads(board.read_text())
    return [
        e
        for e in _prometheus_exprs(doc)
        for m in LAPI_ONLY_METRICS
        if m + "{" in e and NODE_SELECTOR.search(e)
    ]


def test_no_board_filters_a_lapi_only_metric_by_node():
    offenders = {
        b.name: lapi_metrics_filtered_by_node(b) for b in SECURITY.glob("*.json")
    }
    assert {k: v for k, v in offenders.items() if v} == {}


def test_no_crowdsec_board_reads_an_acquisition_metric_this_cluster_cannot_emit():
    offenders = {
        b.name: exprs_naming_an_unemitted_metric(b) for b in SECURITY.glob("*.json")
    }
    assert {k: v for k, v in offenders.items() if v} == {}


def test_no_claude_code_log_panel_reads_the_homelab_loki():
    offenders = {
        name: panels_reading_the_wrong_loki(DASHBOARDS / "AI" / name)
        for name in CLAUDE_BOARDS
    }
    assert {k: v for k, v in offenders.items() if v} == {}


def test_the_deploys_annotation_still_reads_the_homelab_loki():
    """The inverse of the rule above, and the reason it is scoped to panels.

    A blanket uid rewrite would repoint this annotation too, and the fleet's deploy markers
    live in the syslog Loki. Both boards would then annotate nothing.
    """
    for name in CLAUDE_BOARDS:
        doc = json.loads((DASHBOARDS / "AI" / name).read_text())
        annotations = doc["annotations"]["list"]
        assert annotations, name
        for a in annotations:
            assert a["datasource"]["uid"] == LOKI_HOMELAB_UID, (name, a.get("name"))


def test_a_board_that_broke_each_rule_would_be_reported(tmp_path):
    """The rejecting half. Without it a rule that stopped matching still reads green."""
    planted = tmp_path / "planted.json"
    planted.write_text(
        json.dumps(
            {
                "annotations": {"list": []},
                "templating": {"list": []},
                "panels": [
                    {
                        "title": "row",
                        "panels": [
                            {
                                "title": "planted",
                                "datasource": {"uid": LOKI_HOMELAB_UID},
                                "targets": [
                                    {
                                        "expr": 'sum(cs_journalctlsource_hits_total{machine="$machine"})'
                                    }
                                ],
                            }
                        ],
                    }
                ],
            }
        )
    )
    assert exprs_naming_a_machine_label(planted)
    assert exprs_naming_an_unemitted_metric(planted)
    assert panels_reading_the_wrong_loki(planted) == ["planted"]

    unscoped = tmp_path / "unscoped.json"
    unscoped.write_text(
        json.dumps(
            {
                "annotations": {"list": []},
                "templating": {"list": []},
                "panels": [
                    {
                        "title": "planted",
                        "targets": [
                            {"expr": 'process_resident_memory_bytes{node="$node"}'}
                        ],
                    }
                ],
            }
        )
    )
    assert selectors_naming_node_without_its_job(unscoped) == ['{node="$node"}']

    lapi = tmp_path / "lapi.json"
    lapi.write_text(
        json.dumps(
            {
                "annotations": {"list": []},
                "templating": {"list": []},
                "panels": [
                    {
                        "title": "planted",
                        "targets": [{"expr": 'cs_alerts{node="$node"}'}],
                    }
                ],
            }
        )
    )
    assert lapi_metrics_filtered_by_node(lapi) == ['cs_alerts{node="$node"}']


def test_the_boards_the_rules_read_are_all_still_there():
    """Non-vacuity. Every rule above walks a glob or a name set, and both can go empty."""
    assert {b.name for b in SECURITY.glob("*.json")} >= KNOWN_CROWDSEC_BOARDS
    for name in CLAUDE_BOARDS:
        assert (DASHBOARDS / "AI" / name).is_file(), name
    # And the boards genuinely carry PromQL to inspect, so the filters above run on something.
    total = sum(
        len(_prometheus_exprs(json.loads(b.read_text())))
        for b in SECURITY.glob("*.json")
    )
    assert total >= 40, total
    # And the per-node boards genuinely carry node selectors, so the job-scoping rule above
    # is not passing on a board that stopped using the variable.
    scoped = sum(
        len(NODE_SELECTOR.findall(e))
        for b in SECURITY.glob("*.json")
        for e in _prometheus_exprs(json.loads(b.read_text()))
    )
    assert scoped >= 15, scoped
