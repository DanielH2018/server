"""The manifest `homelab-reconcile` reads, and the two spec keys that feed it.

A host package installs `/usr/lib/homelab/manifest.json` (H3, *Shared names*), in the schema
`hosts/lib/homelab-reconcile`'s docstring documents:

- `units` lists every unit the host package and `homelab-updater` install, and every unit the
  host package drops a `50-homelab.conf` into, each mapped to its policy or `{}`;
- `enable_links` lists each unit the host package installs under a `<target>.wants/`
  directory;
- `legacy` holds the host package's legacy entries, each value rendered;
- `reconcilers` is empty until H4.

`units:` and `legacy:` in a `package.yml` or `component.yml` feed it. The version and SHA stay
out of it, so a render changes only when the package's content does (H3, *Package rules*).
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from renderer.renderer_lib.cluster import RenderRefused


DROPIN = "50-homelab.conf"
# The manifest `homelab-reconcile` reads (H3, *Shared names*). Only a host package installs it.
MANIFEST = "/usr/lib/homelab/manifest.json"
# The shared package every host installs beside its own. Reconcile manages its units too, so
# a host's manifest lists them.
UPDATER = "homelab-updater"
POLICIES = frozenset({"manual", "windowed"})
# Each legacy entry kind, by its key set, as `homelab-reconcile:valid_legacy` reads it.
LEGACY_KINDS = (
    frozenset({"path"}),
    frozenset({"cron", "user"}),
    frozenset({"cron_file"}),
    frozenset({"unit"}),
)
_UNIT_NAME = re.compile(r"[A-Za-z0-9@_.:-]+\.(service|timer|socket|path)")
_CRON_FILE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")


class Package(Protocol):
    """The part of `host.Package` the manifest reads, named here so neither module imports the other."""

    name: str
    files: dict[str, tuple[bytes, int]]
    policies: dict[str, str]
    legacy: list[dict[str, str]]

    def units(self) -> list[str]: ...

    def dropin_targets(self) -> list[str]: ...

    def enable_links(self) -> list[str]: ...


def manifest(package: Package, updater: Package | None) -> dict:
    """The manifest `homelab-reconcile` reads, for a host package and the updater beside it.

    Raises:
        RenderRefused: A policy names a unit neither package installs or drops in on.
    """
    shared_units = updater.units() if updater else []
    managed = set(package.units() + package.dropin_targets() + shared_units)
    policies = {**(updater.policies if updater else {}), **package.policies}
    stray = sorted(set(policies) - managed)
    if stray:
        raise RenderRefused(
            f"{package.name}: units {stray} have a policy, but no package installs them "
            "or drops in on them"
        )
    return {
        "units": {
            unit: {"policy": policies[unit]} if unit in policies else {}
            for unit in sorted(managed)
        },
        "enable_links": package.enable_links(),
        "legacy": package.legacy,
        # H4 adds the first reconciler kind; reconcile fails a run that lists an unknown one.
        "reconcilers": [],
    }


def add_manifest(package: Package, updater: Package | None) -> None:
    """Install the manifest in `package`, a host package with `updater` beside it."""
    if MANIFEST in package.files:
        raise RenderRefused(f"{package.name}: {MANIFEST} is the rendered manifest")
    text = json.dumps(manifest(package, updater), indent=1, sort_keys=True) + "\n"
    package.files[MANIFEST] = (text.encode(), 0o644)


def add_policies(package: Package, directory: Path, units: object) -> None:
    """Record in `package` each unit's restart policy from a spec's `units:` map.

    Raises:
        RenderRefused: The map is malformed, or a second spec gives a unit a policy.
    """
    if not isinstance(units, dict):
        raise RenderRefused(f"{directory}: units {units!r} is not a map")
    for unit, spec in units.items():
        policy = spec.get("policy") if isinstance(spec, dict) else None
        if (
            not isinstance(unit, str)
            or not _UNIT_NAME.fullmatch(unit)
            or not isinstance(spec, dict)
            or set(spec) != {"policy"}
            or policy not in POLICIES
        ):
            raise RenderRefused(
                f"{directory}: units entry {unit!r}: {spec!r} is not a unit name mapped "
                f"to {{policy: {' | '.join(sorted(POLICIES))}}}"
            )
        if unit in package.policies:
            raise RenderRefused(f"{package.name}: two specs give {unit} a policy")
        package.policies[unit] = policy


def legacy_entry(
    entry: object, directory: Path, render: Callable[[str], str]
) -> dict[str, str]:
    """One legacy entry, with each value passed through `render`.

    Raises:
        RenderRefused: The entry is of no kind reconcile knows, or reconcile would refuse a
            rendered value.
    """
    if not isinstance(entry, dict) or frozenset(entry) not in LEGACY_KINDS:
        raise RenderRefused(
            f"{directory}: legacy entry {entry!r} is not one of "
            f"{[sorted(kind) for kind in LEGACY_KINDS]}"
        )
    rendered = {key: render(str(value)) for key, value in entry.items()}
    if not _valid_legacy(rendered):
        raise RenderRefused(f"{directory}: legacy entry {rendered!r} is malformed")
    return rendered


def _valid_legacy(entry: dict[str, str]) -> bool:
    """The value checks `homelab-reconcile:valid_legacy` makes, so a render it refuses fails here."""
    if "path" in entry:
        path = entry["path"]
        return path.startswith("/") and ".." not in path.split("/")
    if "cron" in entry:
        return all(value and "\n" not in value for value in entry.values())
    if "cron_file" in entry:
        return bool(_CRON_FILE.fullmatch(entry["cron_file"]))
    return bool(_UNIT_NAME.fullmatch(entry["unit"]))
