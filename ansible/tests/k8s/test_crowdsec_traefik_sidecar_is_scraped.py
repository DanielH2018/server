"""Issue #1694: the traefik pod's CrowdSec sidecar parsed the busiest log in the fleet unseen.

Prometheus scraped two CrowdSec jobs — the central LAPI (`crowdsec`) and the node-agent
DaemonSet (`crowdsec-node-agents`) — and the edge sidecar was in neither. Measured 2026-09-10:
`group by (job, node) (up{job=~"crowdsec.*"})` returned exactly three series, the LAPI and the
two node agents. Its parser, acquisition and bucket counters therefore existed nowhere, so a
sidecar that stopped parsing read as nothing at all rather than as a drop.

Three parts have to hold together, and each fails silently on its own:

- **The scrape job selects the sidecar.** Pod-role SD emits one target per DECLARED
  containerPort, so the job keeps pods labelled `app: traefik` on port 6060.
- **The sidecar declares that port.** Without it the keep above matches nothing — the job
  renders, applies and discovers zero targets, which reads identically to a job nobody added.
- **The netpol admits prometheus to it.** traefik's baseline policy fences every port but the
  two front-door ones, so an unlisted 6060 reads `up == 0` forever.

Each rule is a predicate with an accepting and a rejecting input, then applied to the real
rendered manifests behind a non-vacuity assertion — a census that globs for its own subject
passes on an empty set the moment the tree moves.

Run: uv run pytest ansible/tests/k8s/test_crowdsec_traefik_sidecar_is_scraped.py
"""

from _k8s_render import rendered_docs
from lib import yaml_fast

JOB_NAME = "crowdsec-traefik-agent"
SIDECAR = "crowdsec-agent"
METRICS_PORT = 6060


# --- the rules, as predicates -------------------------------------------------------


def the_job_keeps_the_traefik_pod_on_its_metrics_port(job):
    """True when the job's relabel chain keeps `app: traefik` pods on the metrics port."""
    if job is None:
        return False
    keeps = {
        (tuple(r.get("source_labels") or ()), str(r.get("regex")))
        for r in job.get("relabel_configs") or []
        if r.get("action") == "keep"
    }
    return {
        (("__meta_kubernetes_pod_label_app",), "traefik"),
        (("__meta_kubernetes_pod_container_port_number",), str(METRICS_PORT)),
    } <= keeps


def the_container_declares_the_port(container, port):
    """True when the container declares `port` — what makes pod SD emit a target for it."""
    return port in {
        p.get("containerPort") for p in (container or {}).get("ports") or []
    }


def the_policy_admits_prometheus_on(policy, port):
    """True when some ingress rule admits the prometheus pod on exactly `port`."""
    for rule in (policy.get("spec") or {}).get("ingress") or []:
        sources = {
            ((src.get("podSelector") or {}).get("matchLabels") or {}).get("app")
            for src in rule.get("from") or []
        }
        ports = {p.get("port") for p in rule.get("ports") or []}
        if "prometheus" in sources and port in ports:
            return True
    return False


# --- the rejecting halves -----------------------------------------------------------


def test_a_job_without_the_two_keeps_is_flagged():
    """The node-agent job's shape with the app regex left pointing at the DaemonSet."""
    node_agent_shaped = {
        "job_name": JOB_NAME,
        "relabel_configs": [
            {
                "source_labels": ["__meta_kubernetes_pod_label_app"],
                "regex": "crowdsec-node-agent",
                "action": "keep",
            },
            {
                "source_labels": ["__meta_kubernetes_pod_container_port_number"],
                "regex": "6060",
                "action": "keep",
            },
        ],
    }
    assert not the_job_keeps_the_traefik_pod_on_its_metrics_port(node_agent_shaped)
    assert not the_job_keeps_the_traefik_pod_on_its_metrics_port(None)


def test_a_sidecar_declaring_no_port_is_flagged():
    """The pre-#1694 sidecar: a container with volumeMounts and probes and no `ports`."""
    assert not the_container_declares_the_port(
        {"name": SIDECAR, "volumeMounts": []}, METRICS_PORT
    )
    assert not the_container_declares_the_port(
        {"name": SIDECAR, "ports": [{"containerPort": 8080}]}, METRICS_PORT
    )
    assert the_container_declares_the_port(
        {"name": SIDECAR, "ports": [{"containerPort": METRICS_PORT}]}, METRICS_PORT
    )


def test_a_policy_granting_only_the_dashboard_port_is_flagged():
    """The pre-#1694 traefik policy: prometheus admitted to 8080 alone."""
    dashboard_only = {
        "spec": {
            "ingress": [
                {
                    "from": [{"podSelector": {"matchLabels": {"app": "prometheus"}}}],
                    "ports": [{"port": 8080, "protocol": "TCP"}],
                }
            ]
        }
    }
    assert not the_policy_admits_prometheus_on(dashboard_only, METRICS_PORT)
    assert the_policy_admits_prometheus_on(dashboard_only, 8080)


# --- the accepting halves, against the real tree ------------------------------------


def _rendered():
    """The scrape jobs, the traefik sidecar container and the traefik NetworkPolicy."""
    jobs = sidecar = policy = None
    for role, template, doc in rendered_docs():
        if not isinstance(doc, dict):
            continue
        if (
            role == "claude-otel"
            and "prometheus" in str(template)
            and doc.get("kind") == "ConfigMap"
        ):
            jobs = yaml_fast.safe_load(doc["data"]["prometheus.yml"])["scrape_configs"]
        elif role == "traefik" and doc.get("kind") == "Deployment":
            for container in doc["spec"]["template"]["spec"]["containers"]:
                if container.get("name") == SIDECAR:
                    sidecar = container
        elif (
            doc.get("kind") == "NetworkPolicy"
            and doc.get("metadata", {}).get("name") == "traefik"
        ):
            policy = doc
    return jobs, sidecar, policy


def test_the_three_subjects_this_guard_reads_are_all_still_there():
    """Non-vacuity: each lookup below passes trivially if its subject went missing."""
    jobs, sidecar, policy = _rendered()
    assert jobs, "no prometheus ConfigMap rendered — this guard is watching nothing"
    assert sidecar is not None, f"traefik renders no `{SIDECAR}` container"
    assert policy is not None, "no `traefik` NetworkPolicy rendered"


def test_the_traefik_sidecar_has_a_scrape_job_of_its_own():
    jobs, _, _ = _rendered()
    job = next((j for j in jobs or [] if j.get("job_name") == JOB_NAME), None)
    assert job is not None, f"no `{JOB_NAME}` scrape job — issue #1694 regressed"
    assert the_job_keeps_the_traefik_pod_on_its_metrics_port(job)


def test_the_sidecar_declares_the_port_the_job_keeps_on():
    _, sidecar, _ = _rendered()
    assert the_container_declares_the_port(sidecar, METRICS_PORT)


def test_the_traefik_policy_admits_prometheus_to_the_sidecar():
    _, _, policy = _rendered()
    assert the_policy_admits_prometheus_on(policy, METRICS_PORT)
