"""`probe.py vip-placement` — does every ETP=Local VIP have a Ready endpoint on an announcing node?

Split out of probe_lib/health.py, which carried three unrelated subcommands (health, readonly-rbac,
vip-placement) in one file.

`externalTrafficPolicy: Local` preserves the client IP (CrowdSec needs it) at the cost of a
hard placement rule: kube-proxy programs the VIP on EVERY node, and a node with no local
endpoint installs a filter-table KUBE-EXTERNAL-SERVICES DROP for it. So when the L2 announcer
and the backing pod sit on different nodes, every forwarded LAN packet dies at the announcer —
silently, with the Service Ready and the pod 1/1.

This has fired twice (2026-08-13 node join, 2026-08-14 cold-boot reschedule; the second took
LAN DNS down). Both times a host-originated probe read green, because kube-proxy gives
node-local clients the cluster-policy path. Nothing else in the fleet notices.

A workload deliberately parked at zero replicas has no endpoint by design, and nothing is
dropped because nothing is meant to be listening. Such a row is reported `scaled-to-zero`
and excluded from the FAIL — a check that is red whenever a service is intentionally off
trains its reader to ignore the red. The replica count is read LIVE (`kubectl get
deployments,statefulsets`) rather than from the rendered manifest: every other read here is
live, and rendering would drag in the role-name resolution `health.py` needs and this does
not.
"""

# `probe_lib` is a namespace package under `scripts/`, so reaching a sibling by package name
# needs `scripts/` on sys.path — a module gets only its importer's path otherwise, and
# pyproject's `pythonpath` is a pytest setting. This has to sit ABOVE the imports below.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

# `core.<name>` for anything the tests monkeypatch — binding those into this module's
# globals with a `from core import ...` would take a snapshot the patch never reaches.
from diagnostics.probe_lib import core

ETP_LOCAL = "Local"


def etp_local_services(services):
    """The LoadBalancer Services whose externalTrafficPolicy is Local, as sortable rows."""
    rows = []
    for svc in services:
        spec = svc.get("spec") or {}
        if spec.get("type") != "LoadBalancer":
            continue
        if spec.get("externalTrafficPolicy") != ETP_LOCAL:
            continue
        meta = svc.get("metadata") or {}
        ingress = ((svc.get("status") or {}).get("loadBalancer") or {}).get(
            "ingress"
        ) or [{}]
        rows.append(
            {
                "namespace": meta.get("namespace", ""),
                "name": meta.get("name", ""),
                "ip": ingress[0].get("ip", "none"),
                "selector": spec.get("selector") or {},
            }
        )
    return sorted(rows, key=lambda r: (r["namespace"], r["name"]))


def announcing_nodes(advertisements, nodes):
    """Node names any L2Advertisement announces from.

    The UNION across every advertisement, deliberately: an advertisement may be scoped to
    particular `ipAddressPools`, and resolving a VIP back to its pool needs the IPAddressPool
    CRs as well. Taking the union is the permissive reading, so this probe under-reports
    rather than inventing a red on a pool it modelled wrong. The failure it exists to catch —
    one pinned announcer, the pod somewhere else — is caught exactly either way.

    An advertisement with no `nodeSelectors` announces from every node.
    """
    names = {(n.get("metadata") or {}).get("name", "") for n in nodes}
    announcers = set()
    for advert in advertisements:
        selectors = (advert.get("spec") or {}).get("nodeSelectors")
        if not selectors:
            return set(names)
        for selector in selectors:
            labels = selector.get("matchLabels") or {}
            for node in nodes:
                node_labels = (node.get("metadata") or {}).get("labels") or {}
                if all(node_labels.get(k) == v for k, v in labels.items()):
                    announcers.add((node.get("metadata") or {}).get("name", ""))
    return announcers


def ready_endpoint_nodes(slices, service_name):
    """Nodes carrying a READY endpoint for `service_name`.

    Ready is the condition that matters: an unready endpoint is not a local endpoint as far as
    kube-proxy's rule is concerned, so counting it would hide the exact outage this catches.
    """
    nodes = set()
    for slice_ in slices:
        labels = (slice_.get("metadata") or {}).get("labels") or {}
        if labels.get("kubernetes.io/service-name") != service_name:
            continue
        for endpoint in slice_.get("endpoints") or []:
            if (endpoint.get("conditions") or {}).get("ready") is True:
                nodes.add(endpoint.get("nodeName") or "")
    return nodes - {""}


