#!/usr/bin/env python3
"""Print the items the setup roles still own on each host, and where each must go (H7.2f).

`setup_census_items.py` derives the items from the tree. This module resolves each one against
the host packages and prints what is left. Run it for the list of record:

    uv run python scripts/deploy_tools/setup_lib/setup_census.py            # report, exit 0
    uv run python scripts/deploy_tools/setup_lib/setup_census.py --host daniel-pi
    uv run python scripts/deploy_tools/setup_lib/setup_census.py --strict   # exit 1 if any is left

Report mode prints the count per host and one line per item left. Strict mode fails while any
setup role still has an item that applies to a host (H7.19c turns it on). The report is also
the list of record for the rows `k8s_host_files` holds when H7 starts, and H7.18c takes it
again after every row has moved.

An item resolves to a package path or a legacy entry its host's packages carry, a unit or a
`Depends` entry for the modules that name one, or an entry of `ALLOWLIST`, which names each
deleted item with its reason. The arms that take the other modules (ufw, libvirt, users and
lines) are other PRs. Until they land, an item for such a module stays in the report.
"""

import sys as _sys
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path as _Path, PurePosixPath

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))  # scripts/

from deploy_tools.land_lib.land_reach import setup_role_hosts
from deploy_tools.setup_lib.setup_census_items import (
    CONTROL_MODULES,
    SETUP_ROLES,
    Item,
    role_items,
    setup_roles,
)
from deploy_tools.setup_lib.setup_gates import HOSTS
from lib.cli_help import ArgumentParser
from lib.render_context import render_context
from lib.repo_paths import REPO
from renderer.renderer_lib import host as host_mode

# The items the migration deletes rather than moves, each with the reason. A name here is an
# item's `name`, whatever its role or host.
ALLOWLIST = {
    "Clear ansible log file": (
        "deleted: the log it clears belongs to ansible-playbook, which H7.20a removes"
    ),
}

_PATH_WRITERS = frozenset({"template", "copy", "file", "get_url", "unarchive"})
_UNIT_MODULES = frozenset({"systemd", "systemd_service", "service"})
_CRON_D = PurePosixPath("/etc/cron.d")
_ETC_UNITS = PurePosixPath("/etc/systemd/system")


@dataclass
class HostSide:
    """What a host's packages carry, as the census resolves items against.

    Attributes:
        paths: Every path the packages install, and every path a legacy entry retires.
        crons: The name of every legacy cron entry.
        units: Every unit the packages install, enable or retire, by file name.
        depends: Every `Depends` entry of every package.
    """

    paths: set[str] = field(default_factory=set)
    crons: set[str] = field(default_factory=set)
    units: set[str] = field(default_factory=set)
    depends: set[str] = field(default_factory=set)


def package_side(hosts_root: _Path, hosts: tuple[str, ...]) -> dict[str, HostSide]:
    """Per host, what the shared packages and the host's own package install and retire."""
    shared = [
        host_mode.render_package(spec.parent.name, hosts_root=hosts_root)
        for spec in sorted((hosts_root / "packages").glob("*/package.yml"))
    ]
    sides: dict[str, HostSide] = {}
    for host in hosts:
        side = HostSide()
        packages = list(shared)
        if (hosts_root / host / "package.yml").is_file():
            packages.append(host_mode.render_host(host, hosts_root=hosts_root))
        for package in packages:
            side.paths.update(package.installed_paths())
            side.units.update(package.units())
            side.units.update(package.enable_links())
            side.depends.update(package.depends)
            for entry in package.legacy:
                if "cron" in entry:
                    side.crons.add(entry["cron"])
                elif "cron_file" in entry:
                    side.paths.add(str(_CRON_D / entry["cron_file"]))
                elif "unit" in entry:
                    side.paths.add(str(_ETC_UNITS / entry["unit"]))
                    side.units.add(entry["unit"])
                else:
                    side.paths.add(entry["path"])
        sides[host] = side
    return sides


def resolved(item: Item, side: HostSide, allowlist: dict[str, str] = ALLOWLIST) -> bool:
    """Whether a package, a legacy entry or the allowlist already carries `item`."""
    if item.name in allowlist:
        return True
    if item.kind == "cron":
        return item.name in side.crons
    if item.kind == "timer" or item.kind in _UNIT_MODULES:
        return (item.name if item.kind == "timer" else item.target) in side.units
    if item.kind in _PATH_WRITERS:
        return item.target in side.paths
    if item.kind == "apt" and not item.absent:
        names = [
            n
            for n in item.target.replace("[", "").replace("]", "").split(",")
            if n.strip()
        ]
        return bool(names) and all(n.strip(" '\"") in side.depends for n in names)
    return False


def items_left(
    hosts: tuple[str, ...] = HOSTS,
    setup: _Path = SETUP_ROLES,
    hosts_root: _Path | None = None,
    reach_of: Callable[[str], frozenset[str]] = setup_role_hosts,
    ctx_of: Callable[[_Path, str], dict] = render_context,
    allowlist: dict[str, str] = ALLOWLIST,
    roles: frozenset[str] | None = None,
    control: frozenset[str] = CONTROL_MODULES,
    repo: _Path = REPO,
) -> dict[str, list[Item]]:
    """Per host, the items no package or allowlist entry carries, in role then file order.

    `roles` limits the walk to those setup roles, for a caller that wants a few.
    """
    sides = package_side(hosts_root or host_mode.HOSTS, hosts)
    left: dict[str, list[Item]] = defaultdict(list)
    for role_dir in setup_roles(setup):
        if roles is not None and role_dir.name not in roles:
            continue
        reach = reach_of(role_dir.name) or frozenset(hosts)
        for item in role_items(role_dir, hosts, reach, ctx_of, repo, control):
            if not resolved(item, sides[item.host], allowlist):
                left[item.host].append(item)
    return {host: left.get(host, []) for host in hosts}


def render_report(left: dict[str, list[Item]]) -> str:
    """The report text: a count per host, then one line per item left."""
    out: list[str] = []
    for host, items in left.items():
        out.append(f"{host}: {len(items)} item(s) left")
        out.extend(f"  {item.line()}" for item in items)
    return "\n".join(out)


def strict_exit(left: dict[str, list[Item]]) -> int:
    """1 while any host has an item left, else 0."""
    return 1 if any(left.values()) else 0


def main(argv: list[str] | None = None) -> int:
    parser = ArgumentParser(
        description=__doc__.split("\n\n")[0], prog="setup_census.py"
    )
    parser.add_argument("--host", help="print one host only")
    parser.add_argument(
        "--strict", action="store_true", help="exit 1 while any item is left"
    )
    args = parser.parse_args(argv)
    hosts = (args.host,) if args.host else HOSTS
    left = items_left(hosts)
    print(render_report(left))
    return strict_exit(left) if args.strict else 0


if __name__ == "__main__":
    _sys.exit(main())
