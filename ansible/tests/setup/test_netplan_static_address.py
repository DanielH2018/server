"""Guard: the k3s server pins server_ip statically on the link that carries it, with DHCPv4 off.

WHY THIS EXISTS. k3s binds server_ip itself, so a node whose address comes from DHCP cannot
start k3s while the gateway's DHCP server is down. A WAN outage spanning the 2026-10-05 reboot
kept daniel-box's k3s crash-looping on `bind: cannot assign requested address` for ~70h
(#3882). The rendered file must carry the inventory's address, not merely an address, and
must switch DHCPv4 off rather than run it alongside (the template says why).
"""

from _helpers import ANSIBLE
from lib import yaml_fast
from _setup_render import render_setup_text

_TEMPLATE = "netplan-static-address.yaml.j2"
# The server node, the one static-address.yml runs on. daniel-server is still on DHCP; the
# agent path cannot apply the pin safely (static-address.yml's header says why).
_NODES = ("daniel-box",)


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
    return problems


def _host_vars(host: str) -> dict:
    return yaml_fast.safe_load(
        (ANSIBLE / "inventory" / "host_vars" / f"{host}.yml").read_text()
    )


def test_the_server_node_renders_its_own_address_pinned() -> None:
    router = yaml_fast.safe_load(
        (ANSIBLE / "inventory" / "group_vars" / "all.yml").read_text()
    )["lan_router_ip"]
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
"""


def test_a_pinned_address_is_clean() -> None:
    assert static_address_problems(_PINNED, "eno1", "10.0.0.215", "10.0.0.1") == []


def test_an_address_left_on_dhcp_is_flagged() -> None:
    doc = _PINNED.replace("dhcp4: false", "dhcp4: true")
    assert static_address_problems(doc, "eno1", "10.0.0.215", "10.0.0.1") == [
        "dhcp4 is not false"
    ]


def test_an_address_other_than_server_ip_is_flagged() -> None:
    problems = static_address_problems(_PINNED, "eno1", "10.0.0.216", "10.0.0.1")
    assert problems == ["addresses are ['10.0.0.215/24'], not 10.0.0.216"]


def test_emptying_the_link_removes_the_pin_and_reapplies() -> None:
    """The way back: an emptied variable must not leave DHCPv4 off on the link."""
    tasks = yaml_fast.safe_load(
        (
            ANSIBLE / "roles" / "setup" / "k3s" / "tasks" / "static-address.yml"
        ).read_text()
    )
    dest = "/etc/netplan/90-homelab-static-address.yaml"
    removal = [
        t
        for t in tasks
        if t.get("ansible.builtin.file", {}).get("path") == dest
        and t["ansible.builtin.file"].get("state") == "absent"
    ]
    assert len(removal) == 1, (
        "no task removes the pin when k3s_node_static_link is empty"
    )
    assert removal[0]["when"] == "k3s_node_static_link | length == 0"
    flag = f"{removal[0]['register']}.changed"
    commands = [t for t in tasks if "ansible.builtin.command" in t]
    assert {t["ansible.builtin.command"]["cmd"] for t in commands} == {
        "netplan generate",
        "netplan apply",
    }
    assert all(flag in t["when"] for t in commands), "removal does not trigger netplan"
