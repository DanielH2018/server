"""Every Deployment's update strategy is a decision on the record, not a copied default.

`strategy: Recreate` stops the old pod before starting the new one, so each deploy of that
workload has a hard downtime gap. That is the right call for most of this fleet — sqlite
databases, single-writer TSDBs, a Zigbee radio that accepts one client — and each template
says so in a comment. A comment is not a gate: the next service added inherits whatever its
author copied, and nothing notices.

Two guards:

  * every Deployment is either RollingUpdate or an allowlisted Recreate with a reason. A new
    Recreate fails until it is added below, which forces the author to state why.
  * every ROLLING Deployment reachable through a Service has a readinessProbe. Without one a
    pod is Ready the instant its container starts, so the Service routes to it before it can
    serve — which turns a rolling update into a short outage while looking like the opposite.
    Recreate workloads are exempt: their gap is the point, and a probe does not close it.

Rendering goes through validate_k8s_manifests' own machinery (via _k8s_render), so this
cannot drift from what that validator considers a renderable manifest.
"""

from __future__ import annotations

from _k8s_render import rendered_docs

# (role, deployment name) -> why this workload must stop before it starts.
# Adding an entry is a deliberate act: it means a deploy of this service has a downtime gap.
_RECREATE = {
    # ── sqlite / embedded DB / local config store ──
    (
        "sonarr",
        "sonarr",
    ): "sqlite config DB; two instances would double-run import jobs",
    (
        "radarr",
        "radarr",
    ): "sqlite config DB; two instances would double-run import jobs",
    ("bazarr", "bazarr"): "sqlite config DB",
    (
        "prowlarr",
        "prowlarr",
    ): "sqlite config DB; two instances would double-run RSS syncs",
    ("jellyfin", "jellyfin"): "sqlite library DB",
    ("freshrss", "freshrss"): "sqlite DB plus file-based PHP sessions",
    ("healthchecks", "healthchecks"): "sqlite DB on an RWO Longhorn volume",
    ("speedtest", "speedtest"): "sqlite results DB",
    ("uptime-kuma", "uptime-kuma"): "sqlite DB on two RWO PVCs",
    ("n8n", "n8n"): "sqlite DB under /home/node/.n8n",
    (
        "home-assistant",
        "home-assistant",
    ): "sqlite recorder plus singleton device connections",
    ("authelia", "authelia"): "sqlite storage and an in-memory session provider",
    ("crowdsec", "crowdsec"): "sqlite LAPI DB on an RWO volume",
    ("claude-otel", "grafana"): "sqlite DB on a ReadWriteOnce PVC",
    ("scrutiny", "scrutiny-web"): "local config store",
    ("karakeep", "karakeep"): "sqlite DB on the data PVC",
    # ── single-writer TSDB / index ──
    ("claude-otel", "prometheus"): "TSDB holds an exclusive lock on its data directory",
    ("claude-otel", "loki"): "single-writer index on a filesystem store",
    ("claude-otel", "tempo"): "single-writer trace store",
    ("loki-homelab", "loki-homelab"): "single-writer index on a filesystem store",
    (
        "scrutiny",
        "scrutiny-influxdb",
    ): "influxdb holds an exclusive lock on its data directory",
    ("karakeep", "karakeep-meilisearch"): "meilisearch holds an exclusive LMDB lock",
    # ── single-writer datastore / world save ──
    ("livesync", "livesync"): "couchdb single-writer data directory",
    ("mosquitto", "mosquitto"): "single-writer persistence DB",
    ("valheim", "valheim"): "two servers writing one world save is worse than the gap",
    (
        "terraria",
        "terraria",
    ): "two servers writing one world save is worse than the gap",
    ("tdarr", "tdarr"): "single-writer server DB",
    # ── node-exclusive hardware or network namespace ──
    ("nut", "nut"): "raw USB device plus a loopback hostPort, both node-exclusive",
    ("zigbee2mqtt", "zigbee2mqtt"): "the SLZB coordinator accepts exactly one client",
    ("wg-easy", "wg-easy"): "owns a wireguard interface",
    ("qbittorrent", "qbittorrent"): "VPN killswitch iptables live in the pod netns",
    ("registry", "registry"): "hostPort is node-exclusive",
    # ── in-process state ──
    (
        "monitor-bridge",
        "monitor-bridge",
    ): "grace-cycle and hysteresis streaks are in-process",
    ("autofix-bridge", "autofix-bridge"): "candidate-grace streaks are in-process",
    ("janitorr", "janitorr"): "two instances would both walk the library",
    ("valheim-stats", "valheim-stats"): "in-process aggregation state on an RWO PVC",
    ("terraria-stats", "terraria-stats"): "in-process aggregation state on an RWO PVC",
    # ── ingress / DNS topology ──
    ("traefik", "traefik"): (
        "acme.json is RWO and two Traefiks racing to write it corrupt the account "
        "registration; externalTrafficPolicy: Local plus a MetalLB VIP means a second pod "
        "does not receive traffic anyway"
    ),
    (
        "pihole",
        "pihole",
    ): "owns the LAN DNS VIP; redundancy is a second instance, not a replica",
    (
        "pihole",
        "pihole-2",
    ): "owns the LAN DNS VIP; redundancy is a second instance, not a replica",
    # ── workspace ──
    ("code-server", "code-server"): "live workspace on an RWO PVC",
}


