#!/usr/bin/env python3
"""The container case for the cluster nodes' units that lean on an Ubuntu release's packages.

`node_package_case` installs daniel-server's and daniel-box's whole packages and reads only
whether a unit failed. This case checks two shipped files closer: it puts each onto the Ubuntu
package it overrides, on each `host_packages` leg's release, starts it, and reads what it did:

  * nut-client, with the `nut-secondary` drop-in on nut-monitor.service and an upsmon.conf at
    the drop-in's NUT_CONFPATH. The unit must come up active with upsmon's root parent as its
    main process, not the MODE wrapper 24.04 ships at /usr/sbin/upsmon nor the child upsmon runs
    as user nut. Killing that child must make systemd restart the unit once.
  * netplan.io, under the `homelab-netplan-wifi` installer, run as root on a scratch root with
    the real netplan. It must install a valid file and keep the old one for a file netplan
    cannot parse and for one only its generator refuses (a short passphrase).

It covers only the files it names. Each check runs even
when the one before it failed, so one leg reports every check that fails.
"""

import shlex
import subprocess
import sys

from lib.repo_paths import REPO
from validate.validate_lib.host_container import CaseFailed, Container, expect

NAME = "start the cluster nodes' release-dependent units on this release"
NUT_DROPIN = "/usr/lib/systemd/system/nut-monitor.service.d/50-homelab.conf"
NUT_DROPIN_SOURCE = REPO / "hosts/components/nut-secondary/units/50-homelab.conf"
NUT_CONF = "/var/lib/homelab-secrets/etc/nut/upsmon.conf"
# No upsd answers, so upsmon keeps polling, and the SHUTDOWNCMD keeps a stray FSD off PID 1.
NUT_CONF_TEXT = (
    "MONITOR apc-ups@127.0.0.1 1 upsmon-case not-a-secret secondary\n"
    "MINSUPPLIES 1\n"
    'SHUTDOWNCMD "/bin/true"\n'
)
NUT_PROPERTIES = (
    "ActiveState",
    "SubState",
    "Result",
    "ExecMainStatus",
    "NRestarts",
    "MainPID",
)
START_SECONDS = 60
WIFI_SCRIPT = "/usr/local/lib/node-units-case/homelab-netplan-wifi"
WIFI_SCRIPT_SOURCE = REPO / "hosts/components/k3s-agent/files/homelab-netplan-wifi"
WIFI_NAME = "80-homelab-wifi-fallback.yaml"
WIFI_OLD = "old\n"


def _wifi(password: str) -> str:
    return (
        "network:\n  version: 2\n  wifis:\n    wlan0:\n      optional: true\n"
        f'      dhcp4: true\n      access-points:\n        "case-ssid":\n'
        f'          password: "{password}"\n'
    )


# Each rendered file, and whether the installer must install it.
WIFI_FILES = {
    "valid": (_wifi("case-passphrase"), True),
    "unparsable": ("network:\n  version: 2\n  wifis:\n    wlan0: [\n", False),
    "short passphrase": (_wifi("short"), False),
}


def nut_monitor_problems(
    show: dict[str, str], exe: str, uid: str, restarts: int = 0
) -> list[str]:
    """What keeps nut-monitor from being the running upsmon parent, [] when nothing does.

    Args:
        show: `systemctl show` for NUT_PROPERTIES, property to value.
        exe: Where the unit's main process's /proc/<pid>/exe points, "" for no process.
        uid: The main process's uid, "" for no process.
        restarts: How many times systemd must have restarted the unit.
    """
    problems = []
    if show.get("ActiveState") != "active":
        problems.append(
            f"nut-monitor is {show.get('ActiveState')}/{show.get('SubState')}"
            f" (result {show.get('Result')}, exit status {show.get('ExecMainStatus')})"
        )
    if show.get("NRestarts", "0") != str(restarts):
        problems.append(
            f"nut-monitor restarted {show.get('NRestarts', '0')} times, not {restarts}"
        )
    if not exe.endswith("/upsmon"):
        problems.append(
            f"nut-monitor's main process runs {exe or 'nothing'}, not upsmon"
        )
    elif uid != "0":
        problems.append(f"nut-monitor's main process runs as uid {uid}, not root")
    return problems


def wifi_copy_problems(results: dict[str, tuple[int, str]]) -> list[str]:
    """What the installer did wrong with WIFI_FILES, [] when nothing.

    Args:
        results: Each WIFI_FILES name mapped to the installer's exit status and the installed
            file's text after the run.
    """
    problems = []
    for name, (rendered, accepted) in WIFI_FILES.items():
        status, installed = results[name]
        want = (0, rendered) if accepted else (1, WIFI_OLD)
        if (status, installed) != want:
            kept = "installed it" if installed == rendered else "kept the old file"
            problems.append(
                f"the {name} file: the installer exited {status} and {kept}"
            )
    return problems


