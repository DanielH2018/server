"""`probe.py vip-placement` — does every ETP=Local VIP have a Ready endpoint on an announcing node?

Split out of probe_health.py, which carried three unrelated subcommands (health, readonly-rbac,
vip-placement) in one file.

`externalTrafficPolicy: Local` preserves the client IP (CrowdSec needs it) at the cost of a
hard placement rule: kube-proxy programs the VIP on EVERY node, and a node with no local
endpoint installs a filter-table KUBE-EXTERNAL-SERVICES DROP for it. So when the L2 announcer
and the backing pod sit on different nodes, every forwarded LAN packet dies at the announcer —
silently, with the Service Ready and the pod 1/1.

This has fired twice (2026-08-13 node join, 2026-08-14 cold-boot reschedule; the second took
LAN DNS down). Both times a host-originated probe read green, because kube-proxy gives
node-local clients the cluster-policy path. Nothing else in the fleet notices.
"""

# `core.<name>` for anything the tests monkeypatch — binding those into this module's
# globals with a `from probe_core import ...` would take a snapshot the patch never reaches.
import probe_core as core

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


def format_vip_placement(services, slices, announcers):
    """Render the placement verdict.

    Exit 2 INCONCLUSIVE when there is nothing to check — no ETP=Local Service, or no announcing
    node resolved. Either means the read came back empty, and an empty read must never pass:
    the same green-while-blind shape a denial-only RBAC probe has.
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
    for row in rows:
        on = ready_endpoint_nodes(slices, row["name"])
        local = on & announcers
        flag = "ok" if local else "STRANDED"
        lines.append(
            f"  {row['namespace']}/{row['name']:<14} {row['ip']:<12} "
            f"endpoints={sorted(on) or '[]'} {flag}"
        )
        if not local:
            stranded.append(f"{row['namespace']}/{row['name']} ({row['ip']})")

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
    lines.append(
        f"OK: all {len(rows)} ETP=Local VIPs have a Ready endpoint on an announcing node."
    )
    return "\n".join(lines), 0


def vip_placement_argv():
    """The four reads, in order. Kept as data so `--dry-run` prints exactly what runs."""
    return [
        ["kubectl", "get", "svc", "-A", "-o", "json"],
        ["kubectl", "get", "endpointslices", "-A", "-o", "json"],
        ["kubectl", "get", "l2advertisements.metallb.io", "-A", "-o", "json"],
        ["kubectl", "get", "nodes", "-o", "json"],
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

    services, slices, adverts, nodes = (items(argv) for argv in calls)
    text, code = format_vip_placement(
        services, slices, announcing_nodes(adverts, nodes)
    )
    print(text)
    return code
