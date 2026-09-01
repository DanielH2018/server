#!/usr/bin/env python3
"""The staging guest definition must render, parse, and agree with the network.

WHY THIS RENDERS. Same reason as test_staging_network.py: nothing in this repo validates
libvirt XML or cloud-init YAML, and every bug this role shipped to a real host was a
structurally valid file that only failed when something executed it.

The load-bearing assertion is the MAC cross-check. The guest's address is a DHCP
reservation, so the domain's interface MAC and the network's `<host mac=...>` entry have to
be the same string. If they drift, the guest still boots and still gets an address -- just a
dynamic one -- and the inventory entry that names staging_vm_ip in a later slice points at
nothing. That failure is silent at every layer except the one that tries to ssh in.

Run: uv run pytest ansible/tests/test_staging_vm.py
"""

import xml.etree.ElementTree as ET

import yaml
from jinja2 import Environment, FileSystemLoader
from _helpers import ANSIBLE


ROLE = ANSIBLE / "roles" / "setup" / "hypervisor"

# A stand-in for the key slurped off the host at run time.
STUB_SSH_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI0000000000000000000000000000 stub"


def _vars():
    merged = yaml.safe_load((ROLE / "defaults" / "main.yml").read_text())
    all_vars = yaml.safe_load(
        (ANSIBLE / "inventory" / "group_vars" / "all.yml").read_text()
    )
    for key in ("staging_vm_hostname", "staging_vm_mac", "staging_vm_ip", "sys_user"):
        assert key in all_vars, f"{key} is not defined in group_vars/all.yml"
        merged[key] = all_vars[key]
    merged["hypervisor_staging_vm_ssh_key"] = STUB_SSH_KEY
    # Role defaults reference each other; Jinja in a default value is not resolved by
    # yaml.safe_load, so render the two that matter into plain strings.
    env = Environment()
    for key in (
        "hypervisor_staging_vm_disk",
        "hypervisor_staging_vm_seed",
        "hypervisor_staging_vm_seed_dir",
    ):
        merged[key] = env.from_string(merged[key]).render(**merged)
    return merged


def _render(name):
    env = Environment(
        loader=FileSystemLoader(str(ROLE / "templates")), keep_trailing_newline=True
    )
    return env.get_template(name).render(**_vars())


def _domain():
    return ET.fromstring(_render("staging-vm.xml.j2"))


def test_the_domain_renders_and_parses_as_xml():
    root = _domain()
    assert root.tag == "domain"
    assert root.get("type") == "kvm", (
        "domain type must be kvm -- 'qemu' would silently fall back to software emulation, "
        "which boots and is far too slow to be a useful staging cluster"
    )


def test_the_domain_sizing_matches_the_declared_values():
    root, v = _domain(), _vars()
    assert int(root.findtext("memory")) == v["hypervisor_staging_vm_memory_mib"]
    assert int(root.findtext("vcpu")) == v["hypervisor_staging_vm_vcpus"]


def test_the_domain_mac_matches_the_network_reservation():
    """The whole reason the guest has a predictable address."""
    domain_mac = _domain().find("./devices/interface/mac").get("address")
    net = ET.fromstring(_render("staging-network.xml.j2"))
    reserved_mac = net.find("./ip/dhcp/host").get("mac")
    assert domain_mac == reserved_mac, (
        f"the domain's interface MAC ({domain_mac}) and the network's DHCP reservation "
        f"({reserved_mac}) differ, so the guest would take a dynamic lease and "
        f"staging_vm_ip would point at nothing"
    )


def test_the_domain_attaches_the_staging_network():
    source = _domain().find("./devices/interface/source").get("network")
    assert source == _vars()["hypervisor_staging_net_name"]


def test_the_domain_disks_point_at_the_declared_paths():
    v = _vars()
    sources = {
        d.find("source").get("file") for d in _domain().findall("./devices/disk")
    }
    assert v["hypervisor_staging_vm_disk"] in sources
    assert v["hypervisor_staging_vm_seed"] in sources


def test_the_user_data_parses_as_cloud_config():
    rendered = _render("cloud-init-user-data.j2")
    assert rendered.startswith("#cloud-config"), (
        "cloud-init ignores a user-data file that does not open with the #cloud-config "
        "line, so the guest would boot with no key and no hostname"
    )
    doc = yaml.safe_load(rendered)
    assert doc["hostname"] == _vars()["staging_vm_hostname"]
    assert doc["ssh_pwauth"] is False
    keys = doc["users"][0]["ssh_authorized_keys"]
    assert keys == [STUB_SSH_KEY], f"the authorised key did not render: {keys!r}"


def test_the_meta_data_parses_and_names_the_guest():
    doc = yaml.safe_load(_render("cloud-init-meta-data.j2"))
    assert doc["local-hostname"] == _vars()["staging_vm_hostname"]
    assert doc["instance-id"]
