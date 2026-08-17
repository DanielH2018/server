"""Containers with no readinessProbe are a decision, not an oversight.

A container with no readinessProbe is Ready the instant it starts, so a Service publishes it
before it can serve. That is worth fixing where a Service fronts it — and actively harmful for
a sidecar, because pod Ready is the AND of all containers: giving traefik's CrowdSec agent a
readinessProbe would take every route in the homelab out of service whenever the agent
hiccups. `traefik/templates/deployment.yaml.j2` says so in as many words.

So this guard does not demand universal coverage. It demands that every container without a
probe is listed below with the reason, which is the decision worth forcing on whoever adds the
next one.
"""

from __future__ import annotations

from _k8s_render import rendered_docs

_POD_KINDS = {"Deployment", "DaemonSet", "StatefulSet"}

# (role, container) -> why this container has no readinessProbe.
_NO_READINESS = {
    # ── sidecars: pod Ready is the AND of all containers, so a sidecar probe gates the
    #    whole pod's Service. Documented in each template.
    (
        "traefik",
        "crowdsec-agent",
    ): "sidecar readiness would gate the edge proxy's Service",
    ("authelia", "crowdsec-agent"): "same shape as traefik's sidecar; would gate SSO",
    ("crowdsec", "metabase"): "startupProbe already gates it; pod Ready is the AND",
    (
        "uptime-kuma",
        "autokuma",
    ): "sidecar; readiness would gate uptime-kuma's own Service",
    # ── game servers: a startupProbe already gates Service publication, so a readinessProbe
    #    would add nothing except a second probe hitting the same signal.
    (
        "terraria",
        "terraria",
    ): "startupProbe (grep /proc/net/tcp) already gates the Service; "
    "a tcpSocket connect probe would additionally log ~2900 fake 'is connecting...' "
    "joins/day in the game console",
    ("valheim", "valheim"): "startupProbe (grep /proc/net/udp+udp6) already gates the "
    "Service; UDP has no LISTEN state so tcpSocket is impossible anyway, and a connect "
    "probe would log a join attempt every cycle",
    # ── no Service fronts these; a livenessProbe restarts them and Kuma or Prometheus
    #    notices if they stop working. Readiness would add rollout gating only.
    ("autofix-bridge", "autofix-bridge"): "no Service; liveness plus Kuma monitors",
    ("janitorr", "janitorr"): "no Service; liveness plus Kuma monitors",
    ("karakeep", "time-tagger"): "no Service; liveness probe covers a hung worker",
    (
        "monitor-bridge",
        "monitor-bridge",
    ): "no Service; it is itself the monitor, and Kuma watches it",
    (
        "n8n",
        "n8n-runners",
    ): "no Service; task runners are dialled by the broker, not routed",
    ("crowdsec", "crowdsec-agent"): "DaemonSet agent, no Service; liveness covers it",
    (
        "node-exporter",
        "node-exporter",
    ): "scraped by Prometheus, so absence is a down target",
    # ── periodic workers with no server to probe. A stale Kuma push heartbeat catches
    #    'running but not doing its job', which readiness cannot see.
    ("cloudflare-ddns", "cloudflare-ddns"): "Kuma push heartbeat; no server to probe",
    ("scrutiny", "collector"): "periodic SMART collection; covered by a Kuma monitor",
    (
        "dri-device-plugin",
        "generic-device-plugin",
    ): "registers a kubelet socket; covered by monitor-bridge's check",
}


def _containers():
    for role, _tpl, doc in rendered_docs():
        if doc.get("kind") not in _POD_KINDS:
            continue
        spec = doc["spec"]["template"]["spec"]
        for container in spec.get("containers", []) or []:
            yield role, doc["metadata"]["name"], container


def test_every_container_without_readiness_is_recorded():
    offenders = []
    for role, workload, container in _containers():
        if "readinessProbe" in container:
            continue
        if (role, container["name"]) not in _NO_READINESS:
            offenders.append(f"{role}/{workload} container {container['name']}")

    assert not offenders, (
        "these containers have no readinessProbe, so a Service publishes them the instant they "
        "start.\nAdd a probe, or record the reason in _NO_READINESS in this file:\n  "
        + "\n  ".join(sorted(offenders))
    )


def test_the_record_has_no_stale_entries():
    """A container that gained a probe must leave the list, or the list stops meaning
    anything."""
    without = {
        (role, c["name"]) for role, _w, c in _containers() if "readinessProbe" not in c
    }
    live = {(role, c["name"]) for role, _w, c in _containers()}
    stale = sorted(k for k in _NO_READINESS if k in live and k not in without)
    gone = sorted(k for k in _NO_READINESS if k not in live)

    assert not stale, f"now has a readinessProbe — remove from _NO_READINESS: {stale}"
    assert not gone, f"no such container — remove from _NO_READINESS: {gone}"


def test_every_reason_is_substantive():
    thin = sorted(k for k, v in _NO_READINESS.items() if len(v.strip()) < 20)
    assert not thin, f"reason too thin to be a decision: {thin}"
