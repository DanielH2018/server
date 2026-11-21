#!/usr/bin/env python3
"""The container case for the cluster nodes' units that lean on an Ubuntu release's packages.

No other case installs daniel-server's or daniel-box's package for real: `bootstrap_case`
simulates the install, so none of their units ever starts. This case puts one shipped file at a
time onto the Ubuntu package it overrides, on each `host_packages` leg's release, and starts it:

  * nut-client, with the `nut-secondary` drop-in on nut-monitor.service and an upsmon.conf at
    the drop-in's NUT_CONFPATH. The unit must come up active with the upsmon daemon as its main
    process, not the MODE wrapper 24.04 ships at /usr/sbin/upsmon.

It covers only the files it names, not the rest of either node's package.
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
NUT_PROPERTIES = ("ActiveState", "SubState", "Result", "ExecMainStatus", "NRestarts")
START_SECONDS = 60


def nut_monitor_problems(show: dict[str, str], exe: str) -> list[str]:
    """What keeps nut-monitor from being the running upsmon daemon, [] when nothing does.

    Args:
        show: `systemctl show` for NUT_PROPERTIES, property to value.
        exe: Where the unit's main process's /proc/<pid>/exe points, "" for no process.
    """
    problems = []
    if show.get("ActiveState") != "active":
        problems.append(
            f"nut-monitor is {show.get('ActiveState')}/{show.get('SubState')}"
            f" (result {show.get('Result')}, exit status {show.get('ExecMainStatus')})"
        )
    if show.get("NRestarts", "0") != "0":
        problems.append(f"nut-monitor restarted {show['NRestarts']} times")
    if not exe.endswith("/upsmon"):
        problems.append(
            f"nut-monitor's main process runs {exe or 'nothing'}, not upsmon"
        )
    return problems


def _put(c: Container, path: str, text: str, mode: str, group: str = "root") -> None:
    c.ok(
        f"install -d {shlex.quote(path.rsplit('/', 1)[0])} && printf %s {shlex.quote(text)}"
        f" > {path} && chown root:{group} {path} && chmod {mode} {path}"
    )


def _nut_monitor(c: Container) -> None:
    c.ok("DEBIAN_FRONTEND=noninteractive apt-get install -y nut-client < /dev/null")
    _put(c, NUT_DROPIN, NUT_DROPIN_SOURCE.read_text(), "0644")
    _put(c, NUT_CONF, NUT_CONF_TEXT, "0640", group="nut")
    c.ok("systemctl daemon-reload")
    c.sh("systemctl reset-failed nut-monitor.service")
    # --no-block: on 26.04 the unit is Type=notify, and a start that never notifies would hold
    # this call for the whole TimeoutStartSec.
    c.ok("systemctl restart --no-block nut-monitor.service")
    c.sh(
        f"for _ in $(seq {START_SECONDS}); do"
        ' [ "$(systemctl show -P ActiveState nut-monitor.service)" = active ] && break;'
        " sleep 1; done; sleep 3"
    )
    out = c.ok(f"systemctl show -p {','.join(NUT_PROPERTIES)} nut-monitor.service")
    show = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
    pid = c.ok("systemctl show -P MainPID nut-monitor.service").strip()
    exe = c.sh(f"readlink /proc/{pid}/exe").stdout.strip() if pid != "0" else ""
    problems = nut_monitor_problems(show, exe)
    if problems:
        journal = c.sh("journalctl -u nut-monitor.service -n 40 --no-pager").stdout
        print(f"::group::nut-monitor journal\n{journal}::endgroup::", file=sys.stderr)
    expect(not problems, "; ".join(problems))


def run(cases) -> None:
    """Boot a container, run the case, and record the verdict on `cases`."""
    c = cases.boot("node-units")
    if c is None:
        return
    try:
        _nut_monitor(c)
    except (CaseFailed, subprocess.SubprocessError) as exc:
        cases.failed.append(NAME)
        print(f"[FAIL] {c.name}: {NAME}: {exc}", file=sys.stderr)
    else:
        cases.passed.append(NAME)
    finally:
        c.remove()
