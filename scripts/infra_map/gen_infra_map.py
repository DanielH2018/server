#!/usr/bin/env python3
"""Render a self-contained HTML map of the homelab infrastructure.

Two layers are merged into one page:

* **Declared state** — ``containers_list`` from ``ansible/inventory/host_vars/``,
  the source of truth for what is *supposed* to run where. This is what changes
  when you edit the repo.
* **Live state** — ``kubectl`` against the k3s cluster and ``docker ps`` on
  daniel-pi, overlaid onto the declared skeleton so drift is visible.

The page opens with an architecture diagram: the request path (DNS → ingress VIP
→ Traefik → Authelia → workloads), the cluster's two nodes, the Longhorn backup
chain, and the Pi's LAN-only plane. Its shape is a fixed skeleton — those edges
live in role templates, not in ``containers_list`` — but every number, address,
name and status colour on it is read from the inventory and the live cluster, so
it tracks changes without being hand-edited.

The output is a single ``.html`` file with no external assets, safe to open over
``file://``. Re-running overwrites it in place, so a cron entry keeps an open tab
current (the page carries its own ``<meta http-equiv="refresh">``).

An unreachable host degrades to declared-only rather than failing the render —
this runs unattended, and a partial map beats no map.

The work is split across four sibling modules, in the order the data moves:
``infra_map.inventory`` (repo) and ``infra_map.live`` (cluster) gather,
``infra_map.model`` reconciles them, ``infra_map.render`` draws the page. This
module owns the CLI and re-exports the public names, so ``gen_infra_map.<name>``
keeps resolving for callers and tests.

Usage::

    uv run python scripts/infra_map/gen_infra_map.py                     # default output path
    uv run python scripts/infra_map/gen_infra_map.py -o /tmp/map.html
    uv run python scripts/infra_map/gen_infra_map.py --no-live           # declared state only
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import sys as _sys
from datetime import datetime, timezone
from pathlib import Path
from pathlib import Path as _Path

# `infra_map` is a namespace package under `scripts/`, so reaching a sibling by package
# name needs `scripts/` on sys.path: a directly-invoked script — which is how the cron runs
# this one — gets only its own directory, and pyproject's `pythonpath` is a pytest setting.
# This has to sit ABOVE the sibling imports below, not after them: they are what needs it.
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

from infra_map.constants import (
    DEFAULT_OUTPUT,
    HOST_PLANE,
    HOST_ROLE,
    HOSTS,
    NAMESPACE_OWNERS,
    PAGE_REFRESH_SECONDS,
    REPO_ROOT,
)
from infra_map.inventory import (
    LONG_RUNNING_KINDS,
    RoleIndex,
    declared_services,
    load_inventory,
    load_roles,
    resolve_vars,
)
from infra_map.live import (
    MissingToolError,
    collect_cluster,
    collect_docker,
    collect_k8s,
    find_kubeconfig,
    find_tool,
    parse_backup_targets,
    parse_docker_ps,
    parse_kubectl_workloads,
    parse_kubectl_nodes,
    parse_pod_placement,
)
from infra_map.model import (
    build_model,
    find_extra_containers,
    match_k8s_workloads,
    place_on_nodes,
    reconcile_docker,
    reconcile_k8s,
    services_on_host,
)
from infra_map.render import group_services, render_html, render_svg

# Re-exported so `gen_infra_map.<name>` keeps working for the cron entry point,
# the tests, and anything else that treats this module as the public surface.
__all__ = [
    "DEFAULT_OUTPUT",
    "HOSTS",
    "HOST_PLANE",
    "HOST_ROLE",
    "MissingToolError",
    "NAMESPACE_OWNERS",
    "PAGE_REFRESH_SECONDS",
    "REPO_ROOT",
    "RoleIndex",
    "build_model",
    "collect_cluster",
    "collect_docker",
    "collect_k8s",
    "collect_live",
    "declared_services",
    "find_extra_containers",
    "find_kubeconfig",
    "find_tool",
    "group_services",
    "load_inventory",
    "load_roles",
    "LONG_RUNNING_KINDS",
    "main",
    "match_k8s_workloads",
    "parse_backup_targets",
    "parse_docker_ps",
    "parse_kubectl_workloads",
    "parse_kubectl_nodes",
    "parse_pod_placement",
    "place_on_nodes",
    "reconcile_docker",
    "reconcile_k8s",
    "render_html",
    "render_svg",
    "resolve_vars",
    "services_on_host",
]


def collect_live(
    local_hostname: str, longhorn_namespace: str
) -> tuple[dict[str, dict], dict]:
    """Gather live state for every host, tolerating failures per host.

    The two k3s hosts share one collection: workloads come from the cluster, not
    from either box individually, and a k3s host is reachable when its Node is
    Ready — not when it answers ssh. Only the Pi is polled over ssh, in a single
    call, because `ufw limit ssh` rejects a chatty caller.
    """
    cluster = collect_cluster(local_hostname, longhorn_namespace)
    deployments: dict = {}
    deployments_ok, deployments_err = False, cluster["error"]
    if cluster["ok"]:
        deployments_ok, deployments, deployments_err = collect_k8s(
            local_hostname, local_hostname
        )

    live: dict[str, dict] = {}
    for host in HOSTS:
        if HOST_PLANE[host] == "docker":
            ok, data, err = collect_docker(host, local_hostname)
            live[host] = {"ok": ok, "data": data, "error": err}
            continue
        node = cluster["nodes"].get(host)
        if not deployments_ok:
            err = deployments_err
        elif node is None:
            err = f"{host} is not a member of the cluster"
        elif not node["ready"]:
            err = f"node {host} is not Ready"
        else:
            err = ""
        live[host] = {"ok": not err, "data": deployments, "error": err}
    return live, cluster


def main(argv: list[str] | None = None) -> int:
    """Build the infra model and write it as HTML, SVG, or JSON (per `--format`/`--json`).

    Writes HTML via write-then-rename so a reader never sees a half-written page, and
    writes SVG only when its body actually changed. Exits 2 without touching the
    existing output if live collection is missing a required tool — overwriting it
    with a declared-only render would replace real data with a page that looks healthy
    and reports nothing wrong.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "-o", "--output", type=Path, default=DEFAULT_OUTPUT, help="output HTML path"
    )
    parser.add_argument(
        "--no-live", action="store_true", help="render declared inventory only"
    )
    parser.add_argument(
        "--json", action="store_true", help="dump the model as JSON instead"
    )
    parser.add_argument(
        "--format",
        choices=("html", "svg"),
        default="html",
        help="output format (default: html, for the standalone artifact page)",
    )
    args = parser.parse_args(argv)

    global_vars, host_vars = load_inventory()
    local_hostname = socket.gethostname()
    longhorn_namespace = global_vars.get("k8s_longhorn_namespace", "longhorn-system")
    try:
        if args.no_live:
            live = {h: {"ok": False, "data": {}, "error": "--no-live"} for h in HOSTS}
            cluster = None
        else:
            live, cluster = collect_live(local_hostname, longhorn_namespace)
    except MissingToolError as exc:
        # Deliberately leave the existing page alone. Overwriting it with a
        # declared-only render would replace real data with a page that looks
        # healthy and reports nothing wrong.
        print(f"error: {exc}; leaving the previous map in place", file=sys.stderr)
        return 2
    generated_at = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M %Z")
    model = build_model(
        global_vars, host_vars, live, generated_at, load_roles(), cluster
    )

    if args.json:
        json.dump(model, sys.stdout, indent=2)
        return 0

    args.output.parent.mkdir(parents=True, exist_ok=True)

    if args.format == "svg":
        from lib.docs_provenance import write_if_body_changed

        # The SVG carries no frontmatter, so write_if_body_changed compares the whole
        # text. That is what is wanted: an unconditional write would make the
        # docs-refresh cron commit on every run for an identical diagram.
        wrote = write_if_body_changed(args.output, render_svg(model))
        print(f"{'Wrote' if wrote else 'Unchanged'} {args.output}")
        return 0

    # Write-then-rename so a reader never sees a half-written page.
    tmp = args.output.with_suffix(".html.tmp")
    tmp.write_text(render_html(model), encoding="utf-8")
    os.replace(tmp, args.output)

    unreachable = [h["name"] for h in model["hosts"] if not h["reachable"]]
    note = (
        f" (live state unavailable for: {', '.join(unreachable)})"
        if unreachable
        else ""
    )
    print(f"Wrote {args.output}{note}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
