"""Pi-hole forwards the LAN reverse zone to the router, and nothing else.

Without this, Pi-hole answers PTR only for the handful of `host-record=` names in its dnsmasq
config, so every LAN device holding a DHCP lease reads as a bare IP in the query log. The
router is the DHCP server and knows those names, so the reverse zone is forwarded there.

Two properties, because either one alone passes while the change is wrong:

* the forward exists and names the zone `lan_subnet` implies — a typed zone that stopped
  matching the subnet would forward queries nobody asks, and read as a working config;
* the forward is reverse-only. `rev-server=` would additionally forward `.lan` to the router,
  taking the node names the `host-record=` lines answer for out of Pi-hole's hands. A test that
  only asserted the PTR half would pass with that regression in place.
"""

import ipaddress

from _helpers import GROUP_VARS, load_yaml
from _k8s_render import rendered_texts


def _dnsmasq_conf() -> str:
    """The rendered dnsmasq config as it is embedded in Pi-hole's ConfigMap.

    Read as text: dnsmasq directives are lines inside a YAML string value, so parsing the
    document confirms the key exists and says nothing about the directives in it.
    """
    texts = [
        text
        for role, tpl, text in rendered_texts()
        if role == "pihole" and tpl.endswith("configmap.yaml.j2")
    ]
    assert texts, "pihole's configmap template rendered nothing — the census is empty"
    return "\n".join(texts)


def _server_directives(conf: str) -> list[str]:
    return [
        line.strip()[len("server=") :]
        for line in conf.splitlines()
        if line.strip().startswith("server=")
    ]


def _expected_reverse_zone() -> str:
    """The in-addr.arpa zone for `lan_subnet`, computed independently of the template.

    ipaddress derives it from the network object rather than by slicing octets, so this is a
    real oracle for the template's own slicing and not a copy of it.
    """
    group_vars = load_yaml(GROUP_VARS / "all.yml")
    net = ipaddress.ip_network(group_vars["lan_subnet"])
    assert net.prefixlen % 8 == 0, (
        "lan_subnet is no longer a whole-octet prefix — the template's octet slicing cannot "
        "express its reverse zone, and RFC 2317 delegation would be needed instead"
    )
    kept = net.prefixlen // 8
    octets = str(net.network_address).split(".")
    return ".".join(reversed(octets[:kept])) + ".in-addr.arpa"


def test_lan_reverse_zone_is_forwarded_to_the_router() -> None:
    group_vars = load_yaml(GROUP_VARS / "all.yml")
    expected = f"/{_expected_reverse_zone()}/{group_vars['lan_router_ip']}"
    assert expected in _server_directives(_dnsmasq_conf()), (
        f"pihole's dnsmasq config has no `server={expected}` line, so PTR lookups for LAN "
        "clients reach the root servers instead of the router that leased them"
    )


def test_no_forward_hands_the_lan_tld_to_the_router() -> None:
    """The rejecting half: `rev-server=` (or a bare `.lan` forward) must not appear.

    `rev-server=<cidr>,<router>,lan` expands to a `.lan` forward as well as the PTR one, which
    would send `daniel-box.lan` and its siblings to the router — names only Pi-hole's own
    `host-record=` lines answer for.
    """
    conf = _dnsmasq_conf()
    # Directives only. The comment above the `server=` line names `rev-server=` to say why it
    # was not used, and a whole-file substring check would fire on that explanation.
    directives = "\n".join(
        line for line in conf.splitlines() if not line.strip().startswith("#")
    )
    assert "rev-server" not in directives, (
        "pihole's dnsmasq config uses `rev-server=`, which also forwards a whole TLD to the "
        "router and is a Pi-hole-only directive its config parser may reject at startup"
    )
    forwarded_domains = [
        d.split("/")[1] for d in _server_directives(conf) if d.startswith("/")
    ]
    assert all(d.endswith(".in-addr.arpa") for d in forwarded_domains), (
        f"pihole forwards a non-reverse domain to an upstream: {forwarded_domains}"
    )


def test_local_host_records_still_exist_beside_the_forward() -> None:
    """Non-vacuity: the forward must sit in the file that carries the node host-records.

    If the two ever separate — a template split, a moved include — this file would keep
    passing against a config the node names no longer live in.
    """
    conf = _dnsmasq_conf()
    for name in ("daniel-box", "daniel-server", "daniel-pi.lan"):
        assert "host-record=" in conf and name in conf, (
            f"{name} is no longer in the dnsmasq config the reverse forward was added to"
        )
