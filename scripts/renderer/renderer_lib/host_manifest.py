"""The manifest `homelab-reconcile` reads, and the three spec keys that feed it.

A host package installs `/usr/lib/homelab/manifest.json` (H3, *Shared names*), in the schema
`hosts/lib/homelab-reconcile`'s docstring documents:

- `units` lists every unit the host package and `homelab-updater` install, and every unit the
  host package drops a `50-homelab.conf` into, each mapped to its policy or `{}`;
- `enable_links` lists each unit the host package links from a `<target>.wants/`
  directory;
- `legacy` holds the host package's legacy entries, each value rendered;
- `reconcilers` lists each arm the package declares state for (H7.2b to H7.2d);
- `activations` maps each file with an `activate:` field to its action (H7.2a), and the
  updater's files join the host package's;
- `ufw`, `libvirt`, `users` and `lines` hold the declarations of those arms (`host_arms.py`),
  each present only with its arm listed;
- `secrets_inputs_sha256` is present only for a package that ships the `secrets-unit` component.

A package or component may also carry `units`, a policy for each unit reconcile must not
restart on a change, `enable`, the enable links it installs, and `legacy`, the Ansible-era
entries reconcile retires (H3, *The host handoff rule*)::

    units: {homelab-x.service: {policy: manual}}    # or windowed
    enable:                                         # see host_manifest.add_enable_links
      homelab-x.timer: timers.target
      homelab-y.timer: {target: timers.target, when: x_enabled}
    legacy:
      - {path: /usr/local/bin/x.sh}
      - {cron: "X heartbeat", user: "{{ sys_user }}"}
      - {cron_file: x}
      - {unit: x.service}

Each legacy value renders with the host's context. An enable entry's `when:` is a Jinja
expression evaluated in that context, and the link ships only when it is true. Each enable link
ships as an nfpm `type: symlink` entry. A host package installs
`/usr/lib/homelab/manifest.json`, which `host_manifest` builds from these keys and the units and
enable links the packages install. A shared package installs no manifest, and lists no legacy
entry and no enable link.

`units:`, `enable:` and `legacy:` in a `package.yml` or `component.yml` feed it. The version and SHA stay
out of it, so a render changes only when the package's content does (H3, *Package rules*).
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

import json
import re
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Protocol

from renderer.renderer_lib.cluster import RenderRefused
from renderer.renderer_lib.host_arms import add_sysusers, arm_sections


UNIT_DIR = PurePosixPath("/usr/lib/systemd/system")
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
UNIT_NAME = re.compile(r"[A-Za-z0-9@_.:-]+\.(service|timer|socket|path)")
_TARGET_NAME = re.compile(r"[A-Za-z0-9@_.:-]+\.target")
_CRON_FILE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")


class Package(Protocol):
    """The part of `host.Package` the manifest reads, named here so neither module imports the other."""

    name: str
    files: dict[str, tuple[bytes, int]]
    links: dict[str, str]
    policies: dict[str, str]
    legacy: list[dict[str, str]]
    secrets_inputs_sha256: str | None
    activations: dict[str, str]
    ufw_rules: list[dict]
    ufw_legacy: list[dict]
    libvirt: dict[str, list[dict]]
    users: list[dict]
    lines: list[dict]

    def units(self) -> list[str]: ...

    def dropin_targets(self) -> list[str]: ...

    def enable_links(self) -> list[str]: ...


def manifest(package: Package, updater: Package | None) -> dict:
    """The manifest `homelab-reconcile` reads, for a host package and the updater beside it.

    Raises:
        RenderRefused: A policy or an enable link names a unit neither package installs or
            drops in on, or a drop-in target is a unit kind reconcile does not manage.
            `homelab-reconcile:parse_manifest` refuses each, which would fail every run.
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
    unmanaged = sorted(unit for unit in managed if not UNIT_NAME.fullmatch(unit))
    if unmanaged:
        raise RenderRefused(
            f"{package.name}: drop-ins target {unmanaged}, which are not a service, "
            "timer, socket or path unit"
        )
    links = package.enable_links()
    unlisted = sorted(set(links) - managed)
    if unlisted:
        raise RenderRefused(
            f"{package.name}: enable links name {unlisted}, which no package installs "
            "or drops in on"
        )
    sections = arm_sections(package)
    activations = {**(updater.activations if updater else {}), **package.activations}
    result = {
        "units": {
            unit: {"policy": policies[unit]} if unit in policies else {}
            for unit in sorted(managed)
        },
        "enable_links": links,
        "legacy": package.legacy,
        # Reconcile fails a run that lists a reconciler kind it does not have.
        "reconcilers": list(sections),
        **sections,
    }
    if activations:
        result["activations"] = dict(sorted(activations.items()))
    # Only a package that ships the `secrets-unit` component carries the field, so reconcile keys
    # its secrets step on it (H4, *The restart contract*).
    if package.secrets_inputs_sha256:
        result["secrets_inputs_sha256"] = package.secrets_inputs_sha256
    return result


