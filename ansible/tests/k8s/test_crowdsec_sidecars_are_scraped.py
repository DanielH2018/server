"""Issues #1694 and #1706: two CrowdSec sidecars parsed their logs unseen.

Prometheus scraped two CrowdSec jobs — the central LAPI (`crowdsec`) and the node-agent
DaemonSet (`crowdsec-node-agents`) — and the pod sidecars were in neither. Measured 2026-09-10:
`group by (job, node) (up{job=~"crowdsec.*"})` returned exactly three series, the LAPI and the
two node agents. The traefik sidecar parses the busiest log in the fleet (#1694) and the
authelia one parses the portal's failed logins (#1706), so their parser, acquisition and bucket
counters existed nowhere — a sidecar that stopped parsing read as nothing at all rather than as
a drop.

Three parts have to hold together per sidecar, and each fails silently on its own:

- **The scrape job selects the sidecar.** Pod-role SD emits one target per DECLARED
  containerPort, so the job keeps pods by their `app` label on port 6060.
- **The sidecar declares that port.** Without it the keep above matches nothing — the job
  renders, applies and discovers zero targets, which reads identically to a job nobody added.
- **The netpol admits prometheus to it.** Each baseline policy fences every port but the
  service's own, so an unlisted 6060 reads `up == 0` forever. That grant is a `from` item of
  its own: a NetworkPolicy rule ANDs its `from` with its `ports`, so appending 6060 to an
  existing rule admits that rule's caller and not prometheus.

Each rule is a predicate with an accepting and a rejecting input, then applied to the real
rendered manifests behind a non-vacuity assertion — a census that globs for its own subject
passes on an empty set the moment the tree moves.

Run: uv run pytest ansible/tests/k8s/test_crowdsec_sidecars_are_scraped.py
"""

import pytest
from _k8s_render import rendered_docs
from lib import yaml_fast

SIDECAR = "crowdsec-agent"
METRICS_PORT = 6060

# Named rather than counted, and asserted as a SUBSET of the rendered jobs below: the fleet
# went two jobs -> three (#1694) -> four (#1706) and will move again, so an equality check
# would fail on the next addition rather than on a regression.
SIDECAR_PODS = (
    # (the role and app label, the scrape job that must cover its sidecar, the NetworkPolicy)
    ("traefik", "crowdsec-traefik-agent", "traefik"),
    ("authelia", "crowdsec-authelia-agent", "authelia"),
)


# --- the rules, as predicates -------------------------------------------------------


def the_job_keeps_the_pod_on_its_metrics_port(job, app):
    """True when the job's relabel chain keeps `app: <app>` pods on the metrics port."""
    if job is None:
        return False
    keeps = {
        (tuple(r.get("source_labels") or ()), str(r.get("regex")))
        for r in job.get("relabel_configs") or []
        if r.get("action") == "keep"
    }
    return {
        (("__meta_kubernetes_pod_label_app",), app),
        (("__meta_kubernetes_pod_container_port_number",), str(METRICS_PORT)),
    } <= keeps


def the_container_declares_the_port(container, port):
    """True when the container declares `port` — what makes pod SD emit a target for it."""
    return port in {
        p.get("containerPort") for p in (container or {}).get("ports") or []
    }


def the_policy_admits_prometheus_on(policy, port):
    """True when some ingress rule admits the prometheus pod on exactly `port`.

    The `from` and the `ports` are read from the SAME rule, because that is how a
    NetworkPolicy evaluates them — a port granted beside a different caller grants
    prometheus nothing.
    """
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


