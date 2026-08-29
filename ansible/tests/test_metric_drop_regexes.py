#!/usr/bin/env python3
"""A `metric_relabel_configs` drop that stops matching is SILENT.

Prometheus does not warn about a regex that matches nothing, and it does not warn about one
that matches too much either. Both failures look identical from the outside — the config loads,
the targets go up, the boards render. The only visible difference is a series count nobody is
watching, weeks later.

So each drop rule here is pinned by a pair: a name it MUST drop, and a name it MUST NOT. A rule
that fires on everything and a rule that fires on nothing are indistinguishable from the passing
side alone, which is why neither half is sufficient by itself.

The MUST NOT cases are not hypothetical. `container_memory_failcnt` is a live cross-estate
signal that monitor-bridge reads (check.py:377) and it sits one prefix away from
`container_memory_failures_total`, which this config drops. Any `container_memory_fail.*`
spelling would take both.

Run: uv run pytest ansible/tests/test_metric_drop_regexes.py
"""

import re
import sys as _sys
from pathlib import Path as _Path

import pytest
import yaml

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1] / "tests"))
_sys.path.insert(0, str(_Path(__file__).resolve().parents[2] / "scripts" / "validate"))

from _k8s_render import rendered_docs  # noqa: E402 — needs the path inserts above


def _scrape_jobs():
    """Every scrape job out of the rendered prometheus ConfigMap, by job_name.

    Parsed from the EMBEDDED prometheus.yml rather than the manifest, because the manifest only
    ever sees it as an opaque block scalar — a broken inner config renders and applies cleanly.
    """
    for role, template, doc in rendered_docs():
        if role != "claude-otel" or "prometheus" not in str(template):
            continue
        if not isinstance(doc, dict) or doc.get("kind") != "ConfigMap":
            continue
        config = yaml.safe_load(doc["data"]["prometheus.yml"])
        return {job["job_name"]: job for job in config["scrape_configs"]}
    pytest.fail("no prometheus ConfigMap rendered — this guard is watching nothing")


def _drop_regexes(job):
    """The compiled drop regexes on a job, anchored the way Prometheus anchors them."""
    rules = job.get("metric_relabel_configs") or []
    return [
        re.compile(f"^(?:{rule['regex']})$")
        for rule in rules
        if rule.get("action") == "drop"
    ]


def _drops(job_name: str, metric: str) -> bool:
    job = _scrape_jobs()[job_name]
    return any(pattern.match(metric) for pattern in _drop_regexes(job))


# (job, metric) pairs the config must keep dropping. Series counts measured 2026-08-29.
MUST_DROP = [
    ("kubernetes-cadvisor", "container_blkio_device_usage_total"),
    ("kubernetes-cadvisor", "container_tasks_state"),
    ("kubernetes-cadvisor", "container_memory_failures_total"),
    ("longhorn", "longhorn_rest_client_rate_limiter_latency_seconds_bucket"),
    ("longhorn", "longhorn_rest_client_request_latency_seconds_bucket"),
    ("longhorn", "longhorn_workqueue_queue_duration_seconds_bucket"),
    ("longhorn", "longhorn_workqueue_work_duration_seconds_bucket"),
]

# (job, metric, why it matters) — the half that catches a rule widened past its intent.
MUST_KEEP = [
    (
        "kubernetes-cadvisor",
        "container_memory_failcnt",
        "monitor-bridge reads it (check.py:377); one prefix from container_memory_failures_total",
    ),
    (
        "kubernetes-cadvisor",
        "container_cpu_usage_seconds_total",
        "queried by both cadvisor dashboards and homelab-mcp/files/app.py:305",
    ),
    (
        "kubernetes-cadvisor",
        "container_start_time_seconds",
        "monitor-bridge's restart arm reads it (check.py:1145)",
    ),
    (
        "kubernetes-cadvisor",
        "container_oom_events_total",
        "monitor-bridge reads it (check.py:1171)",
    ),
    (
        "longhorn",
        "longhorn_workqueue_work_duration_seconds_sum",
        "_sum and _count survive deliberately, so rates and averages stay available",
    ),
    (
        "longhorn",
        "longhorn_workqueue_work_duration_seconds_count",
        "_sum and _count survive deliberately, so rates and averages stay available",
    ),
    (
        "longhorn",
        "longhorn_volume_robustness",
        "the volume-state signal; 12 references in the repo",
    ),
    (
        "longhorn",
        "longhorn_disk_usage_bytes",
        "storage headroom; 5 references in the repo",
    ),
]


@pytest.mark.parametrize("job,metric", MUST_DROP)
def test_the_drop_rule_still_matches_what_it_was_added_for(job, metric):
    assert _drops(job, metric), (
        f"{metric} is no longer dropped on job {job}. The rule was added to remove it; if it "
        "stopped matching, the series are silently back and nothing else reports that."
    )


@pytest.mark.parametrize("job,metric,why", MUST_KEEP)
def test_the_drop_rule_does_not_reach_a_metric_something_reads(job, metric, why):
    assert not _drops(job, metric), (
        f"{metric} is now dropped on job {job}, and it must not be — {why}. A drop regex was "
        "widened past the families it was written for."
    )


def test_the_longhorn_drop_is_separate_from_the_control_plane_macro():
    """The macro's families are UNPREFIXED and belong to the kubelet job.

    Folding the longhorn_-prefixed twins into control_plane_bucket_drop() would apply the
    longhorn job's decision to the kubelet job by a route neither job's reader would expect.
    """
    jobs = _scrape_jobs()

    kubelet_regexes = [p.pattern for p in _drop_regexes(jobs["kubernetes-kubelet"])]

    assert kubelet_regexes, "the kubelet job lost its bucket drop entirely"
    assert not any("longhorn_" in pattern for pattern in kubelet_regexes), (
        "the control-plane bucket macro now names longhorn_ families, so it is being shared "
        "between two jobs that made the decision separately"
    )


def test_the_kubelet_family_all_scrapes_at_one_minute():
    """The four control-plane jobs were relaxed to 1m for BYTES; kubelet-resource was missed.

    It is in the family because it is a kubelet endpoint on the same node SD, and it emits
    container_* names that collide with the cadvisor job — so an interval mismatch here shows up
    as uneven sampling on series a consumer cannot tell apart.
    """
    jobs = _scrape_jobs()

    family = [
        "kube-state-metrics",
        "kubernetes-cadvisor",
        "kubernetes-kubelet",
        "kubernetes-kubelet-resource",
        "kubernetes-apiserver",
    ]

    inheriting = [name for name in family if "scrape_interval" not in jobs[name]]

    assert not inheriting, (
        f"{inheriting} inherit the 15s global. These are the largest series producers in the "
        "config and the retention cap is on BYTES — see the interval note at kube-state-metrics."
    )
