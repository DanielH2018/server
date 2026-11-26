"""Vaultwarden's route and signup gate stay closed to everything but the LAN.

The Bitwarden clients cannot pass Authelia, so the role's `vaultwarden-lan-only` ipAllowList is
the gate on the vault's API. A pod or service CIDR in it would admit every pod in the cluster.
`SIGNUPS_ALLOWED` renders from a bool, because Vaultwarden reads an unparsable value as unset,
and unset means signups open.
"""

import ipaddress

from _k8s_render import host_context, render_role_template, rendered_docs
from lib import yaml_fast

_HOST = "vaultwarden.local."


def admits_a_cluster_range(sources: list[str], cluster_cidrs: list[str]) -> bool:
    """True when any allowlist source overlaps a pod or service range."""
    nets = [ipaddress.ip_network(c) for c in cluster_cidrs]
    return any(
        ipaddress.ip_network(s, strict=False).overlaps(n) for s in sources for n in nets
    )


def _cluster_cidrs() -> list[str]:
    ctx = host_context()
    return [ctx["k3s_pod_cidr"], ctx["k3s_service_cidr"]]


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


def test_a_pod_cidr_source_is_flagged():
    assert admits_a_cluster_range(["10.0.0.0/24", "10.42.0.0/16"], _cluster_cidrs())


def test_the_rendered_allowlist_admits_no_cluster_range_is_clean():
    sources = _allowlist()
    assert sources, "vaultwarden-lan-only admits nothing"
    assert not admits_a_cluster_range(sources, _cluster_cidrs()), sources


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
