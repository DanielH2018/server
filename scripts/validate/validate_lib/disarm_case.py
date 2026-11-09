#!/usr/bin/env python3
"""The container case for H3.8b's recovery disarm, which stops the Pi's recovery timer with no reboot.

`scripts/validate/host_package_cases.py` runs it after the by-hand case. It installs the real Pi
package by hand, then upgrades to one whose recovery timer is disarmed. The disarmed build drops
the timer's enable link, and reconcile step 1 stops a timer whose link the new manifest dropped.
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

from validate.validate_lib import secrets_case
from validate.validate_lib.host_container import expect
from validate.validate_lib.host_fixtures import RECOVERY_TIMER


def run(cases) -> None:
    """Boot a container, run the case, and record the verdict on `cases`."""
    c = cases.boot("disarm")
    if c is None:
        return

    def body() -> None:
        secrets_case.seal_pi(cases, c)
        debs = " ".join(
            f"/srv/debs/{cases.debs[label].name}"
            for label in ("updater-head", "pi-head", *cases.tools)
        )
        c.ok(f"DEBIAN_FRONTEND=noninteractive apt-get install -y {debs}")
        c.ok("systemctl start homelab-reconcile.service")
        cases.expect_reconciled(c, "updater-head", "pi-head")
        state = c.active(RECOVERY_TIMER)
        expect(state == "active", f"{RECOVERY_TIMER} is {state} while armed")
        cases.serving("updater-head", "pi-head", "pi-disarmed", *cases.tools)
        status = c.run_updater()
        expect(status == 0, f"the updater exited {status}")
        cases.expect_installed(c, "updater-head", "pi-disarmed")
        cases.expect_reconciled(c, "updater-head", "pi-disarmed")
        state = c.active(RECOVERY_TIMER)
        expect(state == "inactive", f"{RECOVERY_TIMER} is {state} once disarmed")

    try:
        cases.case(c, "disarm the recovery timer by dropping its enable link", body)
    finally:
        c.remove()
