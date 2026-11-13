"""slskd's tunnel resolves through CoreDNS, so Soularr can reach Lidarr's Service by name.

With wg0 up, wg-quick points the pod's whole resolv.conf at the tunnel's DNS server. Mullvad's
default resolver knows no cluster names, and Soularr failed every poll with
`Failed to resolve 'lidarr.homelab.svc.cluster.local'` until slskd passed CoreDNS's address.
qbittorrent looks up no cluster name and keeps Mullvad's resolver.
"""

from _helpers import K8S_ROLES, load_yaml
from lib import yaml_fast
from lib.render_context import render_context
from validate.k8s_manifests import (
    SHARED_TPL,
    k8s_entries,
    make_env,
    make_lookup,
    register_ansible_filters,
)

ROLES = K8S_ROLES


def _sidecar_env(role: str) -> dict[str, str]:
    ctx = render_context(
        ROLES / role,
        overrides={"container_item": k8s_entries()[role]},
        strict=False,
    )
    env = make_env([ROLES / role / "templates", SHARED_TPL])
    env.globals["lookup"] = make_lookup(ctx)
    register_ansible_filters(env)
    [deployment] = [
        d
        for d in yaml_fast.safe_load_all(
            env.get_template("deployment.yaml.j2").render(**ctx)
        )
        if d and d.get("kind") == "Deployment"
    ]
    [sidecar] = [
        c
        for c in deployment["spec"]["template"]["spec"]["initContainers"]
        if c["name"] == "wireguard"
    ]
    return {e["name"]: e.get("value") for e in sidecar["env"]}


def test_slskds_tunnel_resolves_through_coredns():
    coredns = load_yaml(ROLES / "coredns" / "defaults" / "main.yml")
    assert _sidecar_env("slskd")["MULLVAD_DNS"] == coredns["coredns_cluster_ip"]


def test_qbittorrents_tunnel_keeps_mullvads_resolver():
    assert "MULLVAD_DNS" not in _sidecar_env("qbittorrent")