def test_a_job_pointed_at_another_pod_is_flagged():
    """The node-agent job's shape with the app regex left pointing at the DaemonSet."""
    node_agent_shaped = {
        "job_name": "crowdsec-traefik-agent",
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
    assert not the_job_keeps_the_pod_on_its_metrics_port(node_agent_shaped, "traefik")
    assert not the_job_keeps_the_pod_on_its_metrics_port(node_agent_shaped, "authelia")
    assert not the_job_keeps_the_pod_on_its_metrics_port(None, "traefik")
    assert the_job_keeps_the_pod_on_its_metrics_port(
        node_agent_shaped, "crowdsec-node-agent"
    )


def test_a_sidecar_declaring_no_port_is_flagged():
    """The pre-fix sidecar: a container with volumeMounts and probes and no `ports`."""
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


def test_a_port_appended_to_someone_elses_rule_is_flagged():
    """The #1706 near-miss: 6060 added to authelia's traefik rule instead of its own.

    Reads as a grant in a diff and grants prometheus nothing — the rule ANDs `from` with
    `ports`, so this admits TRAEFIK to the agent's metrics and nobody else.
    """
    wrong = {
        "spec": {
            "ingress": [
                {
                    "from": [{"podSelector": {"matchLabels": {"app": "traefik"}}}],
                    "ports": [
                        {"port": 9091, "protocol": "TCP"},
                        {"port": METRICS_PORT, "protocol": "TCP"},
                    ],
                }
            ]
        }
    }
    assert not the_policy_admits_prometheus_on(wrong, METRICS_PORT)


# --- the accepting halves, against the real tree ------------------------------------


def _rendered():
    """The scrape jobs, each pod's sidecar container and each baseline NetworkPolicy."""
    jobs = None
    sidecars, policies = {}, {}
    for role, template, doc in rendered_docs():
        if not isinstance(doc, dict):
            continue
        if (
            role == "claude-otel"
            and "prometheus" in str(template)
            and doc.get("kind") == "ConfigMap"
        ):
            jobs = yaml_fast.safe_load(doc["data"]["prometheus.yml"])["scrape_configs"]
        elif doc.get("kind") == "Deployment":
            for container in doc["spec"]["template"]["spec"]["containers"]:
                if container.get("name") == SIDECAR:
                    sidecars[role] = container
        elif doc.get("kind") == "NetworkPolicy":
            policies[doc.get("metadata", {}).get("name")] = doc
    return jobs, sidecars, policies


def test_every_subject_this_guard_reads_is_still_there():
    """Non-vacuity: each lookup below passes trivially if its subject went missing."""
    jobs, sidecars, policies = _rendered()
    assert jobs, "no prometheus ConfigMap rendered — this guard is watching nothing"
    for role, _, policy_name in SIDECAR_PODS:
        assert role in sidecars, f"{role} renders no `{SIDECAR}` container"
        assert policy_name in policies, f"no `{policy_name}` NetworkPolicy rendered"
    # And EQUALITY the other way: `_rendered()` collects a `crowdsec-agent` from any
    # Deployment, so a third sidecar added later would render unscraped with every test
    # above still green. This is the one assertion that notices it.
    assert set(sidecars) == {r for r, _, _ in SIDECAR_PODS}, (
        f"a pod runs {SIDECAR} and this guard does not cover it: {sorted(sidecars)}"
    )


@pytest.mark.parametrize(("role", "job_name", "policy_name"), SIDECAR_PODS)
def test_each_sidecar_has_a_scrape_job_of_its_own(role, job_name, policy_name):
    jobs, _, _ = _rendered()
    job = next((j for j in jobs or [] if j.get("job_name") == job_name), None)
    assert job is not None, f"no `{job_name}` scrape job — #1694/#1706 regressed"
    assert the_job_keeps_the_pod_on_its_metrics_port(job, role)


@pytest.mark.parametrize(("role", "job_name", "policy_name"), SIDECAR_PODS)
def test_each_sidecar_declares_the_port_its_job_keeps_on(role, job_name, policy_name):
    _, sidecars, _ = _rendered()
    assert the_container_declares_the_port(sidecars.get(role), METRICS_PORT)


@pytest.mark.parametrize(("role", "job_name", "policy_name"), SIDECAR_PODS)
def test_each_policy_admits_prometheus_to_the_sidecar(role, job_name, policy_name):
    _, _, policies = _rendered()
    assert the_policy_admits_prometheus_on(policies[policy_name], METRICS_PORT)
