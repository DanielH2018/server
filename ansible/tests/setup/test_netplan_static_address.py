"""Guard: each k3s node pins server_ip statically on the link that carries it, with DHCPv4 off.

WHY THIS EXISTS. k3s binds server_ip itself, so a node whose address comes from DHCP cannot
start k3s while the gateway's DHCP server is down. A WAN outage spanning the 2026-10-05 reboot
kept daniel-box's k3s crash-looping on `bind: cannot assign requested address` for ~70h
(#3882). The rendered file must carry the inventory's address, not merely an address, and
must switch DHCPv4 off rather than run it alongside (the template says why).
"""

import functools

from lib.repo_paths import ALL_VARS, HOST_VARS
from lib import yaml_fast
from _setup_render import render_setup_text
from renderer.renderer_lib import host as host_mode

_TEMPLATE = "netplan-static-address.yaml.j2"
# Both k3s nodes; each one's package ships its pin.
_NODES = ("daniel-box", "daniel-server")


def static_address_problems(
    text: str, link: str, server_ip: str, router: str
) -> list[str]:
    """What keeps the rendered netplan file from pinning `server_ip` on `link`."""
    iface = (
        (yaml_fast.safe_load(text) or {})
        .get("network", {})
        .get("ethernets", {})
        .get(link)
    )
    if iface is None:
        return [f"no ethernets entry for {link}"]
    problems = []
    if iface.get("dhcp4") is not False:
        problems.append("dhcp4 is not false")
    if iface.get("ignore-carrier") is not True:
        problems.append("ignore-carrier is not true")
    if [a.split("/")[0] for a in iface.get("addresses", [])] != [server_ip]:
        problems.append(f"addresses are {iface.get('addresses')}, not {server_ip}")
    if not any(
        r.get("to") == "default" and r.get("via") == router
        for r in iface.get("routes", [])
    ):
        problems.append(f"no default route via {router}")
    # netplan emits the `[DHCP] RouteMetric` that networkd also gives RA routes only with a DHCP
    # client on, so the metric without `dhcp6: true` renders nothing (#4561).
    if (
        iface.get("dhcp6") is not True
        or (iface.get("dhcp6-overrides") or {}).get("route-metric") != 100
    ):
        problems.append("the IPv6 RA route is not pinned at metric 100")
    return problems


def _host_vars(host: str) -> dict:
    return yaml_fast.safe_load((HOST_VARS / f"{host}.yml").read_text())


def test_each_node_renders_its_own_address_pinned() -> None:
    router = yaml_fast.safe_load(ALL_VARS.read_text())["lan_router_ip"]
    for host in _NODES:
        hv = _host_vars(host)
        link = hv.get("k3s_node_static_link")
        assert link, f"{host} sets no k3s_node_static_link, so its k3s still needs DHCP"
        text = render_setup_text(
            "k3s",
            _TEMPLATE,
            {"server_ip": hv["server_ip"], "k3s_node_static_link": link},
        )
        assert static_address_problems(text, link, hv["server_ip"], router) == [], host


_PINNED = """
network:
  ethernets:
    eno1:
      dhcp4: false
      ignore-carrier: true
      addresses: ["10.0.0.215/24"]
      routes: [{to: default, via: 10.0.0.1, metric: 100}]
      dhcp6: true
      dhcp6-overrides: {route-metric: 100}
"""


def test_a_pinned_address_is_clean() -> None:
    assert static_address_problems(_PINNED, "eno1", "10.0.0.215", "10.0.0.1") == []


def test_an_address_left_on_dhcp_is_flagged() -> None:
    doc = _PINNED.replace("dhcp4: false", "dhcp4: true")
    assert static_address_problems(doc, "eno1", "10.0.0.215", "10.0.0.1") == [
        "dhcp4 is not false"
    ]


def test_an_ra_route_left_at_the_networkd_default_is_flagged() -> None:
    # A wrong metric, no metric at all, and a metric netplan renders nothing for without dhcp6.
    for doc in (
        _PINNED.replace("{route-metric: 100}", "{route-metric: 1024}"),
        _PINNED.replace("      dhcp6-overrides: {route-metric: 100}\n", ""),
        _PINNED.replace("      dhcp6: true\n", ""),
    ):
        assert static_address_problems(doc, "eno1", "10.0.0.215", "10.0.0.1") == [
            "the IPv6 RA route is not pinned at metric 100"
        ], doc


def test_an_address_other_than_server_ip_is_flagged() -> None:
    problems = static_address_problems(_PINNED, "eno1", "10.0.0.216", "10.0.0.1")
    assert problems == ["addresses are ['10.0.0.215/24'], not 10.0.0.216"]


_LINKDOWN_FILE = "/etc/sysctl.d/90-homelab-static-link.conf"


def linkdown_sysctl_problems(text: str, link: str) -> list[str]:
    """What keeps the pinned link's dead default routes from losing to a fallback (#4510, #4561).

    `ignore-carrier` keeps the pinned routes installed with no carrier, and the kernel still routes
    over a linkdown route unless `ignore_routes_with_linkdown` is 1 on that link, per family.
    """
    keys = {
        line.split("=", 1)[0].strip(): line.split("=", 1)[1].strip()
        for line in text.splitlines()
        if "=" in line and not line.lstrip().startswith("#")
    }
    problems = []
    for family in ("ipv4", "ipv6"):
        key = f"net.{family}.conf.{link}.ignore_routes_with_linkdown"
        if keys.get(key) != "1":
            problems.append(f"{key} is {keys.get(key)!r}, not '1'")
    return problems


@functools.cache
def _linkdown_text(host: str) -> str:
    return host_mode.render_host(host).files[_LINKDOWN_FILE][0].decode()


def test_each_pinned_link_ignores_its_routes_without_carrier() -> None:
    for host in _NODES:
        link = _host_vars(host)["k3s_node_static_link"]
        assert linkdown_sysctl_problems(_linkdown_text(host), link) == [], host


def test_a_linkdown_key_left_at_the_kernel_default_is_flagged() -> None:
    text = _linkdown_text("daniel-box").replace(
        "net.ipv6.conf.eno1.ignore_routes_with_linkdown=1",
        "net.ipv6.conf.eno1.ignore_routes_with_linkdown=0",
    )
    assert linkdown_sysctl_problems(text, "eno1") == [
        "net.ipv6.conf.eno1.ignore_routes_with_linkdown is '0', not '1'"
    ]


def test_a_linkdown_key_set_for_ipv4_only_is_flagged() -> None:
    text = "net.ipv4.conf.eno1.ignore_routes_with_linkdown=1\n"
    assert linkdown_sysctl_problems(text, "eno1") == [
        "net.ipv6.conf.eno1.ignore_routes_with_linkdown is None, not '1'"
    ]
