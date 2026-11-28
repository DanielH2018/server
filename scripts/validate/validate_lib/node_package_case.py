#!/usr/bin/env python3
"""The container case that installs each cluster node's real package and reads `systemctl --failed`.

`node_units_case` starts two of daniel-server's and daniel-box's files on their Ubuntu packages;
this case installs the whole of each node's package on each `host_packages` leg's release, so a
unit or script that leans on a release-specific path or tool fails here before a host boots on
that release. For each node it boots a container with the node's hostname, which the package's
preinst requires, and:

  * masks the gettys the runner's shared consoles start (`mask_gettys`);
  * seals the real package with a throwaway age key (`secrets_case.seal_host`), as the Pi's
    by-hand case does, so `homelab-secrets` decrypts and reconcile can run;
  * seeds the host state a container lacks (`seed_node_host`);
  * installs homelab-tools, homelab-updater and the sealed package with apt, which pulls the
    package's `Depends` from the release's archive, and joins the reconcile the postinst queued.
    Every apt call skips Recommends, as the updater does (`hosts/lib/homelab-updater:INSTALL`):
    each Depends a host gains after its bootstrap arrives that way, so a unit that needs a
    Recommends fails here as it would on that host;
  * waits SETTLE_SECONDS, then asserts no unit is failed and no service has restarted itself.
    A long-running service's start returns once it forks, so one that crashes a second later,
    or loops through `Restart=` without reaching its start limit, shows only after the wait.
    Reconcile exits 1 when a unit it starts fails, so its own unit is flagged then too.

A timer-fired service is not exercised. Most of the nodes' timers fire every few minutes by the
wall clock, and most of their services reach the cluster, GitHub or a uv download, so whether
one fails inside the window would depend on the minute the case runs. A drop-in moves each
shipped timer's every trigger to TIMER_FIRES; each timer still loads, gets its enable link and is
started by reconcile, so a timer file a release cannot parse still fails here.

The failure pages (`homelab-alert@`, `homelab-claude-rc-alert`) start only when another unit
fails, so a green run never delivers one. Their curl floor is recorded in
`hosts/components/failure-alert/component.yml`.
"""

import base64
import subprocess
import sys
import time
from collections.abc import Generator
from contextlib import contextmanager

from renderer.renderer_lib import host as host_mode
from validate.validate_lib.host_container import CaseFailed, Container, expect
from validate.validate_lib.host_fixtures import build
from validate.validate_lib.pi_host_seed import seed_pi_host
from validate.validate_lib.secrets_case import seal_host