def _put(c: Container, path: str, text: str, mode: str, group: str = "root") -> None:
    c.ok(
        f"install -d {shlex.quote(path.rsplit('/', 1)[0])} && printf %s {shlex.quote(text)}"
        f" > {path} && chown root:{group} {path} && chmod {mode} {path}"
    )


def _await_active(c: Container, restarts: int) -> None:
    c.sh(
        f"for _ in $(seq {START_SECONDS}); do"
        ' [ "$(systemctl show -P ActiveState nut-monitor.service)" = active ] &&'
        f' [ "$(systemctl show -P NRestarts nut-monitor.service)" = {restarts} ] && break;'
        " sleep 1; done; sleep 3"
    )


def _expect_running(c: Container, restarts: int) -> str:
    """Raise CaseFailed unless nut-monitor is the running upsmon parent; return its MainPID."""
    out = c.ok(f"systemctl show -p {','.join(NUT_PROPERTIES)} nut-monitor.service")
    show = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
    pid = show.get("MainPID", "0")
    exe = c.sh(f"readlink /proc/{pid}/exe").stdout.strip() if pid != "0" else ""
    uid = c.sh(f"stat -c %u /proc/{pid}").stdout.strip() if pid != "0" else ""
    problems = nut_monitor_problems(show, exe, uid, restarts)
    if problems:
        journal = c.sh("journalctl -u nut-monitor.service -n 40 --no-pager").stdout
        print(f"::group::nut-monitor journal\n{journal}::endgroup::", file=sys.stderr)
    expect(not problems, "; ".join(problems))
    return pid


def _nut_monitor(c: Container) -> None:
    c.ok("DEBIAN_FRONTEND=noninteractive apt-get install -y nut-client < /dev/null")
    _put(c, NUT_DROPIN, NUT_DROPIN_SOURCE.read_text(), "0644")
    _put(c, NUT_CONF, NUT_CONF_TEXT, "0640", group="nut")
    c.ok("systemctl daemon-reload")
    c.sh("systemctl reset-failed nut-monitor.service")
    # --no-block: a start that hangs, such as a Type=notify unit that never notifies, would hold
    # this call for the whole TimeoutStartSec.
    c.ok("systemctl restart --no-block nut-monitor.service")
    _await_active(c, restarts=0)
    main = _expect_running(c, restarts=0)
    c.ok(
        "cg=/sys/fs/cgroup$(systemctl show -P ControlGroup nut-monitor.service);"
        f" for p in $(cat $cg/cgroup.procs); do [ $p = {main} ] || kill -KILL $p; done"
    )
    _await_active(c, restarts=1)
    _expect_running(c, restarts=1)


def _netplan_wifi(c: Container) -> None:
    c.ok("DEBIAN_FRONTEND=noninteractive apt-get install -y netplan.io < /dev/null")
    _put(c, WIFI_SCRIPT, WIFI_SCRIPT_SOURCE.read_text(), "0755")
    results = {}
    for index, (name, (rendered, _)) in enumerate(WIFI_FILES.items()):
        root = f"/tmp/node-units-wifi-{index}"
        _put(c, f"{root}/var/lib/homelab-secrets/netplan/{WIFI_NAME}", rendered, "0600")
        _put(c, f"{root}/etc/netplan/{WIFI_NAME}", WIFI_OLD, "0600")
        result = c.sh(f"HOMELAB_ROOT={root} {WIFI_SCRIPT}")
        if result.returncode not in (0, 1):
            print(
                f"{c.name}: the {name} file: {result.stderr.strip()}", file=sys.stderr
            )
        results[name] = (result.returncode, c.read(f"{root}/etc/netplan/{WIFI_NAME}"))
    problems = wifi_copy_problems(results)
    expect(not problems, "; ".join(problems))


def run(cases) -> None:
    """Boot a container, run every check, and record the case's verdict on `cases`."""
    c = cases.boot("node-units")
    if c is None:
        return
    failures = []
    try:
        for check in (_nut_monitor, _netplan_wifi):
            try:
                check(c)
            except (CaseFailed, subprocess.SubprocessError) as exc:
                failures.append(str(exc))
    finally:
        c.remove()
    if failures:
        cases.failed.append(NAME)
        print(f"[FAIL] {c.name}: {NAME}: {'; '.join(failures)}", file=sys.stderr)
    else:
        cases.passed.append(NAME)
