"""Reconciliation: overlay live state onto the declared skeleton.

Takes what ``infra_map.inventory`` read from the repo and what ``infra_map.live``
found running, and produces the single model dict ``infra_map.render`` draws.
Pure functions over both inputs — nothing here touches the cluster or the repo.
"""

from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path

# `infra_map` is a namespace package under `scripts/`, so reaching a sibling by package
# name needs `scripts/` on sys.path: a directly-invoked script gets only its own directory,
# and pyproject's `pythonpath` is a pytest setting.
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

from infra_map.constants import HOST_PLANE, HOST_ROLE, HOSTS, NAMESPACE_OWNERS
from infra_map.inventory import RoleIndex, declared_services


def match_k8s_workloads(
    service: dict,
    workloads: dict[tuple[str, str], dict],
    extra_namespaces: frozenset[str] = frozenset(),
) -> list[dict]:
    """Find the workloads backing a declared k8s service.

    A workload is a Deployment, DaemonSet or StatefulSet. A namespace owner
    (``claude-otel``) claims every workload in its namespace; everything else
    matches its own name plus ``<name>-*`` helpers in the app namespace, and in
    any namespace its own manifests name literally (*extra_namespaces*):
    dri-device-plugin's DaemonSet lives in kube-system because an extended
    resource is a node property, and the inventory has no field saying so.
    """
    name, namespace = service["name"], service.get("namespace")
    namespaces = {namespace, *extra_namespaces}
    matched = []
    if name in NAMESPACE_OWNERS:
        for (ns, wl_name), info in workloads.items():
            if ns == namespace:
                matched.append({"name": wl_name, "namespace": ns, **info})
    else:
        for (ns, wl_name), info in workloads.items():
            if ns not in namespaces:
                continue
            if wl_name == name or wl_name.startswith(f"{name}-"):
                matched.append({"name": wl_name, "namespace": ns, **info})
    return sorted(matched, key=lambda w: w["name"])


def reconcile_docker(
    service: dict, containers: dict[str, dict], roles: RoleIndex
) -> dict:
    """Overlay live Docker state onto one declared service."""
    live = containers.get(service["name"])
    if live is None:
        if service["name"] in roles.batch_roles:
            return {
                **service,
                "status": "job",
                "detail": "one-shot job — leaves no container behind",
            }
        return {**service, "status": "missing", "detail": "no container found"}
    if live["state"] != "running":
        return {
            **service,
            "status": "down",
            "detail": live["status"],
            "image": live["image"],
        }
    status = "degraded" if live["unhealthy"] else "healthy"
    return {
        **service,
        "status": status,
        "detail": live["status"],
        "image": live["image"],
    }


def reconcile_k8s(
    service: dict, workloads: dict[tuple[str, str], dict], roles: RoleIndex
) -> dict:
    """Overlay live workload state onto one declared k8s service."""
    matched = match_k8s_workloads(
        service, workloads, roles.manifest_namespaces.get(service["name"], frozenset())
    )
    if not matched:
        if service["name"] in roles.batch_roles:
            return {
                **service,
                "status": "job",
                "detail": "declares no long-running workload",
            }
        return {**service, "status": "missing", "detail": "no workload found"}
    ready = sum(w["ready"] for w in matched)
    desired = sum(w["desired"] for w in matched)
    detail = f"{ready}/{desired} replicas ready across {len(matched)} workload"
    detail += "s" if len(matched) != 1 else ""
    if ready == 0:
        status = "down"
    elif ready < desired:
        status = "degraded"
    else:
        status = "healthy"
    return {
        **service,
        "status": status,
        "detail": detail,
        "image": matched[0]["image"],
        "replicas": (ready, desired),
        "workloads": matched,
    }


