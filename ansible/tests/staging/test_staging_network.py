#!/usr/bin/env python3
"""The staging libvirt network must render, parse, and not collide with anything.

WHY THIS RENDERS RATHER THAN GREPS. Two bugs in this role reached a real host in one
afternoon, both structurally valid and both invisible to every check that only parses the
file they live in: an assert on `ansible_processor_flags` (not an Ansible fact), and a
verification whose become_user session predated the group it needed. ansible-lint on the
production profile, prek and the whole suite passed over both.

Nothing else in the repo validates libvirt XML -- validate_k8s_manifests.py covers k8s
templates and validate-compose covers Compose. So this renders the template with the real
defaults and parses the result, which is the only way a Jinja or XML mistake here fails
anywhere but on daniel-server.

The collision assertions are the other half. The subnet was chosen against a census of what
daniel-server actually routes; this pins that choice so a later edit cannot quietly move it
onto the LAN, the k3s ranges, or the bridges the retired Docker install left behind.

Run: uv run pytest ansible/tests/staging/test_staging_network.py
"""

import ipaddress
import xml.etree.ElementTree as ET

import pytest
import yaml
from jinja2 import Environment, FileSystemLoader
from _helpers import ANSIBLE


ROLE = ANSIBLE / "roles" / "setup" / "hypervisor"
TEMPLATE = "staging-network.xml.j2"

# Ranges daniel-server routes, or that libvirt itself defines. Sources, in order: lan_subnet
# in group_vars; k3s pod and service CIDRs; the bridges left behind by the Docker purge
# (#479 removed the packages, not the interfaces); libvirt's own `default` network.
RESERVED = [
    ipaddress.ip_network("10.0.0.0/24"),
    ipaddress.ip_network("10.42.0.0/16"),
    ipaddress.ip_network("10.43.0.0/16"),
    ipaddress.ip_network("10.200.0.0/16"),
    ipaddress.ip_network("172.17.0.0/16"),
    ipaddress.ip_network("192.168.122.0/24"),
]


def _vars():
    """Role defaults plus the group_vars the template reads, as Ansible would resolve them."""
    merged = yaml.safe_load((ROLE / "defaults" / "main.yml").read_text())
    all_vars = yaml.safe_load(
        (ANSIBLE / "inventory" / "group_vars" / "all.yml").read_text()
    )
    for key in ("staging_vm_hostname", "staging_vm_mac", "staging_vm_ip"):
        assert key in all_vars, f"{key} is not defined in group_vars/all.yml"
        merged[key] = all_vars[key]
    return merged


def _rendered():
    env = Environment(
        loader=FileSystemLoader(str(ROLE / "templates")),
        keep_trailing_newline=True,
    )
    return env.get_template(TEMPLATE).render(**_vars())


def _network():
    return ipaddress.ip_network(
        f"{_vars()['hypervisor_staging_net_gateway']}/"
        f"{_vars()['hypervisor_staging_net_netmask']}",
        strict=False,
    )


def test_the_template_renders_and_parses_as_xml():
    """A Jinja or tag mistake here fails on the host, not in any other check."""
    root = ET.fromstring(_rendered())
    assert root.tag == "network", f"root element is {root.tag!r}, expected 'network'"
    assert root.findtext("name") == _vars()["hypervisor_staging_net_name"]


def test_the_network_is_nat_with_a_named_bridge():
    root = ET.fromstring(_rendered())
    forward = root.find("forward")
    assert forward is not None and forward.get("mode") == "nat"
    bridge = root.find("bridge")
    assert bridge is not None
    name = bridge.get("name")
    # IFNAMSIZ is 16 including the NUL, so an interface name over 15 chars is rejected by
    # the kernel -- and libvirt reports it as a network that will not start.
    assert name and len(name) <= 15, f"bridge name {name!r} exceeds 15 characters"


@pytest.mark.parametrize("reserved", RESERVED, ids=lambda n: str(n))
def test_the_staging_subnet_does_not_overlap_something_in_use(reserved):
    net = _network()
    assert not net.overlaps(reserved), (
        f"the staging network {net} overlaps {reserved}, which daniel-server already "
        f"routes or libvirt already defines -- see this module's docstring for the census"
    )


def test_the_guest_reservation_is_inside_the_subnet():
    root = ET.fromstring(_rendered())
    host = root.find("./ip/dhcp/host")
    assert host is not None, "no DHCP host reservation for the guest"
    assert ipaddress.ip_address(host.get("ip")) in _network()


def test_the_guest_reservation_is_outside_the_dynamic_range():
    """A reservation inside the pool can be handed to something else first."""
    root = ET.fromstring(_rendered())
    reserved_ip = ipaddress.ip_address(root.find("./ip/dhcp/host").get("ip"))
    dhcp = root.find("./ip/dhcp/range")
    low = ipaddress.ip_address(dhcp.get("start"))
    high = ipaddress.ip_address(dhcp.get("end"))
    assert not (low <= reserved_ip <= high), (
        f"the guest reservation {reserved_ip} sits inside the dynamic range "
        f"{low}-{high}; move it outside so the lease cannot be taken first"
    )


def test_the_guest_mac_uses_the_qemu_oui():
    """52:54:00 is QEMU's OUI. A MAC outside it risks colliding with real hardware."""
    root = ET.fromstring(_rendered())
    mac = root.find("./ip/dhcp/host").get("mac")
    assert mac.lower().startswith("52:54:00:"), (
        f"guest MAC {mac!r} is outside QEMU's 52:54:00 OUI"
    )