def add_manifest(package: Package, updater: Package | None) -> None:
    """Install the manifest in `package`, a host package with `updater` beside it."""
    if MANIFEST in package.files:
        raise RenderRefused(f"{package.name}: {MANIFEST} is the rendered manifest")
    add_sysusers(package)
    text = json.dumps(manifest(package, updater), indent=1, sort_keys=True) + "\n"
    package.files[MANIFEST] = (text.encode(), 0o644)


def add_policies(
    package: Package,
    directory: Path,
    units: object,
    ships: Callable[[object], bool] = lambda entry: True,
) -> None:
    """Record in `package` each unit's restart policy from a spec's `units:` map.

    An entry is `{policy: <policy>}` and may add `when: <expression>`; `ships` returns whether
    the entry holds, and a unit whose `when` is false records no policy.

    Raises:
        RenderRefused: The map is malformed, or a second spec gives a unit a policy.
    """
    if not isinstance(units, dict):
        raise RenderRefused(f"{directory}: units {units!r} is not a map")
    for unit, spec in units.items():
        policy = spec.get("policy") if isinstance(spec, dict) else None
        if (
            not isinstance(unit, str)
            or not UNIT_NAME.fullmatch(unit)
            or not isinstance(spec, dict)
            or not {"policy"} <= set(spec) <= {"policy", "when"}
            or policy not in POLICIES
        ):
            raise RenderRefused(
                f"{directory}: units entry {unit!r}: {spec!r} is not a unit name mapped "
                f"to {{policy: {' | '.join(sorted(POLICIES))}}}"
            )
        if not ships(spec):
            continue
        if unit in package.policies:
            raise RenderRefused(f"{package.name}: two specs give {unit} a policy")
        package.policies[unit] = policy


def add_enable_links(
    package: Package,
    directory: Path,
    enable: object,
    test: Callable[[str], bool],
) -> None:
    """Record in `package` each enable link a spec's `enable:` map declares.

    `{unit: target}` becomes the symlink `systemctl enable` writes:
    `/usr/lib/systemd/system/<target>.wants/<unit>`, pointing at `../<unit>`. systemd adds a
    `Wants=` only for a unit symlinked from a `.wants/` directory, so a copied file there would
    leave the unit unstarted at boot. `{unit: {target: <target>, when: <expression>}}` records
    the link only when `test` returns True for the expression. A dropped link is how a host
    disarms a timer: reconcile stops a timer whose link the new manifest no longer lists.

    Raises:
        RenderRefused: The map is malformed, or a second spec declares the same link.
    """
    if not isinstance(enable, dict):
        raise RenderRefused(f"{directory}: enable {enable!r} is not a map")
    for unit, value in enable.items():
        target, when = value, None
        if isinstance(value, dict) and set(value) == {"target", "when"}:
            target, when = value["target"], value["when"]
        if not (
            isinstance(unit, str)
            and UNIT_NAME.fullmatch(unit)
            and isinstance(target, str)
            and _TARGET_NAME.fullmatch(target)
            and (when is None or isinstance(when, str))
        ):
            raise RenderRefused(
                f"{directory}: enable entry {unit!r}: {value!r} is not a unit name "
                "mapped to a .target, or to {target: <.target>, when: <expression>}"
            )
        if when is not None and not test(when):
            continue
        dst = str(UNIT_DIR / f"{target}.wants" / unit)
        if dst in package.links:
            raise RenderRefused(f"{package.name}: two specs install {dst}")
        package.links[dst] = f"../{unit}"


def refuse_wants_file(directory: Path, dst: PurePosixPath) -> None:
    """Refuse a file entry under a `<target>.wants/` directory, which `enable:` declares.

    Raises:
        RenderRefused: `dst` is one.
    """
    if dst.parent.parent == UNIT_DIR and dst.parent.name.endswith(".wants"):
        target = dst.parent.name.removesuffix(".wants")
        raise RenderRefused(
            f"{directory}: {dst} is a file under a .wants/ directory, where systemd reads "
            f"symlinks; declare it as enable: {{{dst.name}: {target}}}"
        )


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
    return bool(UNIT_NAME.fullmatch(entry["unit"]))
