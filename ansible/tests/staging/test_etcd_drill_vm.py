#!/usr/bin/env python3
"""The full etcd restore drill's throwaway guest, its cron, and the alarm derived from it.

hosts/components/hypervisor builds a transient libvirt guest per run and drives
scripts/backup/etcd_restore_drill.sh inside it. Three things here can drift silently and each
reads green from every other gate:

- the guest's MAC and the staging network's DHCP reservation for it (a mismatch boots a guest
  on a dynamic lease, and the orchestrator waits on an address nothing answers);
- the drill's deadline (`etcd_drill_full_kuma_interval_s`) and the schedule it is derived from
  (a deadline shorter than the period pages every month for nothing; one far longer lets a dead
  drill sit green for a year);
- the timer's shape: root, armed by the flag, day-of-month cadence, off every backup window.

This guest is the only one on the staging network, so
these checks are also what holds the network template to a shape a guest can actually use. Run:
uv run pytest ansible/tests/staging/test_etcd_drill_vm.py
"""

import re
import xml.etree.ElementTree as ET

import os
from pathlib import Path

from lib import yaml_fast
from jinja2 import Undefined
from lib.proc_testing import run, write_exec
from renderer.renderer_lib import host as host_mode
from lib.ansible_jinja_env import make_ansible_env, template_env
from _helpers import ALL_VARS, REPO
from _setup_render import rendered_setup_text
from _shell_render import rendered_shell_text


ROLE = REPO / "hosts" / "components" / "hypervisor"
STUB_SSH_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI0000000000000000000000000000 stub"


def _group_vars() -> dict:
    return yaml_fast.safe_load(ALL_VARS.read_text())


def _vars() -> dict:
    merged = yaml_fast.safe_load((ROLE / "defaults" / "main.yml").read_text())
    all_vars = _group_vars()
    merged["sys_user"] = all_vars["sys_user"]
    env = make_ansible_env(undefined_cls=Undefined)
    for key in (
        "hypervisor_etcd_drill_vm_disk",
        "hypervisor_etcd_drill_vm_xml",
        "hypervisor_etcd_drill_vm_seed_dir",
        "hypervisor_etcd_drill_vm_seed",
        "hypervisor_etcd_drill_vm_hostkey",
        "hypervisor_etcd_drill_share_dir",
    ):
        merged[key] = env.from_string(merged[key]).render(**merged)
    return merged


def _render(name: str) -> str:
    env = template_env(ROLE / "templates", undefined_cls=Undefined)
    return env.get_template(name).render(**_vars())


def _element(parent: ET.Element, path: str) -> ET.Element:
    """The element at `path`, failing with the missing path rather than a NoneType error."""
    found = parent.find(path)
    assert found is not None, f"no {path} element in the rendered XML"
    return found


def test_the_drill_guest_renders_as_a_kvm_domain_with_its_own_disks():
    v = _vars()
    root = ET.fromstring(_render("etcd-drill-vm.xml.j2"))
    assert root.tag == "domain" and root.get("type") == "kvm"
    assert root.findtext("name") == v["hypervisor_etcd_drill_vm_hostname"]
    sources = {
        _element(d, "source").get("file") for d in root.findall("./devices/disk")
    }
    assert sources == {
        v["hypervisor_etcd_drill_vm_disk"],
        v["hypervisor_etcd_drill_vm_seed"],
    }
    assert v["hypervisor_etcd_drill_vm_disk"] != v["hypervisor_base_image_path"], (
        "the drill guest must not boot the shared base image directly — the orchestrator "
        "deletes its disk on every exit, which would destroy the pinned download"
    )


def test_the_drill_guest_mac_matches_its_dhcp_reservation():
    v = _vars()
    domain_mac = _element(
        ET.fromstring(_render("etcd-drill-vm.xml.j2")), "./devices/interface/mac"
    ).get("address")
    net = ET.fromstring(_render("staging-network.xml.j2"))
    reservations = {h.get("mac"): h.get("ip") for h in net.findall("./ip/dhcp/host")}
    assert reservations.get(domain_mac) == v["hypervisor_etcd_drill_vm_ip"], (
        f"the drill guest's MAC {domain_mac} has no reservation at {v['hypervisor_etcd_drill_vm_ip']}; "
        f"reservations: {reservations}"
    )


def test_the_staging_network_pins_the_live_uuid():
    """`net-define` under any other UUID refuses on the name, which failed an apply (#1175)."""
    net = ET.fromstring(_render("staging-network.xml.j2"))
    assert net.findtext("uuid") == "255b2bc3-f6fa-4a2f-9d0c-8c14507eb82a"


def test_the_drill_guest_attaches_to_the_fenced_staging_network():
    v = _vars()
    iface = _element(
        ET.fromstring(_render("etcd-drill-vm.xml.j2")), "./devices/interface"
    )
    assert _element(iface, "source").get("network") == v["hypervisor_staging_net_name"]
    assert (
        _element(iface, "filterref").get("filter")
        == v["hypervisor_staging_nwfilter_name"]
    ), (
        "the drill guest holds the cluster token and R2 credentials; it must not reach the LAN"
    )


