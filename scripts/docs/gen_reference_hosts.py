#!/usr/bin/env python3
"""Generate docs/reference/hosts.md — the three hosts and what each one is.

STATIC PARSING ONLY. Reads ansible/inventory/hosts.ini and host_vars/*.yml with
yaml.safe_load and plain text parsing, never `ansible` or `kubectl`. A fresh worktree has
no Ansible collections installed, and the inventory contains SOPS lookups that do not
render outside a real deploy.

A fact with no machine-readable source prints its reason rather than a guess, following
the convention scripts/docs/service_catalog.py sets.

Usage::

    uv run python scripts/docs/gen_reference_hosts.py --out docs/reference/hosts.md
"""

from __future__ import annotations

import argparse
from pathlib import Path

import yaml

# Reach the sibling package directories: a directly-invoked script gets only its own
# directory on sys.path, and pyproject's `pythonpath` is a pytest setting.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

REPO = Path(__file__).resolve().parents[2]
HOSTS_INI = REPO / "ansible" / "inventory" / "hosts.ini"
HOST_VARS = REPO / "ansible" / "inventory" / "host_vars"
ALL_VARS = REPO / "ansible" / "inventory" / "group_vars" / "all.yml"

UNKNOWN = "unknown"

# What each host is for. Not derivable: the inventory records connection details and
# feature flags, never a host's purpose. Kept here so the page states it, with the
# derived facts alongside carrying whether it is still true.
ROLES = {
    "daniel-box": "k3s server / control-plane node. Ansible runs here, and so do the "
    "GitOps timer, the docs build and most workloads.",
    "daniel-server": "k3s agent node. Intel iGPU for transcoding, LVM storage, and the "
    "UPS hardware behind the NUT shutdown chain.",
    "daniel-pi": "Raspberry Pi, and the only remaining Docker host. LAN-only utilities.",
}


def parse_hosts_ini(path: Path = HOSTS_INI) -> list[dict[str, str]]:
    """Hosts and their connection settings, in inventory order.

    Parsed by hand rather than with configparser: an ini host line is
    `name key=value key=value`, which configparser reads as one long key.
    """
    hosts: list[dict[str, str]] = []
    if not path.is_file():
        return hosts
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ";", "[")):
            continue
        name, *settings = line.split()
        entry = {"name": name}
        for setting in settings:
            if "=" in setting:
                key, value = setting.split("=", 1)
                entry[key] = value
        hosts.append(entry)
    return hosts


def _load_yaml_mapping(path: Path) -> dict:
    if not path.is_file():
        return {}
    loaded = yaml.safe_load(path.read_text())
    return loaded if isinstance(loaded, dict) else {}


def load_host_vars(name: str, host_vars: Path = HOST_VARS) -> dict:
    return _load_yaml_mapping(host_vars / f"{name}.yml")


def _flag(data: dict, key: str, defaults: dict | None = None) -> str:
    """Host value, else the group default, else unknown.

    A host that declares nothing still inherits group_vars/all.yml, so "not in host_vars"
    is not the same as "nobody said". Reading host_vars alone reported `unknown (has_docker
    not declared)` for daniel-pi — which inherits `has_docker: true` — on the same page
    whose own text calls the Pi the only remaining Docker host (2026-08-25 review M-14).

    The provenance stays visible rather than collapsing into a bare "yes": the convention
    here is to never let a default read as something the host asserted.
    """
    if key in data:
        return "yes" if data[key] else "no"
    if defaults and key in defaults:
        return "%s (group default)" % ("yes" if defaults[key] else "no")
    return f"{UNKNOWN} ({key} not declared)"


def build_rows(
    hosts_ini: Path = HOSTS_INI,
    host_vars: Path = HOST_VARS,
    all_vars: Path = ALL_VARS,
) -> list[dict[str, str]]:
    # A parameter, not the module constant read directly: a test builds a synthetic
    # inventory in a tmp dir, and reaching past it to the real group_vars would make that
    # fixture depend on the repo it is meant to stand in for.
    defaults = _load_yaml_mapping(all_vars)
    rows = []
    for entry in parse_hosts_ini(hosts_ini):
        name = entry["name"]
        data = load_host_vars(name, host_vars)
        services = data.get("containers_list")
        rows.append(
            {
                "name": name,
                "role": ROLES.get(name, f"{UNKNOWN} (no description recorded)"),
                "ip": str(data.get("server_ip", f"{UNKNOWN} (server_ip not declared)")),
                "connection": entry.get("ansible_connection", "ssh"),
                "expose_mode": str(data.get("expose_mode", "traefik (default)")),
                "services": str(len(services)) if isinstance(services, list) else "0",
                "gitops": _flag(data, "has_gitops", defaults),
                "docker": _flag(data, "has_docker", defaults),
            }
        )
    return rows


def render_markdown(rows: list[dict[str, str]]) -> str:
    from lib.docs_provenance import generated_banner

    parts = [generated_banner("scripts/docs/gen_reference_hosts.py")]
    parts.append("# Hosts\n")
    parts.append(f"{len(rows)} host(s) in `ansible/inventory/hosts.ini`.\n")

    for row in sorted(rows, key=lambda r: r["name"]):
        parts.append(f"\n## {row['name']}\n")
        parts.append(f"{row['role']}\n")
        parts.append("| Fact | Value |")
        parts.append("|---|---|")
        parts.append(f"| LAN address | `{row['ip']}` |")
        parts.append(f"| Ansible connection | `{row['connection']}` |")
        parts.append(f"| Service exposure | {row['expose_mode']} |")
        parts.append(f"| Services declared | {row['services']} |")
        parts.append(f"| Runs the GitOps timer | {row['gitops']} |")
        parts.append(f"| Has Docker | {row['docker']} |")

    parts.append(
        "\n## Running a playbook against a host\n\n"
        "`hosts.ini` pins both cluster nodes to `ansible_connection=local`, so a play's "
        "`hosts:` defaults to the local hostname. **`--limit daniel-pi` therefore matches "
        "zero hosts** — target the Pi with `-e target=daniel-pi` instead. A one-shot play "
        "that ignores this runs on the wrong box while appearing to succeed.\n"
    )
    return "\n".join(parts).rstrip("\n") + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, required=True, help="output file path")
    parser.add_argument("--hosts-ini", type=Path, default=HOSTS_INI)
    parser.add_argument("--host-vars", type=Path, default=HOST_VARS)
    args = parser.parse_args(argv)

    from lib.docs_provenance import write_if_body_changed

    rows = build_rows(args.hosts_ini, args.host_vars)
    wrote = write_if_body_changed(args.out, render_markdown(rows))
    print(
        f"gen_reference_hosts: {len(rows)} host(s), "
        f"{'wrote' if wrote else 'unchanged'} {args.out}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
