#!/usr/bin/env python3
"""Tests for gen_hosts_block — the /etc/hosts generator for homelab `.local` names.

Two classes of bug matter here, and both are silent: a name that resolves to the *wrong* host
(the k3s VIP split), and a name that is *missing* entirely. Neither produces an error at
generation time — the first surfaces as a 404 from the wrong Traefik, the second as "Server
not found" in a browser, and both look like homelab faults rather than a stale hosts file.

Nothing here decrypts: every test either passes an explicit domain or builds its own scalars,
so this suite runs in CI without an age key.

Run: uv run pytest scripts/dev/tests/test_gen_hosts_block.py
"""

import pytest

import gen_hosts_block as g

SCALARS = {
    "domain": "example.com",
    "k8s_hostname_suffix": "-k8s",
    "k3s_metallb_ingress_vip": "10.0.0.240",
    "daniel-server:server_ip": "10.0.0.161",
    "daniel-box:server_ip": "10.0.0.215",
    "daniel-pi:server_ip": "10.0.0.139",
    "daniel-server:containers_list": [],
    "daniel-box:containers_list": [],
}


def scalars(**overrides):
    return {**SCALARS, **overrides}


def test_resolve_substitutes_a_known_var():
    assert g.resolve("auth{{ k8s_hostname_suffix }}", SCALARS) == "auth-k8s"


def test_resolve_tolerates_missing_whitespace():
    assert g.resolve("auth{{k8s_hostname_suffix}}", SCALARS) == "auth-k8s"


def test_resolve_raises_on_unknown_var():
    """Silently leaving `{{ foo }}` in place would emit a hosts entry for a bogus name."""
    with pytest.raises(KeyError):
        g.resolve("auth{{ not_a_var }}", SCALARS)


def test_docker_service_maps_to_the_server_ip():
    s = scalars(
        **{"daniel-server:containers_list": [{"name": "jellyfin", "port": 8096}]}
    )
    assert (
        "10.0.0.161",
        "jellyfin.local.example.com",
    ) in g.entries(s)


def test_k8s_service_maps_to_the_vip_not_the_server():
    """The bug that started this: k3s services live on a different host entirely."""
    s = scalars(
        **{
            "daniel-box:containers_list": [
                {
                    "name": "bento-pdf",
                    "platform": "k8s",
                    "hostname": "bento-pdf",
                    "port": 8080,
                }
            ]
        }
    )
    assert ("10.0.0.240", "bento-pdf.local.example.com") in g.entries(s)


def test_service_without_a_port_is_skipped():
    s = scalars(**{"daniel-server:containers_list": [{"name": "autoheal"}]})
    assert not [fqdn for _, fqdn in g.entries(s) if fqdn.startswith("autoheal")]


def test_k8s_service_without_a_hostname_is_skipped():
    """The cluster's own Traefik has no route — same skip the dnsmasq template makes.

    Asserted against the VIP specifically, because Docker's Traefik dashboard does own a real
    `traefik.local.<domain>` on daniel-server, which the template scrape picks up.
    """
    s = scalars(
        **{
            "daniel-box:containers_list": [
                {"name": "traefik", "platform": "k8s", "port": 8080}
            ]
        }
    )
    assert ("10.0.0.240", "traefik.local.example.com") not in g.entries(s)


def test_hostname_overrides_the_service_name():
    s = scalars(
        **{
            "daniel-server:containers_list": [
                {"name": "littlelink", "hostname": "www", "port": 80}
            ]
        }
    )
    fqdns = [fqdn for _, fqdn in g.entries(s)]
    assert "www.local.example.com" in fqdns
    assert "littlelink.local.example.com" not in fqdns


def test_pi_gets_its_lan_host_record():
    assert ("10.0.0.139", "daniel-pi.lan") in g.entries(scalars())


def test_entries_are_deduplicated():
    """A service can appear in containers_list AND carry a literal Host rule."""
    s = scalars(**{"daniel-server:containers_list": [{"name": "n8n", "port": 5678}]})
    result = g.entries(s)
    assert len(result) == len(set(result))


@pytest.fixture(scope="module")
def real():
    return g.entries(g.load_vars(domain="daniel-hunter.com"))


def test_auth_portal_is_present(real):
    """authelia moved to k8s at E7 (2026-08-13) and now carries `port`, so it needs no
    hand-written-route scrape — the main containers_list loop finds it like any other k8s
    service. Since the `-k8s` suffix retired (2026-08-15) the LAN login target is the plain
    `auth.local.<domain>` — the `.local.` session cookie's `authelia_url`. Without it every
    service resolves but no LAN login can complete, since each protected route redirects
    here.
    """
    assert ("10.0.0.240", "auth.local.daniel-hunter.com") in real


def test_no_k8s_suffixed_names_remain(real):
    """The transitional `-k8s` suffix retired 2026-08-15 with the last Docker twin.

    A name reappearing here means an inventory `hostname:` grew the suffix back, which would emit a
    hosts entry for a hostname no IngressRoute serves.
    """
    assert not [fqdn for _, fqdn in real if "-k8s.local." in fqdn]


def test_cluster_names_are_on_the_vip(real):
    cluster = {
        fqdn: ip for ip, fqdn in real if fqdn.endswith(".local.daniel-hunter.com")
    }
    assert cluster, "expected at least one cluster name"
    assert set(cluster.values()) == {"10.0.0.240"}


def test_no_name_appears_twice(real):
    fqdns = [fqdn for _, fqdn in real]
    assert len(fqdns) == len(set(fqdns))


def test_every_entry_is_a_plausible_hosts_line(real):
    for ip, fqdn in real:
        assert ip.count(".") == 3, f"{ip} is not an IPv4 address"
        assert "{{" not in fqdn, f"{fqdn} carries an unresolved template"
        assert fqdn.endswith((".daniel-hunter.com", ".lan")), fqdn
