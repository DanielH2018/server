#!/usr/bin/env python3
"""daniel-server's package gives every user on the host a name for daniel-pi (#8227).

daniel-server's resolver has no record for the Pi, so `ssh daniel-pi` and `probe.py pi
containers` fail there with "Could not resolve hostname". Either half of the host the package
owns can carry the name: a line in /etc/hosts or a drop-in under /etc/ssh/ssh_config.d/. A
per-user ssh config, such as the agent's under /usr/lib/homelab/claude-agent/, does not count:
the sessions that hit this run as `ubuntu`.

Run: uv run pytest hosts/tests/test_server_names_daniel_pi.py
"""

from _host_packages import package
from lib import yaml_fast
from lib.repo_paths import HOST_VARS

SSH_DROPINS = "/etc/ssh/ssh_config.d/"


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
                names.update(dict.fromkeys(hosts, fields[1]))
    return names


def test_daniel_server_names_daniel_pi_at_the_pi_address() -> None:
    pi_ip = yaml_fast.safe_load((HOST_VARS / "daniel-pi.yml").read_text())["server_ip"]
    pkg = package("daniel-server")
    names = {**_ssh_dropin_names(pkg), **_etc_hosts_names(pkg)}
    assert names.get("daniel-pi") == pi_ip, (
        "daniel-server's package maps daniel-pi to nothing in /etc/hosts or "
        f"{SSH_DROPINS}; mapped names: {names}"
    )
