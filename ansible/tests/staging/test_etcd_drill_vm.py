#!/usr/bin/env python3
"""The full etcd restore drill's throwaway guest, its cron, and the alarm derived from it (#1175).

roles/setup/hypervisor builds a transient libvirt guest per run and drives
scripts/backup/etcd_restore_drill.sh inside it. Three things here can drift silently and each
reads green from every other gate:

- the guest's MAC and the staging network's DHCP reservation for it (a mismatch boots a guest
  on a dynamic lease, and the orchestrator waits on an address nothing answers);
- the Kuma tile's deadline and the cron it is derived from (a deadline shorter than the period
  pages every month for nothing; one far longer lets a dead drill sit green for a year — the
  exact failure #1175's "1-year budget" would have built in over a monthly cron);
- the cron task's shape: root, armed by the flag, day-of-month cadence, off every backup window.

Renders the templates the same way test_staging_vm.py does. Run:
uv run pytest ansible/tests/staging/test_etcd_drill_vm.py
"""

import re
import xml.etree.ElementTree as ET

from lib import yaml_fast
from jinja2 import Environment, FileSystemLoader
from _helpers import ANSIBLE, load_yaml


ROLE = ANSIBLE / "roles" / "setup" / "hypervisor"
GROUP_VARS = ANSIBLE / "inventory" / "group_vars" / "all.yml"
KUMA_TEMPLATE = (
    ANSIBLE / "roles" / "k8s" / "uptime-kuma" / "templates" / "static-monitors.yaml.j2"
)
CRON_TASK = "Schedule the full etcd restore drill"
STUB_SSH_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI0000000000000000000000000000 stub"


def _group_vars() -> dict:
    return yaml_fast.safe_load(GROUP_VARS.read_text())


def _vars() -> dict:
    merged = yaml_fast.safe_load((ROLE / "defaults" / "main.yml").read_text())
    all_vars = _group_vars()
    for key in ("staging_vm_hostname", "staging_vm_mac", "staging_vm_ip", "sys_user"):
        merged[key] = all_vars[key]
    merged["hypervisor_etcd_drill_vm_ssh_key"] = STUB_SSH_KEY
    merged["hypervisor_staging_net_uuid"] = "00000000-0000-5000-8000-000000000000"
    merged["hypervisor_etcd_drill_vm_hostkey_private"] = (
        "-----BEGIN OPENSSH PRIVATE KEY-----\nstub\n-----END OPENSSH PRIVATE KEY-----"
    )
    merged["hypervisor_etcd_drill_vm_hostkey_public"] = STUB_SSH_KEY
    env = Environment()
    for key in (
        "hypervisor_etcd_drill_vm_disk",
        "hypervisor_etcd_drill_vm_xml",
        "hypervisor_etcd_drill_vm_seed_dir",
        "hypervisor_etcd_drill_vm_seed",
        "hypervisor_etcd_drill_vm_hostkey",
    ):
        merged[key] = env.from_string(merged[key]).render(**merged)
    return merged


def _render(name: str) -> str:
    env = Environment(
        loader=FileSystemLoader(str(ROLE / "templates")), keep_trailing_newline=True
    )
    return env.get_template(name).render(**_vars())


def _cron_task() -> dict:
    docs = load_yaml(ROLE / "tasks" / "etcd_drill.yml")
    task = next((t for t in docs if t.get("name") == CRON_TASK), None)
    assert task is not None, f"{CRON_TASK!r} is missing from etcd_drill.yml"
    return task


def test_the_drill_guest_renders_as_a_kvm_domain_with_its_own_disks():
    v = _vars()
    root = ET.fromstring(_render("etcd-drill-vm.xml.j2"))
    assert root.tag == "domain" and root.get("type") == "kvm"
    assert root.findtext("name") == v["hypervisor_etcd_drill_vm_hostname"]
    sources = {d.find("source").get("file") for d in root.findall("./devices/disk")}
    assert sources == {
        v["hypervisor_etcd_drill_vm_disk"],
        v["hypervisor_etcd_drill_vm_seed"],
    }
    assert v["hypervisor_etcd_drill_vm_disk"] != v["hypervisor_staging_vm_disk"], (
        "the drill guest must not share the staging guest's disk — the orchestrator deletes it"
    )


