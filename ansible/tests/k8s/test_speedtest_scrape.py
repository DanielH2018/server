"""Issue #996: the WAN speed test had a Kuma tile but no Prometheus series, so a
degradation had no history. monitor-bridge's `speedtest` check reads the app's REST API
directly and pushes a verdict to Kuma — a tile, not a series — and nothing scraped the
app itself, so "is 78.8 Mbps a one-off or a week-long slide" was unanswerable.

speedtest-tracker natively exposes `/prometheus` with no config needed on its side, so the
fix is a scrape job in claude-otel's prometheus.yaml.j2, mirroring the terraria-stats and
valheim-stats jobs it sits beside. This guards that job's shape: present, at the right
path, at the right target — the ways it could regress into a silently-empty or 404ing
target without any renderer or test noticing.

Rendering goes through the same `_k8s_render.rendered_docs()` machinery every other
`test_k8s_manifests_*` guard uses, so this cannot drift from what that validator considers
a renderable manifest.
"""

from __future__ import annotations

import pytest
from lib import yaml_fast

from _k8s_render import rendered_docs


def _speedtest_job(jobs: list[dict]) -> dict | None:
    for job in jobs:
        if job.get("job_name") == "speedtest":
            return job
    return None


def _scrape_jobs() -> list[dict]:
    for role, template, doc in rendered_docs():
        if role != "claude-otel" or "prometheus" not in str(template):
            continue
        if not isinstance(doc, dict) or doc.get("kind") != "ConfigMap":
            continue
        return yaml_fast.safe_load(doc["data"]["prometheus.yml"])["scrape_configs"]
    pytest.fail("no prometheus ConfigMap rendered — this guard is watching nothing")


def test_speedtest_is_scraped_at_its_native_prometheus_endpoint() -> None:
    job = _speedtest_job(_scrape_jobs())
    assert job is not None, "no `speedtest` scrape job rendered — issue #996 regressed"
    assert job["metrics_path"] == "/prometheus"
    assert job["static_configs"] == [{"targets": ["speedtest.homelab.svc:80"]}]


def test_a_job_list_without_the_speedtest_entry_is_flagged() -> None:
    """Proves the lookup above can actually fail, not just pass on whatever the render
    happens to produce — the sibling jobs it sits beside, with the entry itself removed."""
    jobs = [
        {"job_name": "terraria-stats", "static_configs": [{"targets": ["x:9420"]}]},
        {"job_name": "valheim-stats", "static_configs": [{"targets": ["y:9420"]}]},
    ]
    assert _speedtest_job(jobs) is None
