"""Every homelab sysctl key ships in a file under /etc/sysctl.d, which systemd-sysctl reads at boot.

Ubuntu 26.04's systemd-sysctl never reads /etc/sysctl.conf. Only `sysctl --system` does, so a key
kept there reverts at every reboot until reconcile next changes a sysctl file.
"""

import dataclasses
import re

import pytest
from _host_packages import HOSTS, package

from renderer.renderer_lib import host as host_mode

SYSCTL_D = re.compile(r"/etc/sysctl\.d/(?P<name>[^/]+\.conf)")
# systemd-sysctl applies files in name order across all sysctl.d directories, so a homelab file
# sorting before this loses each shared key.
LAST_DISTRO_FILE = "55-zeropage.conf"
INOTIFY = {"fs.inotify.max_user_instances": "1024"}


def declared(pkg: host_mode.Package) -> list[tuple[str, str, str]]:
    """Each (path, key, value) the package's sysctl.d files set, in path order."""
    found = []
    for dst, (data, _mode) in sorted(pkg.files.items()):
        if not SYSCTL_D.fullmatch(dst):
            continue
        for line in data.decode().splitlines():
            key, _, value = line.partition("=")
            if key.strip() and not key.lstrip().startswith(("#", ";")):
                found.append((dst, key.strip().lstrip("-"), value.strip()))
    return found


def sysctl_problems(pkg: host_mode.Package) -> list[str]:
    problems = [
        f"a line edit writes {edit['line']!r} to /etc/sysctl.conf"
        for edit in pkg.lines
        if edit["path"] == "/etc/sysctl.conf"
    ]
    for dst in sorted(pkg.files):
        match = SYSCTL_D.fullmatch(dst)
        if match and match["name"] <= LAST_DISTRO_FILE:
            problems.append(
                f"{dst} sorts before {LAST_DISTRO_FILE}, which overrides it"
            )
        if match and pkg.activations.get(dst) != "sysctl":
            problems.append(
                f"{dst} lacks `activate: sysctl`, so a change waits for a reboot"
            )
    owner: dict[str, str] = {}
    for dst, key, _value in declared(pkg):
        if owner.setdefault(key, dst) != dst:
            problems.append(f"{key} is set in both {owner[key]} and {dst}")
    return problems


@pytest.mark.parametrize("host", HOSTS)
def test_each_hosts_sysctl_keys_are_clean(host):
    assert sysctl_problems(package(host)) == []


def test_the_nodes_ship_the_pis_hardening_keys_and_the_inotify_key():
    pi = {key: value for _dst, key, value in declared(package("daniel-pi"))}
    assert pi["vm.swappiness"] == "130"
    hardening = {key: value for key, value in pi.items() if not key.startswith("vm.")}
    assert len(hardening) == 20
    for node in ("daniel-box", "daniel-server"):
        keys = {
            key: value
            for _dst, key, value in declared(package(node))
            if not key.endswith(".ignore_routes_with_linkdown")
        }
        assert keys == hardening | INOTIFY, node


def _with(**changes) -> host_mode.Package:
    return dataclasses.replace(package("daniel-box"), **changes)


def test_a_line_edit_on_sysctl_conf_is_flagged():
    real = package("daniel-box")
    edit = {
        "path": "/etc/sysctl.conf",
        "line": "vm.swappiness=10",
        "regexp": r"^vm\.swappiness=",
    }
    assert sysctl_problems(_with(lines=[*real.lines, edit])) == [
        "a line edit writes 'vm.swappiness=10' to /etc/sysctl.conf"
    ]


def test_a_key_set_in_two_sysctl_d_files_is_flagged():
    real = package("daniel-box")
    extra = "/etc/sysctl.d/91-homelab-extra.conf"
    pkg = _with(
        files={**real.files, extra: (b"kernel.sysrq=1\n", 0o644)},
        activations={**real.activations, extra: "sysctl"},
    )
    assert sysctl_problems(pkg) == [
        f"kernel.sysrq is set in both /etc/sysctl.d/90-homelab-host-tuning.conf and {extra}"
    ]


def test_a_sysctl_d_file_sorting_before_the_distributions_is_flagged():
    real = package("daniel-box")
    early = "/etc/sysctl.d/50-homelab-early.conf"
    pkg = _with(
        files={**real.files, early: (b"vm.swappiness=10\n", 0o644)},
        activations={**real.activations, early: "sysctl"},
    )
    assert sysctl_problems(pkg) == [
        f"{early} sorts before {LAST_DISTRO_FILE}, which overrides it"
    ]


def test_a_sysctl_d_file_with_no_activation_is_flagged():
    real = package("daniel-box")
    late = "/etc/sysctl.d/91-homelab-late.conf"
    pkg = _with(files={**real.files, late: (b"vm.swappiness=10\n", 0o644)})
    assert sysctl_problems(pkg) == [
        f"{late} lacks `activate: sysctl`, so a change waits for a reboot"
    ]