def test_the_drill_guest_mac_matches_its_dhcp_reservation_and_is_not_the_staging_guests():
    v = _vars()
    domain_mac = (
        ET.fromstring(_render("etcd-drill-vm.xml.j2"))
        .find("./devices/interface/mac")
        .get("address")
    )
    net = ET.fromstring(_render("staging-network.xml.j2"))
    reservations = {h.get("mac"): h.get("ip") for h in net.findall("./ip/dhcp/host")}
    assert reservations.get(domain_mac) == v["hypervisor_etcd_drill_vm_ip"], (
        f"the drill guest's MAC {domain_mac} has no reservation at {v['hypervisor_etcd_drill_vm_ip']}; "
        f"reservations: {reservations}"
    )
    assert domain_mac != v["staging_vm_mac"]
    assert v["hypervisor_etcd_drill_vm_ip"] != v["staging_vm_ip"]


def test_the_staging_network_pins_its_uuid_and_pushes_reservations_live():
    """The drill's reservation was the first change to the network XML since bring-up, and it
    failed the apply twice over: net-define minted a fresh UUID and refused on the name, and
    a running network's dnsmasq ignores the persistent config until the network restarts."""
    net = ET.fromstring(_render("staging-network.xml.j2"))
    assert net.findtext("uuid") == _vars()["hypervisor_staging_net_uuid"]
    tasks = load_yaml(ROLE / "tasks" / "network.yml")
    pin = next(t for t in tasks if t["name"] == "Pin the staging network's UUID")
    assert "to_uuid" in pin["ansible.builtin.set_fact"]["hypervisor_staging_net_uuid"]
    live = next(t for t in tasks if "DHCP reservation" in t["name"])
    argv = live["ansible.builtin.command"]["argv"]
    assert "net-update" in argv and "--live" in argv and "ip-dhcp-host" in argv
    assert "existing dhcp host entry" in live["failed_when"]
    reserved = {h["name"] for h in live["loop"]}
    assert reserved == {
        "{{ staging_vm_hostname }}",
        "{{ hypervisor_etcd_drill_vm_hostname }}",
    }


def test_the_drill_guest_is_fenced_like_the_staging_guest():
    v = _vars()
    iface = ET.fromstring(_render("etcd-drill-vm.xml.j2")).find("./devices/interface")
    assert iface.find("source").get("network") == v["hypervisor_staging_net_name"]
    assert (
        iface.find("filterref").get("filter") == v["hypervisor_staging_nwfilter_name"]
    ), (
        "the drill guest holds the cluster token and R2 credentials; it must not reach the LAN"
    )


def test_the_drill_guest_user_data_pins_the_host_key():
    rendered = _render("etcd-drill-user-data.j2")
    assert rendered.startswith("#cloud-config")
    doc = yaml_fast.safe_load(rendered)
    assert doc["hostname"] == _vars()["hypervisor_etcd_drill_vm_hostname"]
    assert doc["ssh_pwauth"] is False
    assert doc["users"][0]["ssh_authorized_keys"] == [STUB_SSH_KEY]
    assert doc["ssh_genkeytypes"] == ["ed25519"]
    assert "stub" in doc["ssh_keys"]["ed25519_private"], (
        "the seeded private host key did not render"
    )
    assert doc["ssh_keys"]["ed25519_public"] == STUB_SSH_KEY


def test_the_orchestrator_never_defines_the_guest():
    """A defined second domain would silently break teardown.yml's 'no guest defined' refusal."""
    script = (ROLE / "templates" / "etcd-restore-drill-vm.sh.j2").read_text()
    assert " create " in script and '"${VIRSH[@]}" create' in script
    assert "virsh define" not in script and '"${VIRSH[@]}" define' not in script
    assert "autostart" not in script