def place_on_nodes(service: dict, pods: list[dict]) -> dict:
    """Record which cluster nodes a k8s service's pods actually landed on.

    Placement is not in ``containers_list`` and not on the Deployment either —
    ``.spec.nodeName`` is a pod field — so it can only come from the live pod
    list. It matters here because several failures in this cluster have been
    placement-dependent rather than workload-dependent.
    """
    nodes = set()
    for workload in service.get("workloads") or []:
        prefix = f"{workload['name']}-"
        for pod in pods:
            if (
                pod["namespace"] == workload["namespace"]
                and pod["name"].startswith(prefix)
                and pod["node"]
            ):
                nodes.add(pod["node"])
    return {**service, "nodes": sorted(nodes)}


def find_extra_containers(
    containers: dict[str, dict], declared_names: set[str], roles: RoleIndex
) -> list[dict]:
    """Classify live containers that have no ``containers_list`` entry.

    Most are companions: a role's compose file defines several containers but
    only the main one earns an inventory entry (the prometheus role also brings
    node-exporter and cadvisor). Those are expected. Anything the repo does not
    account for at all is real drift, and only that gets flagged as undeclared.
    """
    extras = []
    for name, live in sorted(containers.items()):
        if name in declared_names:
            continue
        owner = roles.container_owners.get(name)
        if owner and owner in declared_names:
            status, detail = (
                "companion",
                f"owned by the {owner} role · {live['status']}",
            )
        elif live["state"] == "running":
            status, detail = "undeclared", live["status"]
        else:
            status, detail = "down", live["status"]
        extras.append(
            {
                "name": name,
                "platform": "docker",
                "hostname": None,
                "port": None,
                "authelia": False,
                "networks": [],
                "namespace": None,
                "declared": False,
                "owner": owner,
                "status": status,
                "detail": detail,
                "image": live["image"],
                "replicas": None,
            }
        )
    return extras


def services_on_host(
    host: str, declared_here: list[dict], k8s_services: list[dict]
) -> list[dict]:
    """What a k3s host is actually running, rather than what declares it.

    Every k8s entry in the inventory is declared under daniel-box, so listing a
    host by its own ``containers_list`` renders daniel-server empty while it
    runs half the fleet. Placement is the honest answer to "what is on this
    box", so a service is shown wherever its pods landed — on both hosts when
    it is spread across both. A service with no pods anywhere (a one-shot job,
    or something genuinely missing) stays with the host that declares it, so
    nothing drops off the page.
    """
    placed = [s for s in k8s_services if host in (s.get("nodes") or [])]
    unplaced = [s for s in declared_here if not s.get("nodes")]
    return sorted(placed + unplaced, key=lambda s: s["name"])


