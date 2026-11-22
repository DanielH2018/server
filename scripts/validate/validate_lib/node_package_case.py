#!/usr/bin/env python3
"""The container case that installs each cluster node's real package and reads `systemctl --failed`.

`node_units_case` starts two of daniel-server's and daniel-box's files on their Ubuntu packages;
this case installs the whole of each node's package on each `host_packages` leg's release, so a
unit or script that leans on a release-specific path or tool fails here before a host boots on
that release. For each node it boots a container with the node's hostname, which the package's
preinst requires, and:

  * seals the real package with a throwaway age key (`secrets_case.seal_host`), as the Pi's
    by-hand case does, so `homelab-secrets` decrypts and reconcile can run;
  * seeds the host state a container lacks (`seed_node_host`);
  * installs homelab-tools, homelab-updater and the sealed package with apt, which pulls the
    package's `Depends` from the release's archive, and joins the reconcile the postinst queued;
  * waits SETTLE_SECONDS, then asserts no unit is failed and no service has restarted itself.
    A long-running service's start returns once it forks, so one that crashes a second later,
    or loops through `Restart=` without reaching its start limit, shows only after the wait.
    Reconcile exits 1 when a unit it starts fails, so its own unit is flagged then too.
"""

import base64
import subprocess
import sys

from renderer.renderer_lib import host as host_mode
from validate.validate_lib.host_container import CaseFailed, Container, expect
from validate.validate_lib.host_fixtures import build
from validate.validate_lib.pi_host_seed import seed_pi_host
from validate.validate_lib.secrets_case import seal_host

NODES = ("daniel-server", "daniel-box")
NAME = "install {host}'s real package, and no unit fails"
# What a real node has from its Ubuntu server install and the image lacks.
SERVER_PACKAGES = ("netplan.io", "nftables")
# The operator's per-user tools, which the dotfiles repo installs and the package does not.
DOTFILES_TOOLS = ("/home/ubuntu/.local/bin/uv", "/home/ubuntu/.local/bin/claude")
EXIT_0 = "#!/bin/sh\nexit 0\n"
# Long enough for a forked service that dies, or a restart loop's RestartSec, to show.
SETTLE_SECONDS = 30
# Under `Container.sh`'s 900-second default, which kills the exec and drops its output.
STEP_SECONDS = 840
UNIT_PROPERTIES = "Id,ActiveState,SubState,NRestarts"
# Each unit whose start needs something only the real host has, and that thing. A drop-in swaps
# its ExecStart= for `sleep`, so its other lines still load and its enable link still starts it.
STUBBED_UNITS = {
    "coredns-host.service": "daniel-box's LAN address, which its listener binds",
    "homelab-apt-repo.service": "daniel-box's LAN address, which its listener binds",
    "deploy-ui.service": "the Python uv runs it with, which the dotfiles repo installs",
}
STUB_DROPIN = (
    "[Service]\nExecStart=\nExecStart=/bin/sleep infinity\n"
    "ExecReload=\nExecReload=/bin/true\n"
)


def unit_problems(show: str) -> dict[str, str]:
    """Each unit that failed or restarted itself, mapped to which, from `systemctl show` output.

    Args:
        show: `systemctl show -p` UNIT_PROPERTIES for every loaded unit: one block of
            `property=value` lines per unit, blocks separated by a blank line.
    """
    problems = {}
    for block in show.split("\n\n"):
        unit = dict(line.split("=", 1) for line in block.splitlines() if "=" in line)
        if unit.get("ActiveState") == "failed":
            problems[unit["Id"]] = "failed"
        elif (
            unit.get("SubState") == "auto-restart" or unit.get("NRestarts", "0") != "0"
        ):
            problems[unit["Id"]] = (
                f"restarted itself {unit.get('NRestarts', '?')} times"
            )
    return problems


def _put(c: Container, path: str, text: str, mode: str) -> None:
    encoded = base64.b64encode(text.encode()).decode()
    c.ok(
        f"install -d {path.rsplit('/', 1)[0]} && echo {encoded} | base64 -d > {path}"
        f" && chmod {mode} {path}"
    )


def seed_node_host(c: Container) -> None:
    """Seed in container `c` the host state a cluster node has and the container lacks.

    The nodes ship the Pi's sshd, ufw, auditd, postfix, hosts and modules-load subjects, so they
    get `seed_pi_host`, which unmounts /etc/resolv.conf: no name resolves after it, so every
    fetch from the archive comes first. A node adds the Ubuntu server packages in
    SERVER_PACKAGES, the dotfiles tools in DOTFILES_TOOLS as stubs that exit 0, and a drop-in on
    each STUBBED_UNITS unit.
    """
    c.ok(
        "DEBIAN_FRONTEND=noninteractive apt-get install -y"
        f" {' '.join(SERVER_PACKAGES)} < /dev/null"
    )
    seed_pi_host(c)
    for path in DOTFILES_TOOLS:
        _put(c, path, EXIT_0, "0755")
    c.ok("chown -R ubuntu:ubuntu /home/ubuntu/.local")
    for unit in STUBBED_UNITS:
        _put(c, f"/etc/systemd/system/{unit}.d/10-test.conf", STUB_DROPIN, "0644")
    c.ok("systemctl daemon-reload")


def _install(cases, c: Container, host: str) -> None:
    seal_dir = seal_host(cases, c, host)
    head = seal_dir.parent / "head"
    host_mode.write_packages([host_mode.render_host(host, sops_dir=seal_dir)], head)
    deb = build(head / "hosts" / host, cases.versions["pi-head"], head.parent / "deb")
    cases.add(f"{host}-head", deb, name=f"sealed-{c.name}-{deb.name}")
    debs = " ".join(
        f"/srv/debs/{cases.debs[label].name}"
        for label in ("updater-head", f"{host}-head", *cases.tools)
    )
    # The Depends are fetched while names still resolve; the seed takes resolv.conf away.
    c.ok(f"apt-get install -y --download-only {debs} < /dev/null")
    seed_node_host(c)
    # A shell timeout under the exec's own, so a hung step still returns what it printed.
    c.ok(
        f"DEBIAN_FRONTEND=noninteractive timeout {STEP_SECONDS} apt-get install -y {debs} < /dev/null"
    )
    # Joins the reconcile the postinst queued, and returns once it ends.
    c.sh(f"timeout {STEP_SECONDS} systemctl start homelab-reconcile.service")
    c.ok(f"sleep {SETTLE_SECONDS}")
    problems = unit_problems(c.ok(f"systemctl show -p {UNIT_PROPERTIES} '*'"))
    for unit in problems:
        journal = c.sh(f"journalctl -u {unit} -n 40 --no-pager -o short-monotonic")
        print(f"::group::{unit} journal\n{journal.stdout}::endgroup::", file=sys.stderr)
    expect(
        not problems,
        "; ".join(f"{unit} {problem}" for unit, problem in problems.items()),
    )


def run(cases) -> None:
    """Install each node's package in its own container, and record each verdict on `cases`."""
    for host in NODES:
        name = NAME.format(host=host)
        c = cases.boot(f"node-{host}", hostname=host)
        if c is None:
            continue
        try:
            _install(cases, c, host)
        except (CaseFailed, subprocess.SubprocessError) as exc:
            cases.failed.append(name)
            print(f"[FAIL] {c.name}: {name}: {exc}", file=sys.stderr)
        else:
            cases.passed.append(name)
        finally:
            c.remove()