NODES = ("daniel-server", "daniel-box")
NAME = "install {host}'s real package, and no unit fails"
# claude-agent-home runs openssh-client's ssh-keygen.
SERVER_PACKAGES = ("netplan.io", "nftables", "openssh-client")
# claude-agent-setup's `loginctl` needs logind, which needs dbus. They install in the package's own
# transaction: a system bus already running when polkitd installs refuses polkitd its name.
BUS_PACKAGES = ("dbus", "libpam-systemd")
APT_INSTALL = "apt-get install -y --no-install-recommends"
# The operator's per-user tools, which the dotfiles repo installs and the package does not.
DOTFILES_TOOLS = ("/home/ubuntu/.local/bin/uv", "/home/ubuntu/.local/bin/claude")
EXIT_0 = "#!/bin/sh\nexit 0\n"
# claude-agent-setup clones each from GitHub only while its .git is missing, and no name resolves
# once the seed has run.
AGENT_CLONES = (
    "/var/lib/claude/.local/share/chezmoi/.git",
    "/var/lib/claude/server/.git",
)
# The image masks systemd-udevd so no case reaches the runner's devices through --privileged; a node
# runs udevd, which reconcile's udev arm reloads.
UDEVADM_STUB = "/usr/local/sbin/udevadm"
# virsh's defines failed identically on 24.04 and 26.04, so the container refuses libvirt; the stub
# leaves reconcile's libvirt arm untested here.
VIRSH_STUB = "/usr/local/sbin/virsh"
# Every container shares the runner's virtual consoles through --privileged, so getty on one trips
# another's start limits; a node container masks the rest.
GETTY_TEMPLATES = ("getty@.service", "autovt@.service")
# Long enough for a forked service that dies, or a restart loop's RestartSec, to show.
SETTLE_SECONDS = 30
# Under `Container.sh`'s 900-second default, which kills the exec and drops its output.
STEP_SECONDS = 840
UNIT_PROPERTIES = "Id,ActiveState,SubState,NRestarts"
# A drop-in clears what runs or changes directory before ExecStart= and swaps in `sleep`, so the
# other lines still load and the enable link still starts it.
STUBBED_UNITS = {
    "coredns-host.service": "daniel-box's LAN address, which its listener binds",
    "homelab-apt-repo.service": "daniel-box's LAN address, which its listener binds",
    "deploy-ui.service": "the operator's checkout, its working directory, and the uv the"
    " dotfiles repo installs",
    "homelab-claude-rc.service": "the operator's checkout, its working directory, and a"
    " logged-in Claude Code",
}
STUB_DROPIN = (
    "[Service]\nWorkingDirectory=/\nBindPaths=\nReadWritePaths=\nExecStartPre=\n"
    "ExecStart=\nExecStart=/bin/sleep infinity\nExecReload=\nExecReload=/bin/true\n"
)
# The same for each oneshot unit, whose ExecStart= becomes `true` so that its start returns.
STUBBED_ONESHOTS = {
    "homelab-builder.service": "a Python download, and no name resolves once the seed has run",
    "homelab-repo-sign.service": "the packages the builder stages, and the stubbed builder,"
    " whose OnSuccess= starts it, stages none",
    "homelab-renovate-agent-setup.service": "a Claude Code download from claude.ai, and no name"
    " resolves once the seed has run",
    "homelab-claude-guardrail-audit.service": "the kernel's audit control, which it refuses"
    " outside the initial PID namespace",
}
ONESHOT_DROPIN = (
    "[Service]\nWorkingDirectory=/\nReadWritePaths=\nExecStartPre=\n"
    "ExecStart=\nExecStart=/bin/true\n"
)
# An empty assignment resets every trigger of a timer, the monotonic ones included.
TIMER_FIRES = "2099-01-01"
TIMER_DROPIN = f"[Timer]\nOnCalendar=\nOnCalendar={TIMER_FIRES}\n"
# Sorts after the renderer's 50-homelab.conf, so a packaged drop-in cannot undo it.
TEST_DROPIN = "90-test.conf"


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


def case_dropins(package) -> dict[str, str]:
    """Each unit of `package` the case puts a drop-in on, mapped to the drop-in's text.

    The timers are those the package ships and those it enables, such as the updater's.
    """
    timers = {
        unit
        for unit in (*package.units(), *package.enable_links())
        if unit.endswith(".timer")
    }
    return {
        **dict.fromkeys(timers, TIMER_DROPIN),
        **dict.fromkeys(STUBBED_UNITS, STUB_DROPIN),
        **dict.fromkeys(STUBBED_ONESHOTS, ONESHOT_DROPIN),
    }


def seed_node_host(c: Container, dropins: dict[str, str]) -> None:
    """Seed in container `c` the host state a cluster node has and the container lacks.

    The nodes ship the Pi's sshd, ufw, auditd, postfix, hosts and modules-load subjects, so they
    get `seed_pi_host`, which unmounts /etc/resolv.conf: no name resolves after it, so every
    fetch from the archive comes first. A node adds the Ubuntu server packages in
    SERVER_PACKAGES, the dotfiles tools in DOTFILES_TOOLS, `udevadm` and `virsh` as stubs that
    exit 0,
    the agent's clones in AGENT_CLONES, and `dropins`, from `case_dropins`.
    """
    c.ok(
        f"DEBIAN_FRONTEND=noninteractive {APT_INSTALL}"
        f" {' '.join(SERVER_PACKAGES)} < /dev/null"
    )
    seed_pi_host(c)
    for path in (*DOTFILES_TOOLS, UDEVADM_STUB, VIRSH_STUB):
        _put(c, path, EXIT_0, "0755")
    c.ok("chown -R ubuntu:ubuntu /home/ubuntu/.local")
    c.ok(f"install -d {' '.join(AGENT_CLONES)}")
    for unit, dropin in dropins.items():
        _put(c, f"/etc/systemd/system/{unit}.d/{TEST_DROPIN}", dropin, "0644")
    c.ok("systemctl daemon-reload")