def build_model(
    global_vars: dict,
    host_vars: dict[str, dict],
    live: dict[str, dict],
    generated_at: str,
    roles: RoleIndex,
    cluster: dict | None = None,
) -> dict:
    """Merge declared and live state into the structure the renderer consumes.

    *live* maps a host name to ``{"ok": bool, "error": str, "data": ...}``.
    *cluster* is the extra cluster-wide state from :func:`collect_cluster`, or
    None when it was not collected. Pure — every side effect happens before this
    is called, which is what makes the whole reconciliation layer testable.
    """
    cluster = cluster or {
        "ok": False,
        "error": "not collected",
        "nodes": {},
        "pods": [],
        "volumes": None,
        "backup_targets": [],
    }
    pods_by_node: dict[str, int] = {}
    for pod in cluster["pods"]:
        if pod["node"]:
            pods_by_node[pod["node"]] = pods_by_node.get(pod["node"], 0) + 1
    hosts = []
    per_host_services: dict[str, list[dict]] = {}

    for host in HOSTS:
        hv = host_vars.get(host, {})
        declared = declared_services(host, hv, global_vars)
        info = live.get(host, {"ok": False, "error": "not collected", "data": {}})
        platform = HOST_PLANE.get(host) or (
            "k8s" if any(s["platform"] == "k8s" for s in declared) else "docker"
        )

        if info["ok"]:
            declared_names = {s["name"] for s in declared}
            if platform == "k8s":
                services = [
                    place_on_nodes(
                        reconcile_k8s(s, info["data"], roles), cluster["pods"]
                    )
                    for s in declared
                ]
            else:
                services = [reconcile_docker(s, info["data"], roles) for s in declared]
                services += find_extra_containers(info["data"], declared_names, roles)
        else:
            services = declared

        services.sort(key=lambda s: (not s["declared"], s["name"]))
        per_host_services[host] = services

        node = cluster["nodes"].get(host) if platform == "k8s" else None
        hosts.append(
            {
                "name": host,
                "ip": hv.get("server_ip", ""),
                "platform": platform,
                "role": HOST_ROLE.get(host, ""),
                "node": ({**node, "pods": pods_by_node.get(host, 0)} if node else None),
                "reachable": info["ok"],
                "error": info.get("error", ""),
                "declared_count": len(declared),
            }
        )

    # `per_host_services` stays declaration-based — it is the canonical list the
    # table, the totals and the grouping count exactly once. The host panels
    # answer a different question ("what is on this box"), and for the k3s hosts
    # that is placement, not declaration.
    k8s_services = [
        s
        for host, services in per_host_services.items()
        if HOST_PLANE.get(host) == "k8s"
        for s in services
    ]
    for host in hosts:
        declared_here = per_host_services[host["name"]]
        shown = (
            services_on_host(host["name"], declared_here, k8s_services)
            if host["platform"] == "k8s"
            else declared_here
        )
        counts: dict[str, int] = {}
        for service in shown:
            counts[service["status"]] = counts.get(service["status"], 0) + 1
        host["services"] = shown
        host["counts"] = counts
        host["routed_count"] = sum(1 for s in shown if s["hostname"])
        host["authelia_count"] = sum(1 for s in shown if s["authelia"])

    all_services = [s for services in per_host_services.values() for s in services]
    totals = {
        "services": len(all_services),
        "healthy": sum(1 for s in all_services if s["status"] == "healthy"),
        "degraded": sum(1 for s in all_services if s["status"] == "degraded"),
        "down": sum(1 for s in all_services if s["status"] in ("down", "missing")),
        "job": sum(1 for s in all_services if s["status"] == "job"),
        "companion": sum(1 for s in all_services if s["status"] == "companion"),
        "undeclared": sum(1 for s in all_services if s["status"] == "undeclared"),
        "unknown": sum(1 for s in all_services if s["status"] == "unknown"),
    }
    return {
        "generated_at": generated_at,
        "hosts": hosts,
        "services": all_services,
        "totals": totals,
        "domain": global_vars.get("domain", ""),
        "hostname_suffix": global_vars.get("k8s_hostname_suffix", ""),
        "cluster": {
            "ok": cluster["ok"],
            "error": cluster["error"],
            "nodes": [
                {"name": name, "pods": pods_by_node.get(name, 0), **info}
                for name, info in sorted(cluster["nodes"].items())
            ],
            "pod_count": len(cluster["pods"]),
            "volumes": cluster["volumes"],
            "backup_targets": cluster["backup_targets"],
        },
        # Every address the diagram labels an edge with, read from the inventory
        # rather than written into the drawing. Renaming a VIP in group_vars
        # moves the label; it does not leave a stale one behind.
        "endpoints": {
            "ingress_vip": global_vars.get("k3s_metallb_ingress_vip", ""),
            "dns_vip": global_vars.get("dns_k8s_vip", ""),
            "mqtt_vip": global_vars.get("mqtt_k8s_vip", ""),
            "jellyfin_vip": global_vars.get("jellyfin_k8s_lan_ip", ""),
            "public_routes": bool(global_vars.get("k8s_public_route")),
            "longhorn_namespace": global_vars.get(
                "k8s_longhorn_namespace", "longhorn-system"
            ),
        },
    }