def _prepare(tmp_path) -> tuple[Path, Path]:
    """Run the rendered etcd-drill-prepare under a scratch root, with its tools stubbed."""
    v = _vars()
    root = tmp_path / "root"
    share = root / v["hypervisor_etcd_drill_share_dir"].lstrip("/")
    share.mkdir(parents=True)
    (share / "user-data.in").write_text(_render("etcd-drill-user-data.j2"))
    (share / "meta-data").write_text(_render("etcd-drill-meta-data.j2"))
    pub = root / "home" / v["sys_user"] / ".ssh" / "id_ed25519.pub"
    pub.parent.mkdir(parents=True)
    pub.write_text(STUB_SSH_KEY + "\n")
    image = root / v["hypervisor_base_image_path"].lstrip("/")
    image.parent.mkdir(parents=True)
    image.write_text("image")
    stubs = tmp_path / "bin"
    stubs.mkdir()
    calls = tmp_path / "calls"
    write_exec(
        stubs / "virsh", f'#!/bin/sh\necho "virsh $*" >> {calls}\necho "Active: yes"\n'
    )
    write_exec(
        stubs / "xorriso", f'#!/bin/sh\necho "xorriso $*" >> {calls}\ntouch "$4"\n'
    )
    write_exec(stubs / "usermod", f'#!/bin/sh\necho "usermod $*" >> {calls}\n')
    write_exec(stubs / "chgrp", "#!/bin/sh\n")
    write_exec(stubs / "curl", f'#!/bin/sh\necho "curl" >> {calls}\n')
    # Drain stdin: a stub that exits first can SIGPIPE the script's `echo |` under pipefail.
    write_exec(stubs / "sha256sum", "#!/bin/sh\ncat > /dev/null\nexit 0\n")
    write_exec(
        stubs / "ssh-keygen",
        f'#!/bin/sh\necho "ssh-keygen" >> {calls}\n'
        'while [ "$1" != "-f" ]; do shift; done\n'
        'printf "PRIVATE-1\\nPRIVATE-2\\n" > "$2"\necho HOSTPUB > "$2.pub"\n',
    )
    script = tmp_path / "prepare"
    write_exec(
        script,
        rendered_shell_text("components", "hypervisor", "etcd-drill-prepare.sh.j2"),
    )
    result = run(
        [str(script)],
        env={"HOMELAB_ROOT": str(root), "PATH": os.environ["PATH"]},
        stub_bin=stubs,
    )
    assert result.returncode == 0, result.stderr
    return root, calls


def test_prepare_fills_the_seed_with_the_hosts_keys_and_builds_the_iso(tmp_path):
    root, calls = _prepare(tmp_path)
    v = _vars()
    seed_dir = root / v["hypervisor_etcd_drill_vm_seed_dir"].lstrip("/")
    doc = yaml_fast.safe_load((seed_dir / "user-data").read_text())
    assert doc["users"][0]["ssh_authorized_keys"] == [STUB_SSH_KEY]
    assert doc["ssh_genkeytypes"] == ["ed25519"]
    assert doc["ssh_keys"]["ed25519_private"] == "PRIVATE-1\nPRIVATE-2\n"
    assert doc["ssh_keys"]["ed25519_public"] == "HOSTPUB"
    assert doc["ssh_pwauth"] is False
    assert (seed_dir / "user-data").stat().st_mode & 0o777 == 0o600
    called = calls.read_text()
    assert "-volid cidata" in called
    assert "net-destroy default" in called


def test_prepare_keeps_the_host_key_and_the_iso_on_a_second_run(tmp_path):
    """A new host key would break the orchestrator's pin, so the second run must not mint one."""
    root, calls = _prepare(tmp_path)
    first = calls.read_text()
    assert first.count("ssh-keygen") == 1 and first.count("xorriso") == 1
    script = tmp_path / "prepare"
    result = run(
        [str(script)],
        env={"HOMELAB_ROOT": str(root), "PATH": os.environ["PATH"]},
        stub_bin=tmp_path / "bin",
    )
    assert result.returncode == 0, result.stderr
    second = calls.read_text()
    assert second.count("ssh-keygen") == 1 and second.count("xorriso") == 1


def test_prepare_refuses_a_base_image_that_differs_from_the_pin(tmp_path):
    """The guest is built only from the reviewed image, so a mismatch stops the run."""
    _prepare(tmp_path)
    root = tmp_path / "root"
    write_exec(tmp_path / "bin" / "sha256sum", "#!/bin/sh\nexit 1\n")
    result = run(
        [str(tmp_path / "prepare")],
        env={"HOMELAB_ROOT": str(root), "PATH": os.environ["PATH"]},
        stub_bin=tmp_path / "bin",
    )
    assert result.returncode == 1
    assert "differs from the pin" in result.stderr


