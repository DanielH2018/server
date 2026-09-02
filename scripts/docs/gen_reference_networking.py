#!/usr/bin/env python3
"""Generate docs/reference/networking.md — what is routed, and what fronts it.

WHAT THIS ADDS OVER THE SERVICES PAGE. services.md answers "does this service have a
route". This answers "what does the edge do to a request before it arrives": whether the
route is reachable from the internet or only the LAN, and which Traefik middlewares sit in
front of it.

THE DOMAIN SUFFIX IS NOT DERIVABLE. `ingressroute.yml.j2` builds the hostname as
"{{ hostname }}.local.{{ domain }}", and `domain` is SOPS-sourced with no static default.
So this prints the hostname LABEL and says so, rather than constructing an FQDN it cannot
verify — the same constraint scripts/docs/service_catalog.py records in its FIELD NOTES.

STATIC PARSING ONLY: yaml.safe_load over the inventory, plain regex over template text.

Usage::

    uv run python scripts/docs/gen_reference_networking.py --out docs/reference/networking.md
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path


from route_facts import (
    GROUP_VARS,
    LAN,
    ingressroute_templates,
    linkify_fqdns,
    reachability,
    route_cell,
)

# Reach the sibling package directories: a directly-invoked script gets only its own
# directory on sys.path, and pyproject's `pythonpath` is a pytest setting.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

from lib.render_guard import host_files, load_yaml
from lib.repo_paths import HOST_VARS, K8S_ROLES

# The macro applies these to every route it renders, in this order. Read from
# ansible/templates/ingressroute.yml.j2 rather than assumed; see _baseline_middlewares.
_ALWAYS = "rate-limit"

_EXTRA_MW_RE = re.compile(r"extra_middlewares\s*=\s*\[([^\]]*)\]")
_QUOTED_RE = re.compile(r"['\"]([^'\"]+)['\"]")


def _load_host_vars(host_vars: Path) -> dict[str, dict]:
    return {path.stem: load_yaml(path) for path in host_files(host_vars)}


def build_rows(
    host_vars: Path = HOST_VARS,
    k8s_roles: Path = K8S_ROLES,
    group_vars: Path = GROUP_VARS,
) -> list[dict[str, str]]:
    """One row per k8s service that declares a route."""
    rows = []
    for host, data in _load_host_vars(host_vars).items():
        for entry in data.get("containers_list") or []:
            if not isinstance(entry, dict) or entry.get("platform") != "k8s":
                continue
            name = entry.get("name")
            if not name:
                continue
            templates = ingressroute_templates(k8s_roles / str(name))
            if not templates:
                continue
            text = "\n".join(p.read_text() for p in templates)

            middlewares = [_ALWAYS]
            if entry.get("use_authelia"):
                middlewares.append("authelia")
            match = _EXTRA_MW_RE.search(text)
            if match:
                middlewares.extend(_QUOTED_RE.findall(match.group(1)))

            label = str(entry.get("hostname", name))
            reach = reachability(k8s_roles / str(name), group_vars)
            rows.append(
                {
                    "name": str(name),
                    "host": host,
                    "hostname": label,
                    "reach": "LAN only" if reach == LAN else "LAN + public",
                    "route": route_cell(label, reach),
                    "middlewares": ", ".join(f"`{m}`" for m in middlewares),
                }
            )
    return rows


def render_markdown(rows: list[dict[str, str]]) -> str:
    from lib.docs_provenance import generated_banner

    parts = [generated_banner("scripts/docs/gen_reference_networking.py")]
    parts.append("# Networking\n")
    parts.append(f"{len(rows)} routed k8s service(s).\n")
    parts.append(
        '!!! note "The domain is filled in by your browser"\n'
        "    `domain` is SOPS-sourced with no static default, and these pages are rendered "
        "by static parsing, so the generator writes `<domain>` rather than guessing. On the "
        "docs site the routes below become links, built from the domain of the URL you are "
        "reading this on — so you get LAN links on the LAN name and public links on the "
        "public one.\n"
    )

    lan_only = [r for r in rows if r["reach"] == "LAN only"]
    parts.append(
        f"\n{len(lan_only)} route(s) are LAN-only, and the rest answer on both names. "
        "The absent Host rule is what keeps a LAN-only route off the internet, not DNS — "
        "the Cloudflare wildcard resolves any name.\n"
    )

    parts.append("\n## Routes\n")
    parts.append("| Service | Host | Route | Reachable from | Middlewares |")
    parts.append("|---|---|---|---|---|")
    for row in sorted(rows, key=lambda r: r["name"]):
        parts.append(
            f"| {row['name']} | {row['host']} | {linkify_fqdns(row['route'])} | "
            f"{row['reach']} | {row['middlewares']} |"
        )

    parts.append(
        "\n## Reading the middleware column\n\n"
        "`rate-limit` is applied by the shared macro to every route. `authelia` is present "
        "when the inventory entry sets `use_authelia: true`, and it is what makes a request "
        "meet the SSO gate. **An Authelia redirect fires in the middleware, before Traefik "
        "proxies to the workload** — so a 302 from a route proves the edge is up, and "
        "nothing about whether the backend is healthy.\n"
    )
    return "\n".join(parts).rstrip("\n") + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, required=True, help="output file path")
    parser.add_argument("--host-vars", type=Path, default=HOST_VARS)
    parser.add_argument("--k8s-roles", type=Path, default=K8S_ROLES)
    args = parser.parse_args(argv)

    from lib.docs_provenance import finish_generator

    rows = build_rows(args.host_vars, args.k8s_roles)
    return finish_generator(
        "gen_reference_networking", args.out, rows, render_markdown, "route"
    )


if __name__ == "__main__":
    raise SystemExit(main())
