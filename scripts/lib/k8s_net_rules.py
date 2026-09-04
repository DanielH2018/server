#!/usr/bin/env python3
"""The two semantic rules on rendered manifests that no schema can make.

Split out of ``scripts/validate/k8s_manifests.py`` on 2026-09-04; that module re-exports every
name here, so an existing importer keeps working.

Both bug classes fail silently the same way: the object applies cleanly and then matches
nothing. An https IngressRoute with no ``tls:`` is never a TLS router, and a NetworkPolicy
holding a Service's published port fences no traffic. Pure functions over parsed documents —
this module reads no files and imports nothing from the repo.
"""

__all__ = [
    "HTTPS_ENTRYPOINT",
    "https_route_without_tls",
    "netpol_port_mismatches",
    "service_port_translations",
    "workload_container_ports",
]

# Traefik's TLS entrypoint in this cluster. `websecure` is upstream's conventional name and is
# not used here — see the traefik role's static-config.yaml.j2.
HTTPS_ENTRYPOINT = "https"


def https_route_without_tls(doc: dict) -> str | None:
    """An IngressRoute on the https entrypoint that declares no `spec.tls`.

    Such a route is not a TLS router at all: Traefik never considers it a candidate for an
    HTTPS request, so the route silently never matches while reading as correctly configured.
    ansible/templates/ingressroute.yml.j2 already says the `tls:` key is never conditional and
    records that omitting it cost a week — this makes the rule something a machine enforces
    rather than a comment a hand-written route can miss.

    IngressRoute is a CRD, so `schema_error` returns NO_SCHEMA for it and nothing else here
    looks at one. `certResolver` may legitimately be absent (an empty resolver means Traefik
    serves its own self-signed certificate); the `tls` key itself may not.
    """
    if doc.get("kind") != "IngressRoute":
        return None
    spec = doc.get("spec")
    if not isinstance(spec, dict):
        return None
    entrypoints = spec.get("entryPoints")
    if not isinstance(entrypoints, list) or HTTPS_ENTRYPOINT not in entrypoints:
        return None
    if "tls" in spec:
        return None
    name = (doc.get("metadata") or {}).get("name", "<unnamed>")
    return (
        f"IngressRoute {name} lists the {HTTPS_ENTRYPOINT} entrypoint but declares no "
        "spec.tls, so Traefik never treats it as a TLS router and the route never matches. "
        "Add `tls:` — certResolver stays optional, the key does not."
    )


def workload_container_ports(docs) -> dict[tuple, set[int]]:
    """Pod-label-set -> the container ports its workloads actually listen on.

    Keyed by the frozen label set on `spec.template.metadata.labels`, which is what a
    NetworkPolicy's podSelector matches against.
    """
    ports: dict[tuple, set[int]] = {}
    for doc in docs:
        if not isinstance(doc, dict) or doc.get("kind") not in (
            "Deployment",
            "DaemonSet",
            "StatefulSet",
        ):
            continue
        template = ((doc.get("spec") or {}).get("template")) or {}
        labels = (template.get("metadata") or {}).get("labels")
        if not isinstance(labels, dict):
            continue
        key = tuple(sorted((str(k), str(v)) for k, v in labels.items()))
        found = ports.setdefault(key, set())
        for container in (template.get("spec") or {}).get("containers") or []:
            for port in (container or {}).get("ports") or []:
                number = (port or {}).get("containerPort")
                if isinstance(number, int):
                    found.add(number)
    return ports


def _selected_ports(
    selector: dict, by_labels: dict[tuple, set[int]]
) -> set[int] | None:
    """The container ports of every workload a podSelector matches, or None when it matches none.

    None and an empty set mean different things: no matching workload is no evidence, while a
    matching workload with no declared containerPort is a workload we cannot check.
    """
    wanted = selector.get("matchLabels")
    if not isinstance(wanted, dict) or not wanted:
        return None
    matched = None
    for labels, ports in by_labels.items():
        as_dict = dict(labels)
        if all(as_dict.get(str(k)) == str(v) for k, v in wanted.items()):
            matched = (matched or set()) | ports
    return matched


def service_port_translations(docs) -> dict[int, set[int]]:
    """Service port -> the targetPorts it forwards to, for every Service that translates.

    Only ports where `port` and `targetPort` differ are recorded. A Service that maps 1:1
    cannot produce the confusion this rule exists to catch.
    """
    translations: dict[int, set[int]] = {}
    for doc in docs:
        if not isinstance(doc, dict) or doc.get("kind") != "Service":
            continue
        for port in ((doc.get("spec") or {}).get("ports")) or []:
            published, target = (port or {}).get("port"), (port or {}).get("targetPort")
            if (
                isinstance(published, int)
                and isinstance(target, int)
                and published != target
            ):
                translations.setdefault(published, set()).add(target)
    return translations


def netpol_port_mismatches(
    doc: dict, by_labels: dict[tuple, set[int]], translations: dict[int, set[int]]
) -> list[str]:
    """NetworkPolicy ports that hold a Service's published port instead of the pod's.

    A NetworkPolicy port is the CONTAINER's port, never the Service's. Traefik is the standing
    example: its Service publishes 80 and 443 while the pod listens on 8000 and 8443, so a
    policy written against the Service's numbers fences nothing — silently, because a policy
    that matches no traffic looks exactly like one that matches all of it until something is
    refused.

    THREE conditions, all required, because the obvious one-condition version is wrong. Flagging
    every policy port that is not a declared containerPort produces a false positive on the
    first real workload: a containerPort declaration is informational, and traefik's own
    Prometheus metrics listener answers on 8080 while declaring nothing. So the port must also
    be a port some Service publishes, and that Service must translate it to a DIFFERENT
    targetPort — which is exactly the confusion, and nothing else.
    """
    if doc.get("kind") != "NetworkPolicy":
        return []
    spec = doc.get("spec")
    if not isinstance(spec, dict):
        return []
    selector = spec.get("podSelector")
    if not isinstance(selector, dict):
        return []
    declared = _selected_ports(selector, by_labels)
    if not declared:
        # No matching workload, or one that declares no containerPort: no evidence either way.
        return []
    name = (doc.get("metadata") or {}).get("name", "<unnamed>")
    problems = []
    for rule in spec.get("ingress") or []:
        for port in (rule or {}).get("ports") or []:
            number = (port or {}).get("port")
            if not isinstance(number, int) or number in declared:
                continue
            targets = translations.get(number)
            if not targets:
                continue
            problems.append(
                f"NetworkPolicy {name} admits port {number}, which is a Service's published "
                f"port forwarding to {sorted(targets)}. A NetworkPolicy port is the "
                f"container's port — the pods this policy selects listen on {sorted(declared)}."
            )
    return problems
