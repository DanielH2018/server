#!/usr/bin/env python3
"""Each k3s node's package gives every user on the host a name and a host key for daniel-pi.

Neither node's resolver has a record for the Pi, so `ssh daniel-pi` and `probe.py pi
containers` fail there with "Could not resolve hostname" (#8227). Either half of the host the
package owns can carry the name: a line in /etc/hosts or a drop-in under /etc/ssh/ssh_config.d/.
A per-user ssh config or known_hosts, such as the operator's on daniel-box or the
agent's under /usr/lib/homelab/claude-agent/, does not count: a rebuilt host has neither.

Run: uv run pytest hosts/tests/pi/test_server_names_daniel_pi.py
"""

import pytest
from _host_packages import package
from lib.estate import host_vars

SSH_DROPINS = "/etc/ssh/ssh_config.d/"
K3S_NODES = ("daniel-box", "daniel-server")


def _etc_hosts_names(pkg) -> dict[str, str]:
    names: dict[str, str] = {}
    for entry in pkg.lines:
        if entry.get("path") != "/etc/hosts":
            continue
        for row in (entry.get("line") or entry.get("block") or "").splitlines():
            fields = row.split("#", 1)[0].split()
            for name in fields[1:]:
                names[name] = fields[0]
    return names


def _ssh_dropin_names(pkg) -> dict[str, str]:
    names: dict[str, str] = {}
    for path, (body, _mode) in pkg.files.items():
        if not path.startswith(SSH_DROPINS):
            continue
        hosts: list[str] = []
        for row in body.decode().splitlines():
            fields = row.split("#", 1)[0].split()
            if len(fields) < 2:
                continue
            key = fields[0].lower()
            if key == "host":
                hosts = fields[1:]
            elif key == "hostname":
                for host in hosts:
                    names[host] = fields[1]
    return names


@pytest.mark.parametrize("host", K3S_NODES)
def test_node_pins_the_pi_host_key_under_every_name_it_gives_the_pi(host: str) -> None:
    """ssh looks a host key up under the name it connects to, so a pin under the address alone
    leaves `ssh daniel-pi` at "Host key verification failed"."""
    pi = host_vars("daniel-pi")
    pkg = package(host)
    body, _mode = pkg.files["/etc/ssh/ssh_known_hosts"]
    pinned: dict[str, str] = {}
    for row in body.decode().splitlines():
        if row.strip() and not row.startswith("#"):
            names, key = row.split(None, 1)
            pinned.update(dict.fromkeys(names.split(","), key))
    reachable_as = {pi["server_ip"], *_etc_hosts_names(pkg), *_ssh_dropin_names(pkg)}
    assert {name: pinned.get(name) for name in reachable_as} == dict.fromkeys(
        reachable_as, pi["ssh_host_ed25519_key"]
    )


@pytest.mark.parametrize("host", K3S_NODES)
def test_node_names_daniel_pi_at_the_pi_address(host: str) -> None:
    pi_ip = host_vars("daniel-pi")["server_ip"]
    pkg = package(host)
    names = {**_ssh_dropin_names(pkg), **_etc_hosts_names(pkg)}
    assert names.get("daniel-pi") == pi_ip, (
        f"{host}'s package maps daniel-pi to nothing in /etc/hosts or "
        f"{SSH_DROPINS}; mapped names: {names}"
    )
