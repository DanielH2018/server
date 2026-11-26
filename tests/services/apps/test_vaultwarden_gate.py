"""Vaultwarden's route and signup gate stay closed to everything but the LAN and WireGuard.

The Bitwarden clients cannot pass Authelia, so the role's `vaultwarden-lan-only` ipAllowList is
the gate on the vault's API. WireGuard peers arrive as the wg-easy pod's IP, so the list admits
the primary node's pod subnet, where wg-easy is pinned. Any wider cluster range would admit the
other node's pods or the whole cluster.
`SIGNUPS_ALLOWED` renders from a bool, because Vaultwarden reads an unparsable value as unset,
and unset means signups open.
"""

import ipaddress

from _k8s_render import host_context, render_role_template, rendered_docs
from lib import yaml_fast

_HOST = "vaultwarden.local."


def admits_more_of_the_cluster(
    sources: list[str], cluster_cidrs: list[str], node_subnet: str
) -> bool:
    """True when a source reaches a pod or service range beyond `node_subnet`."""
    nets = [ipaddress.IPv4Network(c) for c in cluster_cidrs]
    node = ipaddress.IPv4Network(node_subnet)
    for source in (ipaddress.IPv4Network(s, strict=False) for s in sources):
        if any(source.overlaps(n) for n in nets) and not source.subnet_of(node):
            return True
    return False


def _cluster_cidrs() -> list[str]:
    ctx = host_context()
    return [ctx["k3s_pod_cidr"], ctx["k3s_service_cidr"]]


def _primary_node_subnet() -> str:
    """daniel-box's /24 pod subnet, the one holding its cni0 gateway (`k3s_cni0_gateways[0]`)."""
    gateway = host_context()["k3s_cni0_gateways"][0].split("/")[0]
    return str(ipaddress.ip_network(f"{gateway}/24", strict=False))


def _vaultwarden_docs() -> list[dict]:
    docs = [doc for role, _, doc in rendered_docs() if role == "vaultwarden"]
    assert docs, "the vaultwarden role rendered nothing"
    return docs


def _allowlist() -> list[str]:
    (mw,) = [
        d
        for d in _vaultwarden_docs()
        if d["kind"] == "Middleware" and d["metadata"]["name"] == "vaultwarden-lan-only"
    ]
    return mw["spec"]["ipAllowList"]["sourceRange"]


def test_a_wider_cluster_range_is_flagged():
    node = _primary_node_subnet()
    assert admits_more_of_the_cluster(["10.42.0.0/16"], _cluster_cidrs(), node)
    assert admits_more_of_the_cluster(["10.42.1.0/24"], _cluster_cidrs(), node)


def test_the_rendered_allowlist_admits_only_the_primary_nodes_pods_is_clean():
    sources = _allowlist()
    node = _primary_node_subnet()
    assert node in sources, f"WireGuard peers' source {node} is not admitted: {sources}"
    assert not admits_more_of_the_cluster(sources, _cluster_cidrs(), node), sources


def test_every_vault_router_but_the_probes_carries_the_allowlist():
    routes = [
        r
        for d in _vaultwarden_docs()
        if d["kind"] == "IngressRoute"
        for r in d["spec"]["routes"]
        if _HOST in r["match"]
    ]
    assert routes, "no IngressRoute matches the vaultwarden .local. host"
    for route in routes:
        names = {m["name"] for m in route.get("middlewares", [])}
        if "ClientIP(" in route["match"]:
            assert "Path(`/`)" in route["match"], route["match"]
            assert "PathPrefix(" not in route["match"], route["match"]
            continue
        assert "vaultwarden-lan-only" in names, route["match"]


def _signups(value) -> str:
    text = render_role_template(
        "vaultwarden",
        "deployment.yaml.j2",
        {"vaultwarden_k8s_signups_allowed": value},
    )
    (doc,) = [d for d in yaml_fast.safe_load_all(text) if d]
    env = doc["spec"]["template"]["spec"]["containers"][0]["env"]
    return {e["name"]: e["value"] for e in env}["SIGNUPS_ALLOWED"]


def test_signups_render_open_only_from_a_true_bool():
    assert _signups(False) == "false"
    assert _signups("not-a-bool") == "false"
    assert _signups(True) == "true"
