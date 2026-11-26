#!/usr/bin/env python3
"""Generate docs/reference/hosts.md — the three hosts and what each one is.

STATIC PARSING ONLY. Reads hosts/*/vars.yml, k8s/services.yml and each
hosts/<host>/package.yml with yaml.safe_load and plain text parsing, never `ansible` or
`kubectl`. A fresh worktree has
no Ansible collections installed, and the inventory contains SOPS lookups that do not
render outside a real deploy.

A fact with no machine-readable source prints its reason rather than a guess, following
the convention scripts/docs/service_catalog.py sets.

Usage::

    uv run python scripts/docs/reference/hosts.py --out docs/reference/hosts.md
"""

from pathlib import Path

from lib.cli_help import ArgumentParser

from lib import yaml_fast
from lib.estate import REPO_INVENTORY, Estate, Inventory
from lib.inventory_lists import K8S_HOST
from lib.repo_paths import REPO

UNKNOWN = "unknown"
HOSTS = REPO / "hosts"

# Not derivable: the inventory records connection details and flags, never a host's purpose.
ROLES = {
    "daniel-box": "k3s server / control-plane node. The host-package builder, the docs build, "
    "landings and most workloads run here.",
    "daniel-server": "k3s agent node. Intel iGPU for transcoding, LVM storage, and the "
    "UPS hardware behind the NUT shutdown chain.",
    "daniel-pi": "Raspberry Pi, and the only remaining Docker host. LAN-only utilities.",
}


def _components(name: str, hosts_root: Path) -> list[str] | None:
    """The components the host's package lists, or None when it has no package."""
    spec = hosts_root / name / "package.yml"
    if not spec.is_file():
        return None
    return (yaml_fast.safe_load(spec.read_text()) or {}).get("components") or []


def _package(name: str, components: list[str] | None) -> str:
    """The host's package and how many components it lists, or why there is none to name."""
    if components is None:
        return f"{UNKNOWN} (no hosts/{name}/package.yml)"
    return f"`homelab-{name}` ({len(components)} components)"


def _docker(name: str, components: list[str] | None) -> str:
    """Whether the host's package ships the Docker engine, the `pi-docker` component."""
    if components is None:
        return f"{UNKNOWN} (no hosts/{name}/package.yml)"
    return "yes" if "pi-docker" in components else "no"


def build_rows(
    inventory: Inventory = REPO_INVENTORY, hosts_root: Path = HOSTS
) -> list[dict[str, str]]:
    """Collect every host in `inventory` into one row of inventory facts each.

    A host's services are the k8s services on the k8s host and its `compose_services` on any
    other.

    Args:
        inventory: The inventory: each host's `host_vars` file and the k8s service list.
        hosts_root: The `hosts/` tree, read for each host's package.

    Returns:
        One dict per host, keyed by the columns rendered in `render_markdown`.
    """
    estate = Estate(inventory)
    rows = []
    for name in estate.hosts():
        data = estate.own_vars(name)
        components = _components(name, hosts_root)
        declared = (
            estate.services if name == K8S_HOST else data.get("compose_services") or []
        )
        rows.append(
            {
                "name": name,
                "role": ROLES.get(name, f"{UNKNOWN} (no description recorded)"),
                "ip": str(data.get("server_ip", f"{UNKNOWN} (server_ip not declared)")),
                "package": _package(name, components),
                "services": str(len(declared)),
                "docker": _docker(name, components),
            }
        )
    return rows


def render_markdown(rows: list[dict[str, str]]) -> str:
    """Render `rows` as the "Hosts" reference page, one section per host.

    Args:
        rows: Host rows as returned by `build_rows`.

    Returns:
        The full page as Markdown text, ending in a single trailing newline.
    """
    from lib.docs_provenance import generated_banner

    parts = [generated_banner("scripts/docs/reference/hosts.py")]
    parts.append("# Hosts\n")
    parts.append(f"{len(rows)} host(s) in `hosts/`.\n")

    for row in sorted(rows, key=lambda r: r["name"]):
        parts.append(f"\n## {row['name']}\n")
        parts.append(f"{row['role']}\n")
        parts.append("| Fact | Value |")
        parts.append("|---|---|")
        parts.append(f"| LAN address | `{row['ip']}` |")
        parts.append(f"| Host package | {row['package']} |")
        parts.append(f"| Services declared | {row['services']} |")
        parts.append(f"| Has Docker | {row['docker']} |")

    return "\n".join(parts).rstrip("\n") + "\n"


def main(argv: list[str] | None = None) -> int:
    """Build the host rows, render the reference page, and write it if the body changed.

    Returns:
        The exit code from `finish_generator` (0 on success, non-zero on a write failure).
    """
    parser = ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, required=True, help="output file path")
    args = parser.parse_args(argv)

    from lib.docs_provenance import finish_generator

    rows = build_rows()
    return finish_generator(
        "docs.reference.hosts", args.out, rows, render_markdown, "host"
    )


if __name__ == "__main__":
    raise SystemExit(main())