def workload_replicas(workloads, namespace, selector):
    """Declared `spec.replicas` of every workload whose pod template matches `selector`.

    A Service's `spec.selector` must be a SUBSET of the workload's pod-template labels. An
    empty or missing selector resolves to nothing rather than to everything: `all()` over an
    empty dict is True, which would match every workload in the namespace.

    `spec.get("replicas")` is compared to 0 explicitly by the caller. A missing field means
    the API default of 1, so a falsy test would read it as zero — the fail-open hole here.
    """
    if not selector:
        return []
    counts = []
    for workload in workloads:
        meta = workload.get("metadata") or {}
        if meta.get("namespace") != namespace:
            continue
        spec = workload.get("spec") or {}
        labels = ((spec.get("template") or {}).get("metadata") or {}).get(
            "labels"
        ) or {}
        if all(labels.get(k) == v for k, v in selector.items()):
            counts.append(spec.get("replicas"))
    return counts


def is_scaled_to_zero(workloads, namespace, selector):
    """True when the Service resolves to at least one workload and EVERY one declares zero.

    Both halves matter. No workload at all stays a FAIL — a Service pointing at nothing is
    the class of bug this probe exists for, and `all()` over an empty list would pass it.
    Two workloads with one at zero and one at 1 is likewise not "off".
    """
    counts = workload_replicas(workloads, namespace, selector)
    return bool(counts) and all(count == 0 for count in counts)


def format_vip_placement(services, slices, announcers, workloads=()):
    """Render the placement verdict.

    Exit 2 INCONCLUSIVE when there is nothing to check — no ETP=Local Service, or no announcing
    node resolved. Either means the read came back empty, and an empty read must never pass:
    the same green-while-blind shape a denial-only RBAC probe has.

    A scaled-to-zero row is NOT that shape and exits 0: the read returned data and resolved a
    positive fact about the row. The OK line counts only the VIPs actually asserted, so the
    number never overstates what was checked, and names the skipped ones separately.
    """
    rows = etp_local_services(services)
    lines = []

    if not rows or not announcers:
        missing = (
            "no ETP=Local LoadBalancer Service"
            if not rows
            else "no announcing node (no L2Advertisement resolved)"
        )
        lines.append(
            f"INCONCLUSIVE: {missing} was read, so there is nothing to assert. An empty "
            "read is not a pass — check RBAC and the namespace before believing this."
        )
        return "\n".join(lines), 2

    stranded = []
    parked = []
    for row in rows:
        on = ready_endpoint_nodes(slices, row["name"])
        local = on & announcers
        if local:
            flag = "ok"
        elif is_scaled_to_zero(workloads, row["namespace"], row["selector"]):
            flag = "scaled-to-zero"
            parked.append(f"{row['namespace']}/{row['name']}")
        else:
            flag = "STRANDED"
            stranded.append(f"{row['namespace']}/{row['name']} ({row['ip']})")
        lines.append(
            f"  {row['namespace']}/{row['name']:<14} {row['ip']:<12} "
            f"endpoints={sorted(on) or '[]'} {flag}"
        )

    lines.append("")
    lines.append(f"announcing nodes: {sorted(announcers)}")
    lines.append("")
    if stranded:
        lines.append(
            "FAIL: these ETP=Local VIPs have no Ready endpoint on an announcing node, so "
            "kube-proxy DROPs every forwarded packet to them at the announcer while the "
            "Service and pods read healthy: " + ", ".join(stranded)
        )
        return "\n".join(lines), 1
    checked = len(rows) - len(parked)
    if checked:
        summary = f"OK: all {checked} ETP=Local VIPs have a Ready endpoint on an announcing node."
    else:
        summary = (
            "OK: no ETP=Local VIP was asserted — every one declares zero replicas."
        )
    if parked:
        summary += (
            f" {len(parked)} declared zero replicas and {'was' if len(parked) == 1 else 'were'}"
            " not checked: " + ", ".join(parked)
        )
    lines.append(summary)
    return "\n".join(lines), 0


def vip_placement_argv():
    """The four reads, in order. Kept as data so `--dry-run` prints exactly what runs."""
    return [
        ["kubectl", "get", "svc", "-A", "-o", "json"],
        ["kubectl", "get", "endpointslices", "-A", "-o", "json"],
        ["kubectl", "get", "l2advertisements.metallb.io", "-A", "-o", "json"],
        ["kubectl", "get", "nodes", "-o", "json"],
        ["kubectl", "get", "deployments,statefulsets", "-A", "-o", "json"],
    ]


def run_vip_placement(ns):
    """Assert every ETP=Local VIP is backed on the node that announces it."""
    calls = vip_placement_argv()
    if getattr(ns, "dry_run", False):
        for argv in calls:
            print(" ".join(argv))
        return 0

    def items(argv):
        data = core.json_or_none(argv)
        return (data or {}).get("items") or []

    services, slices, adverts, nodes, workloads = (items(argv) for argv in calls)
    text, code = format_vip_placement(
        services, slices, announcing_nodes(adverts, nodes), workloads
    )
    print(text)
    return code