def test_the_orchestrator_detaches_the_drill_and_never_hands_timeout_a_function():
    """Two shapes of the same run that the first hand runs took, both of which read green here
    until they were executed: `timeout` execs its argument, so a shell function there is exit 127
    ("failed to run command 'ssh_guest'"); and one ssh session held open for the whole drill hung
    for 20 minutes after the drill had died. The orchestrator starts the guest script --detached
    and polls its exit file over fresh sessions instead."""
    script = (ROLE / "templates" / "etcd-restore-drill-vm.sh.j2").read_text()
    guest = (ROLE / "files" / "etcd-drill-guest-run.sh").read_text()
    assert "etcd-drill-guest-run --detached" in script
    assert '"${1:-}" == "--detached"' in guest and "nohup" in guest
    assert (
        "/var/tmp/etcd-drill.rc" in script
        and "/var/tmp/etcd-drill.rc"
        in guest.replace("RC=/var/tmp/etcd-drill.rc", "/var/tmp/etcd-drill.rc")
    )
    functions = set(re.findall(r"^([A-Za-z_]\w*)\(\) \{", script, re.M))
    assert "ssh_guest" in functions
    joined = script.replace("\\\n", " ")
    for args in re.findall(r"\btimeout\b((?:\s+\S+)+)", joined):
        words = [w for w in args.split() if not w.startswith("-") and "=" not in w]
        if len(words) < 2:
            continue
        assert words[1] not in functions, f"timeout cannot run the function {words[1]}"


def test_the_orchestrator_pins_the_guest_host_key():
    script = (ROLE / "templates" / "etcd-restore-drill-vm.sh.j2").read_text()
    assert "StrictHostKeyChecking=yes" in script
    assert "StrictHostKeyChecking=no" not in script
    assert "HostKeyAlgorithms=ssh-ed25519" in script


def test_the_cron_is_root_and_follows_the_armed_flag():
    task = _cron_task()
    cron = task["ansible.builtin.cron"]
    assert cron["user"] == "root"
    assert "hypervisor_etcd_drill_armed" in cron["state"]
    assert _vars()["hypervisor_etcd_drill_armed"] is True


def test_the_cadence_is_monthly_and_clear_of_every_backup_window():
    minute, hour, dom, month, dow = _group_vars()["etcd_drill_full_cron"].split()
    assert dom.isdigit() and month == "*" and dow == "*", (
        "expected a day-of-month schedule"
    )
    assert int(hour) not in range(2, 7), (
        "02:00-06:59 is the backup window on daniel-box"
    )
    assert (hour, minute) != ("10", "20"), "the Monday list-only slot on daniel-box"
    cron = _cron_task()["ansible.builtin.cron"]
    assert "etcd_drill_full_cron.split()[2]" in cron["day"], (
        "the cron task must schedule by day-of-month from the same string the deadline is derived from"
    )


def test_the_kuma_deadline_is_derived_from_the_cadence():
    """Longer than the longest possible gap between runs, shorter than two of them."""
    gv = _group_vars()
    _, _, dom, month, dow = gv["etcd_drill_full_cron"].split()
    assert dom.isdigit() and month == "*" and dow == "*"
    longest_gap_s = 31 * 86400
    deadline = gv["etcd_drill_full_kuma_interval_s"]
    assert longest_gap_s < deadline < 2 * longest_gap_s, (
        f"etcd_drill_full_kuma_interval_s={deadline}: below {longest_gap_s} it pages on every "
        f"31-day month; above {2 * longest_gap_s} a dead drill sits green for a whole extra cycle"
    )
    tile = KUMA_TEMPLATE.read_text()
    assert '"interval": {{ etcd_drill_full_kuma_interval_s }}' in tile, (
        "the Kuma tile must take its deadline from the same group_vars value"
    )
    assert "etcd_drill_full_push_token" in tile


def test_the_orchestrator_is_registered_as_a_cross_host_token_consumer():
    consumers = (
        ANSIBLE.parent / "scripts" / "secrets_mgmt" / "consumers.py"
    ).read_text()
    assert '"etcd_drill_full_push_token"' in consumers
