"""Writing rendered packages to disk: `host.write_packages` and the tree it lays out.

`host.py` renders a `Package` and re-exports `write_packages` from here, so its callers and
tests read it through `host_mode`. A package's tree holds `root/`, the files it installs,
`scripts/`, its maintainer scripts, `nfpm.yaml`, and the tool pins of a package with tools.
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

import json
from pathlib import Path

from renderer.renderer_lib.cluster import RenderRefused
from renderer.renderer_lib.host_package import Package
from renderer.renderer_lib.host_tools import ARCHES, PINS_FILE


def write_packages(packages: list[Package], out: Path) -> list[Path]:
    """Write each package's `root/` and `nfpm.yaml` under `out`; return the files written.

    Raises:
        RenderRefused: A package directory already exists under `out`. Nothing is written.
    """
    for package in packages:
        if (out / package.directory).exists():
            raise RenderRefused(
                f"{out / package.directory} exists; render into an empty --out"
            )
    written = []
    for package in packages:
        # A package with tools renders once per architecture, so nfpm runs once for each.
        trees = (
            [(out / package.directory / arch, arch) for arch in ARCHES]
            if package.tools
            else [(out / package.directory, "all")]
        )
        for base, arch in trees:
            written += _write_tree(package, base, arch)
    return written


def _write_tree(package: Package, base: Path, arch: str) -> list[Path]:
    """Write one package tree, `root/`, `scripts/`, `nfpm.yaml` and any tool pins, at `base`."""
    written = []
    base.mkdir(parents=True, exist_ok=True)
    for dst, (content, mode) in sorted(package.files.items()):
        path = base / "root" / dst.lstrip("/")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        path.chmod(mode)
        written.append(path)
    for dst, target in sorted(package.links.items()):
        path = base / "root" / dst.lstrip("/")
        path.parent.mkdir(parents=True, exist_ok=True)
        # A host package's link may point at the updater's unit, absent from this root.
        path.symlink_to(target)
        written.append(path)
    for name, content in sorted(package.scripts.items()):
        path = base / "scripts" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        path.chmod(0o755)
        written.append(path)
    (base / "nfpm.yaml").write_text(package.nfpm_spec(arch))
    written.append(base / "nfpm.yaml")
    if package.tools:
        pins = {tool: arches[arch].as_json() for tool, arches in package.tools.items()}
        (base / PINS_FILE).write_text(json.dumps(pins, indent=2, sort_keys=True) + "\n")
        written.append(base / PINS_FILE)
    return written