def _orchestrator_text() -> str:
    """The orchestrator as the shared accessor renders it.

    `_fence_probe.rendered_orchestrator` renders the same template a second way, with
    `hostvars` repointed at three real hosts, for a test whose subject IS a rendered dial
    target. The three tests below never touch one of those lines (lifecycle verbs, the
    detach/timeout shape, the host-key pins), so the accessor's plain render is enough —
    don't collapse the two into one read.
    """
    return rendered_shell_text(
        "components", "hypervisor", "etcd-restore-drill-vm.sh.j2"
    )


def test_the_orchestrator_never_defines_the_guest():
    """A defined domain would outlive the run, and the disk it names is deleted on exit."""
    script = _orchestrator_text()
    assert " create " in script and '"${VIRSH[@]}" create' in script
    assert "virsh define" not in script and '"${VIRSH[@]}" define' not in script
    assert "autostart" not in script


def test_the_orchestrator_detaches_the_drill_and_never_hands_timeout_a_function():
    """Two shapes of the same run that the first hand runs took, both of which read green here
    until they were executed: `timeout` execs its argument, so a shell function there is exit 127
    ("failed to run command 'ssh_guest'"); and one ssh session held open for the whole drill hung
    for 20 minutes after the drill had died. The orchestrator starts the guest script --detached
    and polls its exit file over fresh sessions instead."""
    script = _orchestrator_text()
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
    script = _orchestrator_text()
    assert "StrictHostKeyChecking=yes" in script
    assert "StrictHostKeyChecking=no" not in script
    assert "HostKeyAlgorithms=ssh-ed25519" in script


def test_the_timer_is_armed_by_the_flag_and_runs_the_cron_strings_slot():
    package = host_mode.render_host("daniel-server")
    timer = package.files["/usr/lib/systemd/system/homelab-etcd-restore-drill.timer"][
        0
    ].decode()
    assert "OnCalendar=*-*-01 11:20:00" in timer
    assert (
        "/usr/lib/systemd/system/timers.target.wants/homelab-etcd-restore-drill.timer"
        in package.links
    )
    component = yaml_fast.safe_load((ROLE / "component.yml").read_text())
    assert component["enable"]["homelab-etcd-restore-drill.timer"]["when"] == (
        "hypervisor_etcd_drill_armed"
    )
    service = package.files[
        "/usr/lib/systemd/system/homelab-etcd-restore-drill.service"
    ][0].decode()
    assert "User=" not in service, "virsh and the root-only env files need root"


def test_the_cadence_is_monthly_and_clear_of_every_backup_window():
    minute, hour, dom, month, dow = _group_vars()["etcd_drill_full_cron"].split()
    assert dom.isdigit() and month == "*" and dow == "*", (
        "expected a day-of-month schedule"
    )
    assert int(hour) not in range(2, 7), (
        "02:00-06:59 is the backup window on daniel-box"
    )
    assert (hour, minute) != ("10", "20"), "the Monday list-only slot on daniel-box"


def test_the_drill_deadline_is_derived_from_the_cadence():
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


def test_the_fence_verdict_has_a_check_of_its_own():
    """The fence's non-fatal verdict is written to its own textfile check.

    An unmeasured range leaves the restore drill passing, so the drill's verdict is `up` and the
    label is only message text. M6 batch B4's Cutover-2 dropped both Kuma pushes, so the second
    check is what alarms on it.
    """
    body = rendered_setup_text("hypervisor", "etcd-restore-drill-vm.sh.j2")
    assert "CHECK_FENCE=etcd-restore-drill-egress-fence" in body
    assert 'write_verdict "$CHECK_FENCE"' in body
    for leftover in ("kuma-push-lib", "kuma_push", "/api/push/", "PUSH_TOKEN"):
        assert leftover not in body, f"the orchestrator still names {leftover}"


def test_every_path_the_scripts_read_is_a_path_the_package_installs():
    """The `dst:` values are literals, so a drift would first fail the drill on the 1st."""
    package = host_mode.render_host("daniel-server")
    v = _vars()
    env = make_ansible_env(undefined_cls=Undefined)
    share = v["hypervisor_etcd_drill_share_dir"]
    wanted = {
        env.from_string(v[key]).render(**v)
        for key in (
            "hypervisor_etcd_drill_runner_path",
            "hypervisor_etcd_drill_prepare_path",
            "hypervisor_etcd_drill_script_path",
            "hypervisor_etcd_drill_guest_script_path",
            "hypervisor_etcd_drill_vm_xml",
            "hypervisor_suc_rehearsal_helper_path",
            "hypervisor_suc_rehearsal_guest_script_path",
        )
    } | {f"{share}/user-data.in", f"{share}/meta-data"}
    assert wanted <= set(package.files), wanted - set(package.files)
    assert v["hypervisor_etcd_drill_s3_env"] in package.links