def mask_gettys(c: Container) -> None:
    """Mask the GETTY_TEMPLATES in container `c`, and clear what their instances did before."""
    patterns = " ".join(f"'{t.replace('@.', '@*.')}'" for t in GETTY_TEMPLATES)
    c.ok(f"systemctl mask {' '.join(GETTY_TEMPLATES)}")
    c.ok(f"systemctl stop {patterns} && systemctl reset-failed {patterns}")


@contextmanager
def _timed(cases, step: str) -> Generator[None]:
    """Record the seconds the body takes in `cases.case_seconds` under `step`, even on a raise."""
    start = time.monotonic()
    try:
        yield
    finally:
        cases.case_seconds[step] = time.monotonic() - start


def _seal_and_build(cases, c: Container, host: str):
    """Seal node `host`'s package, build it, and return it with the `.deb` paths apt installs."""
    seal_dir = seal_host(cases, c, host)
    head = seal_dir.parent / "head"
    package = host_mode.render_host(host, sops_dir=seal_dir)
    host_mode.write_packages([package], head)
    deb = build(head / "hosts" / host, cases.versions["pi-head"], head.parent / "deb")
    cases.add(f"{host}-head", deb, name=f"sealed-{c.name}-{deb.name}")
    debs = " ".join(
        f"/srv/debs/{cases.debs[label].name}"
        for label in ("updater-head", f"{host}-head", *cases.tools)
    )
    return package, debs


def _install(cases, c, host: str, seal_and_build=_seal_and_build) -> None:
    """Install node `host`'s package in `c` and assert no unit failed.

    Each step's seconds go to `cases.case_seconds`, which `case_groups` names on the `[DONE]`
    line. `seal_and_build` is the seam a test replaces.
    """
    with _timed(cases, "mask gettys"):
        mask_gettys(c)
    with _timed(cases, "seal and build"):
        package, debs = seal_and_build(cases, c, host)
    installs = f"{debs} {' '.join(BUS_PACKAGES)}"
    # The Depends are fetched while names still resolve; the seed takes resolv.conf away.
    with _timed(cases, "download"):
        c.ok(f"{APT_INSTALL} --download-only {installs} < /dev/null")
    with _timed(cases, "seed"):
        seed_node_host(c, case_dropins(package))
    # A shell timeout under the exec's own, so a hung step still returns what it printed.
    with _timed(cases, "apt install"):
        c.ok(
            f"DEBIAN_FRONTEND=noninteractive timeout {STEP_SECONDS} {APT_INSTALL} {installs}"
            " < /dev/null"
        )
    # Joins the reconcile the postinst queued, and returns once it ends.
    with _timed(cases, "reconcile"):
        c.sh(f"timeout {STEP_SECONDS} systemctl start homelab-reconcile.service")
    with _timed(cases, "settle"):
        c.ok(f"sleep {SETTLE_SECONDS}")
    with _timed(cases, "check"):
        problems = unit_problems(c.ok(f"systemctl show -p {UNIT_PROPERTIES} '*'"))
        for unit in problems:
            # Reconcile logs its errors at priority 3, often above the tail's last 40 lines.
            journal = c.sh(
                f"journalctl -u {unit} -p warning --no-pager -o short-monotonic;"
                f" echo '--- the last 40 lines ---';"
                f" journalctl -u {unit} -n 40 --no-pager -o short-monotonic"
            )
            print(
                f"::group::{unit} journal\n{journal.stdout}::endgroup::",
                file=sys.stderr,
            )
    expect(
        not problems,
        "; ".join(f"{unit} {problem}" for unit, problem in problems.items()),
    )


def run(cases, host: str) -> None:
    """Install node `host`'s package in a container of its own, and record the verdict on `cases`.

    `case_groups.groups` makes each of NODES a group of its own: one node's install runs about
    as long as the longest other group, so two in turn would be the leg's longest group.
    """
    name = NAME.format(host=host)
    with _timed(cases, "boot"):
        c = cases.boot(f"node-{host}", hostname=host)
    if c is None:
        return
    try:
        _install(cases, c, host)
    except (CaseFailed, subprocess.SubprocessError) as exc:
        cases.failed.append(name)
        print(f"[FAIL] {c.name}: {name}: {exc}", file=sys.stderr)
    else:
        cases.passed.append(name)
    finally:
        with _timed(cases, "remove"):
            c.remove()
