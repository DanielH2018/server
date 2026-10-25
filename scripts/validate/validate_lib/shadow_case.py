#!/usr/bin/env python3
"""The container case for a packaged unit that an Ansible-era copy under /etc shadows (H7.41a).

A copy in /etc/systemd/system wins over the same name in /usr/lib/systemd/system, so a package
that ships `homelab-*` units already installed by Ansible changes nothing until reconcile removes
the copy. `scripts/validate/host_package_cases.py` runs this case after the conffile case. The case:

  * writes /etc/systemd/system/homelab-fixture-shadow.service, an old copy of a unit the
    `shadow` fixture ships, and starts it; and writes homelab-fixture-orphan.service, a copy
    of a unit the fixture does not ship;
  * installs the updater and the fixture, whose legacy entries name both files, and runs
    reconcile;
  * asserts the shadowing copy is gone, the unit's fragment is the packaged one, the packaged
    unit runs its packaged command, and the orphan is still in place.
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

import shutil
import subprocess
import sys

from validate.validate_lib.host_container import CaseFailed, Container, expect
from validate.validate_lib.host_fixtures import build, render_fixture

NAME = "a packaged unit replaces the Ansible-era copy that shadows it"
UNIT = "homelab-fixture-shadow.service"
ORPHAN = "homelab-fixture-orphan.service"
ETC = "/etc/systemd/system"
PACKAGED = f"/usr/lib/systemd/system/{UNIT}"
OLD_UNIT = "[Service]\nExecStart=/usr/bin/sleep 1000\n"


def _body(cases, c: Container) -> None:
    build_root = cases.work / "build"
    rendered = render_fixture("shadow", build_root / "shadow")
    deb = build(
        rendered, f"{cases.versions['updater-head']}.7", build_root / "shadow-deb"
    )
    host = cases.work / "debs" / deb.name
    shutil.copy(deb, host)
    updater = cases.debs["updater-head"]
    for name in (UNIT, ORPHAN):
        c.ok(f"printf '%b' {OLD_UNIT!r} > {ETC}/{name}")
    c.ok(f"systemctl daemon-reload && systemctl start {UNIT}")
    expect(
        "sleep 1000" in c.ok(f"systemctl show -P ExecStart {UNIT}"),
        "the old copy did not start under /etc",
    )
    c.ok(
        "DEBIAN_FRONTEND=noninteractive apt-get install -y"
        f" /srv/debs/{updater.name} /srv/debs/{host.name}"
    )
    # Joins the start the postinst queued, then runs reconcile once more to its end.
    c.ok("systemctl start homelab-reconcile.service")
    c.ok("systemctl start homelab-reconcile.service")
    expect(not c.exists(f"{ETC}/{UNIT}"), f"{ETC}/{UNIT} is still there")
    fragment = c.ok(f"systemctl show -P FragmentPath {UNIT}").strip()
    expect(fragment == PACKAGED, f"{UNIT} loads {fragment}, not {PACKAGED}")
    command = c.ok(f"systemctl show -P ExecStart {UNIT}")
    expect("infinity" in command, f"{UNIT} still runs its old command: {command}")
    expect(c.active(UNIT) == "active", f"{UNIT} is {c.active(UNIT)}, not active")
    # The reject half: no packaged unit of that name, so the file is the only copy.
    expect(c.exists(f"{ETC}/{ORPHAN}"), f"{ETC}/{ORPHAN} was removed")


def run(cases) -> None:
    """Boot a container, run the case, and record the verdict on `cases`."""
    c = cases.boot("shadow")
    if c is None:
        return
    try:
        _body(cases, c)
    except (CaseFailed, subprocess.SubprocessError) as exc:
        cases.failed.append(NAME)
        print(f"[FAIL] {c.name}: {NAME}: {exc}", file=sys.stderr)
        journal = c.sh(
            "journalctl -u homelab-reconcile.service -n 60 --no-pager -o short-monotonic"
        ).stdout
        print(f"::group::journal after {NAME}\n{journal}::endgroup::", file=sys.stderr)
    else:
        print(f"[PASS] {c.name}: {NAME}", file=sys.stderr)
    finally:
        c.remove()
