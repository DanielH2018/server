"""The textfile metrics reconcile owns: reboot-pending, activation-pending and drift.

`homelab_host.prom` belongs to the updater, so reconcile writes `homelab_host_reconcile.prom`
beside it, with M3's write rule: mktemp in the directory, chmod 0644, rename. A writer never
creates the textfile directory, so a host whose exporter is not set up writes nothing here and
the updater, which does fail on that, reports it.

    homelab_host_reboot_pending{host}             1 while /run/homelab/reboot-pending exists
    homelab_host_reboot_pending_since_seconds{host}  that flag's mtime, only while it exists
    homelab_host_activation_pending{host,unit}    1 per unit still owed a restart
    homelab_host_reconcile_drift{host,arm}        items an arm left alone this run
    homelab_host_unit_started_seconds{host,unit}  when reconcile last started or restarted the unit

Drift is not a failure. H3's `host_packages` check names a non-zero count in its message.

`homelab-reconcile-metrics.service` rewrites the file at boot from the record, so the
reboot-pending series reads 0 after the boot that cleared the flag (#6188). A unit's start time
lets a rule catch a unit whose own result predates the restart reconcile gave it (#6342).
"""

import os
import tempfile
from pathlib import Path

from reconcile_lib.activate import REBOOT_PENDING
from reconcile_lib.common import ROOT, say

TEXTFILE_DIR = ROOT / "var/lib/node-exporter-textfile"
PROM = "homelab_host_reconcile.prom"


def label(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def render(host: str, state: dict) -> str:
    """The metric file for `host` and the record `state` the run just wrote."""
    lines = [
        f'homelab_host_reboot_pending{{host="{label(host)}"}} {int(REBOOT_PENDING.exists())}'
    ]
    if REBOOT_PENDING.exists():
        since = int(REBOOT_PENDING.stat().st_mtime)
        lines.append(
            f'homelab_host_reboot_pending_since_seconds{{host="{label(host)}"}} {since}'
        )
    lines += [
        f'homelab_host_activation_pending{{host="{label(host)}",unit="{label(unit)}"}} 1'
        for unit in sorted(state.get("pending") or {})
    ]
    lines += [
        f'homelab_host_reconcile_drift{{host="{label(host)}",arm="{label(arm)}"}} {count}'
        for arm, count in sorted((state.get("drift") or {}).items())
    ]
    lines += [
        f'homelab_host_unit_started_seconds{{host="{label(host)}",unit="{label(unit)}"}} {when}'
        for unit, when in sorted((state.get("started") or {}).items())
    ]
    return "\n".join(lines) + "\n"


def write(host: str, state: dict) -> None:
    """Write the metrics file; say why and write nothing when the directory is missing."""
    if not TEXTFILE_DIR.is_dir():
        say(f"{TEXTFILE_DIR} is missing; writing no metrics")
        return
    try:
        fd, tmp = tempfile.mkstemp(dir=TEXTFILE_DIR, prefix=f".{PROM}.")
        with os.fdopen(fd, "w") as handle:
            handle.write(render(host, state))
        Path(tmp).chmod(0o644)
        os.replace(tmp, TEXTFILE_DIR / PROM)
    except OSError as exc:
        say(f"cannot write {PROM}: {exc}")
