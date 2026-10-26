"""The units each host package spec ships, for the `host-package-units-are-named-homelab` row.

Kept beside `test_census_rows_text.py` so that module stays under its length allowlist.
"""

from pathlib import PurePosixPath

from _helpers import REPO
from _row_table import Subject, tracked
from lib import yaml_fast
from renderer.renderer_lib import host as host_mode


def host_package_units() -> list[Subject]:
    """One subject per unit a host spec ships: its name as `rel`, the spec's path as `text`.

    A unit is a file directly under `UNIT_DIR` with a unit suffix, as `Package.units()` reads
    it, or a unit an `enable:` map links from a `<target>.wants/` directory. A drop-in under
    `<unit>.d/` is not one: it may extend another package's unit, which keeps its own name.
    """
    units = []
    for rel in tracked("hosts/*package.yml", "hosts/*component.yml"):
        spec = yaml_fast.safe_load((REPO / rel).read_text()) or {}
        # A unit the spec hands over from /etc/systemd/system keeps its Ansible-era name on
        # purpose: reconcile removes the shadowing copy and restarts the unit (h7, *Ruling 3*,
        # 2026-10-24), which a rename could not do for a running service that holds a port.
        handed = {
            PurePosixPath(str(entry.get("path", entry.get("unit", "")))).name
            for entry in spec.get("legacy") or []
            if str(entry.get("path", "")).startswith("/etc/systemd/system/")
            or "unit" in entry
        }
        units.extend(
            Subject(str(unit), rel)
            for unit in spec.get("enable") or {}
            if str(unit) not in handed
        )
        for entry in spec.get("files") or []:
            dst = PurePosixPath(str(entry.get("dst", "")))
            if (
                dst.parent == host_mode.UNIT_DIR
                and dst.name.endswith(host_mode.UNIT_SUFFIXES)
                and dst.name not in handed
            ):
                units.append(Subject(dst.name, rel))
    return units
