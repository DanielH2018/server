"""The units each host package spec ships, for the `host-package-units-are-named-homelab` row.

Kept beside `test_census_rows_text.py` so that module stays under its length allowlist.
"""

from pathlib import PurePosixPath

from _helpers import REPO
from _row_table import Subject, tracked
from lib import yaml_fast
from renderer.renderer_lib import host as host_mode

# Units that kept their Ansible-era name: reconcile handed each over from /etc/systemd/system,
# which a rename could not do for a running service that holds a port (h7, *Ruling 3*,
# 2026-10-24), or a hook, poller or polkit rule starts it by name.
KEPT_NAMES = frozenset(
    {
        "claude-stop-alert@.service",
        "claude-triage-report-alert@.service",
        "claude-triage@.service",
        "coredns-host.service",
        "deploy-ui.service",
        "fanout-alert@.service",
        "node_exporter.service",
        "renovate-agent-land@.service",
        "worktree-holders.socket",
        "worktree-holders@.service",
    }
)


def host_package_units() -> list[Subject]:
    """One subject per unit a host spec ships: its name as `rel`, the spec's path as `text`.

    A unit is a file directly under `UNIT_DIR` with a unit suffix, as `Package.units()` reads
    it, or a unit an `enable:` map links from a `<target>.wants/` directory. A drop-in under
    `<unit>.d/` is not one: it may extend another package's unit, which keeps its own name.
    """
    units = []
    for rel in tracked("hosts/*package.yml", "hosts/*component.yml"):
        spec = yaml_fast.safe_load((REPO / rel).read_text()) or {}
        units.extend(
            Subject(str(unit), rel)
            for unit in spec.get("enable") or {}
            if str(unit) not in KEPT_NAMES
        )
        for entry in spec.get("files") or []:
            dst = PurePosixPath(str(entry.get("dst", "")))
            if (
                dst.parent == host_mode.UNIT_DIR
                and dst.name.endswith(host_mode.UNIT_SUFFIXES)
                and dst.name not in KEPT_NAMES
            ):
                units.append(Subject(dst.name, rel))
    return units