def _deployments():
    for role, tpl, doc in rendered_docs():
        if doc.get("kind") == "Deployment":
            yield role, tpl, doc


def _services():
    """(role, selector dict) for every Service that selects on labels."""
    for role, _tpl, doc in rendered_docs():
        if doc.get("kind") == "Service":
            selector = doc.get("spec", {}).get("selector")
            if selector:
                yield role, selector


def test_every_recreate_deployment_is_allowlisted_with_a_reason():
    offenders = []
    for role, tpl, doc in _deployments():
        strategy = doc.get("spec", {}).get("strategy", {}).get("type")
        if strategy != "Recreate":
            continue
        key = (role, doc["metadata"]["name"])
        if key not in _RECREATE:
            offenders.append(f"{role}/{tpl} ({doc['metadata']['name']})")

    assert not offenders, (
        "strategy: Recreate means every deploy of these workloads has a downtime gap.\n"
        "Add each to _RECREATE in this file with the reason it cannot roll, or convert it:\n  "
        + "\n  ".join(sorted(offenders))
    )


def test_allowlist_has_no_stale_entries():
    """An allowlisted workload that converted must leave the list, or the list stops meaning
    anything."""
    live = {(role, doc["metadata"]["name"]) for role, _tpl, doc in _deployments()}
    recreate = {
        (role, doc["metadata"]["name"])
        for role, _tpl, doc in _deployments()
        if doc.get("spec", {}).get("strategy", {}).get("type") == "Recreate"
    }
    stale = sorted(k for k in _RECREATE if k in live and k not in recreate)
    missing = sorted(k for k in _RECREATE if k not in live)

    assert not stale, f"no longer Recreate — remove from _RECREATE: {stale}"
    assert not missing, f"no such Deployment — remove from _RECREATE: {missing}"


def test_every_reason_is_substantive():
    thin = sorted(k for k, v in _RECREATE.items() if len(v.strip()) < 15)
    assert not thin, f"reason is too thin to be a decision: {thin}"


def test_rolling_deployments_behind_a_service_have_a_readiness_probe():
    """A rolling pod with no readinessProbe is Ready before it can serve, so the Service
    routes to it and the rollout drops requests — the exact failure this program exists to
    prevent."""
    selectors = list(_services())
    offenders = []

    for role, tpl, doc in _deployments():
        spec = doc.get("spec", {})
        if spec.get("strategy", {}).get("type") == "Recreate":
            continue
        labels = spec.get("template", {}).get("metadata", {}).get("labels", {}) or {}
        behind_service = any(
            svc_role == role and all(labels.get(k) == v for k, v in sel.items())
            for svc_role, sel in selectors
        )
        if not behind_service:
            continue
        containers = (
            spec.get("template", {}).get("spec", {}).get("containers", []) or []
        )
        if not any(c.get("readinessProbe") for c in containers):
            offenders.append(f"{role}/{tpl} ({doc['metadata']['name']})")

    assert not offenders, (
        "rolling Deployment behind a Service with no readinessProbe on any container:\n  "
        + "\n  ".join(sorted(offenders))
    )
