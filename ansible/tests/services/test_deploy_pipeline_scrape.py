"""observability's `deploy-pipeline` scrape job targets the Service monitor-bridge renders.

The job is hand-written in `prometheus.yaml.j2` with a literal port, because observability
cannot read `monitor_bridge_deploy_pipeline_port` (role defaults do not cross the deploy play's
include_role loop), and a `metrics` item on monitor-bridge's entry would widen every deploy of
that entry to each role reading the field. These guards render both sides and hold them equal,
so a port moved in one role alone fails here rather than as `up == 0` in production.

Run: uv run pytest ansible/tests/services/test_deploy_pipeline_scrape.py
"""

from _k8s_render import render_role_template
from lib import yaml_fast

JOB = "deploy-pipeline"


def scrape_job() -> dict:
    """The rendered `deploy-pipeline` job from observability's prometheus.yml."""
    text = render_role_template("observability", "prometheus.yaml.j2")
    configmap = next(
        doc
        for doc in yaml_fast.safe_load_all(text)
        if doc and doc.get("kind") == "ConfigMap"
    )
    config = yaml_fast.safe_load(configmap["data"]["prometheus.yml"])
    [job] = [j for j in config["scrape_configs"] if j["job_name"] == JOB]
    return job


def service_address(service: dict) -> str:
    """The in-cluster `<name>.<namespace>.svc:<port>` of a rendered Service's `metrics` port."""
    [port] = [p for p in service["spec"]["ports"] if p.get("name") == "metrics"]
    meta = service["metadata"]
    return "%s.%s.svc:%s" % (meta["name"], meta["namespace"], port["port"])


def bridge_service() -> dict:
    return yaml_fast.safe_load(
        render_role_template("monitor-bridge", "service.yaml.j2")
    )


def test_the_job_targets_monitor_bridges_metrics_service():
    [static] = scrape_job()["static_configs"]
    assert static["targets"] == [service_address(bridge_service())]


def test_a_service_on_another_port_is_flagged():
    service = bridge_service()
    service["spec"]["ports"][0]["port"] += 1
    [static] = scrape_job()["static_configs"]
    assert static["targets"] != [service_address(service)]


def test_the_job_carries_no_origin_label():
    """No `origin`, so Kuma's Cluster Scrape Targets tile pages a failed scrape, as the
    DeployPipelineExporterAbsent rule's `up == 1` conjunct assumes (M1 convention 5)."""
    [static] = scrape_job()["static_configs"]
    assert "origin" not in static.get("labels", {})
