#!/usr/bin/env python3
"""Render a self-contained HTML map of the homelab infrastructure.

Two layers are merged into one page:

* **Declared state** — ``containers_list`` from ``ansible/inventory/host_vars/``,
  the source of truth for what is *supposed* to run where. This is what changes
  when you edit the repo.
* **Live state** — ``docker ps`` on daniel-server and ``kubectl get deployments``
  on daniel-box, overlaid onto the declared skeleton so drift is visible.

The output is a single ``.html`` file with no external assets, safe to open over
``file://``. Re-running overwrites it in place, so a cron entry keeps an open tab
current (the page carries its own ``<meta http-equiv="refresh">``).

An unreachable host degrades to declared-only rather than failing the render —
this runs unattended, and a partial map beats no map.

Usage::

    uv run python scripts/gen_infra_map.py                     # default output path
    uv run python scripts/gen_infra_map.py -o /tmp/map.html
    uv run python scripts/gen_infra_map.py --no-live           # declared state only
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import shutil
import socket
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT = Path.home() / ".claude" / "artifacts" / "homelab-infra-map.html"

# The two hosts this map covers. daniel-pi is deliberately out of scope.
HOSTS = ("daniel-box", "daniel-server")

# How long the rendered page waits before reloading itself, in seconds. Matches
# the refresh cron's 15-minute period (initial_setup's `infra-map` task) — a
# shorter reload would imply the data is fresher than it is.
PAGE_REFRESH_SECONDS = 900

# Live-collection timeouts. Short on purpose: an unattended run must not hang.
LOCAL_TIMEOUT = 20
SSH_TIMEOUT = 25

_CONTAINER_NAME = re.compile(
    r"^\s*container_name:\s*([A-Za-z0-9_.-]+)\s*$", re.MULTILINE
)

# `claude-otel` is one declared entry that expands to the whole observability
# namespace (collector + loki + prometheus + tempo + grafana). Nothing else in
# the inventory owns a namespace, so an explicit entry beats deriving it by
# rendering the role's Jinja namespace manifest.
NAMESPACE_OWNERS = {"claude-otel": "k8s_observability_namespace"}

_JINJA_VAR = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")


# --------------------------------------------------------------------------
# Inventory (declared state)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class RoleIndex:
    """What the repo says each role actually creates.

    ``container_owners`` maps a container name to the role whose compose file
    declares it. ``batch_roles`` are roles that run something one-shot and leave
    nothing behind — configarr is `compose run --rm` on a cron, and the k8s
    image-build roles ship no manifests — so "not running" is correct for them,
    not a fault.
    """

    container_owners: dict[str, str]
    batch_roles: frozenset[str]


def load_roles(repo_root: Path = REPO_ROOT) -> RoleIndex:
    """Build the role index by reading the role trees, not by guessing names."""
    owners: dict[str, str] = {}
    batch: set[str] = set()

    docker_roles = repo_root / "ansible" / "roles" / "containers"
    for role in sorted(p for p in docker_roles.glob("*") if p.is_dir()):
        compose = role / "templates" / "docker-compose.yml.j2"
        if not compose.is_file():
            continue
        names = _CONTAINER_NAME.findall(compose.read_text())
        if not names:
            batch.add(role.name)
        for name in names:
            owners[name] = role.name

    # A k8s role with no templates directory builds images or seeds volumes; it
    # has no Deployment to find.
    k8s_roles = repo_root / "ansible" / "roles" / "k8s"
    for role in sorted(p for p in k8s_roles.glob("*") if p.is_dir()):
        if not (role / "templates").is_dir():
            batch.add(role.name)

    return RoleIndex(container_owners=owners, batch_roles=frozenset(batch))


def resolve_vars(value: Any, variables: dict[str, Any], _depth: int = 0) -> Any:
    """Substitute simple ``{{ name }}`` references from *variables*.

    Only bare-variable interpolation is supported — that is all the inventory
    uses for the keys this map reads (``hostname``, namespace names). Filters,
    expressions, and unknown names are left untouched so they show up verbatim
    in the output rather than being silently blanked.
    """
    if not isinstance(value, str):
        return value

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in variables:
            return match.group(0)
        return str(variables[name])

    resolved = _JINJA_VAR.sub(replace, value)
    # Values can reference other templated values (k8s_registry_pull_host).
    if resolved != value and _depth < 5 and _JINJA_VAR.search(resolved):
        return resolve_vars(resolved, variables, _depth + 1)
    return resolved


def load_inventory(
    repo_root: Path = REPO_ROOT,
) -> tuple[dict[str, Any], dict[str, dict]]:
    """Return ``(global_vars, {host: host_vars})`` from the Ansible inventory."""
    inventory = repo_root / "ansible" / "inventory"
    global_vars = (
        yaml.safe_load((inventory / "group_vars" / "all.yml").read_text()) or {}
    )
    host_vars: dict[str, dict] = {}
    for host in HOSTS:
        path = inventory / "host_vars" / f"{host}.yml"
        host_vars[host] = (
            (yaml.safe_load(path.read_text()) or {}) if path.exists() else {}
        )
    return global_vars, host_vars


def declared_services(host: str, host_vars: dict, global_vars: dict) -> list[dict]:
    """Flatten a host's ``containers_list`` into normalized service records."""
    variables = {**global_vars, **host_vars}
    services = []
    for entry in host_vars.get("containers_list") or []:
        name = entry.get("name")
        if not name:
            continue
        platform = entry.get("platform", "docker")
        hostname = resolve_vars(entry.get("hostname", name), variables)
        namespace = None
        if platform == "k8s":
            ns_var = NAMESPACE_OWNERS.get(name)
            namespace = (
                variables.get(ns_var) if ns_var else variables.get("k8s_namespace")
            )
        services.append(
            {
                "name": name,
                "host": host,
                "platform": platform,
                "hostname": hostname if entry.get("port") else None,
                "port": entry.get("port"),
                "authelia": bool(entry.get("use_authelia")),
                "networks": list(entry.get("networks") or []),
                "namespace": namespace,
                "declared": True,
                "status": "unknown",
                "detail": "",
                "image": "",
                "replicas": None,
            }
        )
    return services


# --------------------------------------------------------------------------
# Live state
# --------------------------------------------------------------------------


# Directories searched for the collector binaries, on top of whatever PATH the
# caller happens to have. cron runs with PATH=/usr/bin:/bin, which omits
# /usr/local/bin — where kubectl lives as a symlink to k3s. Resolving tools here
# rather than trusting PATH is what stops an impoverished environment from
# silently blinding half the map; see MissingToolError for the other half.
TOOL_DIRS = ("/usr/local/bin", "/usr/bin", "/bin", "/usr/local/sbin", "/snap/bin")


class MissingToolError(Exception):
    """A collector binary is absent — a broken setup, not a host being down.

    Kept distinct from an unreachable host on purpose. A host that is down is an
    observation worth rendering; a missing binary means this run could never
    have seen anything, and a page that quietly reports declared-only in that
    case looks identical to a healthy one. This escalates to a non-zero exit so
    cron surfaces it instead of overwriting the page with a half-blind copy.
    """


def find_tool(name: str) -> str | None:
    """Resolve a binary by absolute path, searching beyond the inherited PATH."""
    found = shutil.which(name)
    if found:
        return found
    search = os.pathsep.join(TOOL_DIRS)
    return shutil.which(name, path=search)


def _run(cmd: list[str], timeout: int) -> tuple[bool, str]:
    """Run *cmd*, returning ``(ok, stdout-or-error)``. Never raises."""
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=False
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return False, str(exc)
    if proc.returncode != 0:
        return False, (proc.stderr or proc.stdout).strip()[:400]
    return True, proc.stdout


DOCKER_PS_FORMAT = "{{.Names}}\t{{.State}}\t{{.Status}}\t{{.Image}}"


def parse_docker_ps(output: str) -> dict[str, dict]:
    """Parse tab-separated ``docker ps -a`` output keyed by container name."""
    containers: dict[str, dict] = {}
    for line in output.splitlines():
        parts = line.rstrip("\n").split("\t")
        if len(parts) < 4 or not parts[0].strip():
            continue
        name, state, status, image = (p.strip() for p in parts[:4])
        containers[name] = {
            "state": state,
            "status": status,
            "image": image,
            "healthy": "(healthy)" in status,
            "unhealthy": "(unhealthy)" in status,
        }
    return containers


def collect_docker(host: str, local_hostname: str) -> tuple[bool, dict[str, dict], str]:
    """Collect Docker state for *host*, locally or over a single ssh call."""
    if host == local_hostname:
        docker = find_tool("docker")
        if docker is None:
            raise MissingToolError("docker not found on this host")
        cmd = [docker, "ps", "-a", "--format", DOCKER_PS_FORMAT]
        timeout = LOCAL_TIMEOUT
    else:
        ssh = find_tool("ssh")
        if ssh is None:
            raise MissingToolError("ssh not found on this host")
        # One ssh invocation, not one per service: `ufw limit ssh` rejects
        # 6+ connections per 30s and would ban an over-eager generator.
        remote = "docker ps -a --format '%s'" % DOCKER_PS_FORMAT
        cmd = [
            ssh,
            "-o",
            "BatchMode=yes",
            "-o",
            f"ConnectTimeout={SSH_TIMEOUT}",
            host,
            remote,
        ]
        timeout = SSH_TIMEOUT + 10
    ok, out = _run(cmd, timeout)
    if not ok:
        return False, {}, out
    return True, parse_docker_ps(out), ""


def parse_kubectl_deployments(payload: str) -> dict[tuple[str, str], dict]:
    """Parse ``kubectl get deployments -A -o json`` into ``{(ns, name): info}``."""
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        return {}
    workloads: dict[tuple[str, str], dict] = {}
    for item in data.get("items", []):
        meta = item.get("metadata", {})
        name, namespace = meta.get("name"), meta.get("namespace")
        if not name or not namespace:
            continue
        status = item.get("status", {})
        spec = item.get("spec", {})
        desired = spec.get("replicas", 0)
        ready = status.get("readyReplicas", 0) or 0
        containers = (
            item.get("spec", {})
            .get("template", {})
            .get("spec", {})
            .get("containers", [])
        )
        workloads[(namespace, name)] = {
            "ready": ready,
            "desired": desired,
            "image": containers[0].get("image", "") if containers else "",
        }
    return workloads


def collect_k8s(host: str, local_hostname: str) -> tuple[bool, dict, str]:
    """Collect Deployment state from the cluster (local kubectl only)."""
    if host != local_hostname:
        return False, {}, f"kubectl only queried locally; run this on {host}"
    kubectl = find_tool("kubectl")
    if kubectl is None:
        raise MissingToolError("kubectl not found on this host")
    ok, out = _run([kubectl, "get", "deployments", "-A", "-o", "json"], LOCAL_TIMEOUT)
    if not ok:
        return False, {}, out
    return True, parse_kubectl_deployments(out), ""


# --------------------------------------------------------------------------
# Reconciliation
# --------------------------------------------------------------------------


def match_k8s_workloads(
    service: dict, workloads: dict[tuple[str, str], dict]
) -> list[dict]:
    """Find the Deployments backing a declared k8s service.

    A namespace owner (``claude-otel``) claims every Deployment in its
    namespace; everything else matches its own name plus ``<name>-*`` helper
    Deployments in the app namespace.
    """
    name, namespace = service["name"], service.get("namespace")
    matched = []
    if name in NAMESPACE_OWNERS:
        for (ns, wl_name), info in workloads.items():
            if ns == namespace:
                matched.append({"name": wl_name, "namespace": ns, **info})
    else:
        for (ns, wl_name), info in workloads.items():
            if ns != namespace:
                continue
            if wl_name == name or wl_name.startswith(f"{name}-"):
                matched.append({"name": wl_name, "namespace": ns, **info})
    return sorted(matched, key=lambda w: w["name"])


def reconcile_docker(
    service: dict, containers: dict[str, dict], roles: RoleIndex
) -> dict:
    """Overlay live Docker state onto one declared service."""
    live = containers.get(service["name"])
    if live is None:
        if service["name"] in roles.batch_roles:
            return {
                **service,
                "status": "job",
                "detail": "one-shot job — leaves no container behind",
            }
        return {**service, "status": "missing", "detail": "no container found"}
    if live["state"] != "running":
        return {
            **service,
            "status": "down",
            "detail": live["status"],
            "image": live["image"],
        }
    status = "degraded" if live["unhealthy"] else "healthy"
    return {
        **service,
        "status": status,
        "detail": live["status"],
        "image": live["image"],
    }


def reconcile_k8s(
    service: dict, workloads: dict[tuple[str, str], dict], roles: RoleIndex
) -> dict:
    """Overlay live Deployment state onto one declared k8s service."""
    matched = match_k8s_workloads(service, workloads)
    if not matched:
        if service["name"] in roles.batch_roles:
            return {
                **service,
                "status": "job",
                "detail": "build/seed role — no long-running workload",
            }
        return {**service, "status": "missing", "detail": "no deployment found"}
    ready = sum(w["ready"] for w in matched)
    desired = sum(w["desired"] for w in matched)
    detail = f"{ready}/{desired} replicas ready across {len(matched)} deployment"
    detail += "s" if len(matched) != 1 else ""
    if ready == 0:
        status = "down"
    elif ready < desired:
        status = "degraded"
    else:
        status = "healthy"
    return {
        **service,
        "status": status,
        "detail": detail,
        "image": matched[0]["image"],
        "replicas": (ready, desired),
        "workloads": matched,
    }


def find_extra_containers(
    containers: dict[str, dict], declared_names: set[str], roles: RoleIndex
) -> list[dict]:
    """Classify live containers that have no ``containers_list`` entry.

    Most are companions: a role's compose file defines several containers but
    only the main one earns an inventory entry (the prometheus role also brings
    node-exporter and cadvisor). Those are expected. Anything the repo does not
    account for at all is real drift, and only that gets flagged as undeclared.
    """
    extras = []
    for name, live in sorted(containers.items()):
        if name in declared_names:
            continue
        owner = roles.container_owners.get(name)
        if owner and owner in declared_names:
            status, detail = (
                "companion",
                f"owned by the {owner} role · {live['status']}",
            )
        elif live["state"] == "running":
            status, detail = "undeclared", live["status"]
        else:
            status, detail = "down", live["status"]
        extras.append(
            {
                "name": name,
                "platform": "docker",
                "hostname": None,
                "port": None,
                "authelia": False,
                "networks": [],
                "namespace": None,
                "declared": False,
                "owner": owner,
                "status": status,
                "detail": detail,
                "image": live["image"],
                "replicas": None,
            }
        )
    return extras


def classify_migration(box_services: list[dict], server_services: list[dict]) -> dict:
    """Split services by where the k3s strangler migration has reached.

    ``dual`` is the interesting bucket: a k8s copy on daniel-box running
    alongside the Docker twin that still serves the unsuffixed hostname.
    """
    box_names = {s["name"] for s in box_services if s["platform"] == "k8s"}
    server_names = {s["name"] for s in server_services if s["declared"]}
    return {
        "cutover": sorted(box_names - server_names),
        "dual": sorted(box_names & server_names),
        "docker_only": sorted(server_names - box_names),
    }


def build_model(
    global_vars: dict,
    host_vars: dict[str, dict],
    live: dict[str, dict],
    generated_at: str,
    roles: RoleIndex,
) -> dict:
    """Merge declared and live state into the structure the renderer consumes.

    *live* maps a host name to ``{"ok": bool, "error": str, "data": ...}``.
    Pure — every side effect happens before this is called, which is what makes
    the whole reconciliation layer testable.
    """
    hosts = []
    per_host_services: dict[str, list[dict]] = {}

    for host in HOSTS:
        hv = host_vars.get(host, {})
        declared = declared_services(host, hv, global_vars)
        info = live.get(host, {"ok": False, "error": "not collected", "data": {}})
        platform = "k8s" if any(s["platform"] == "k8s" for s in declared) else "docker"

        if info["ok"]:
            declared_names = {s["name"] for s in declared}
            if platform == "k8s":
                services = [reconcile_k8s(s, info["data"], roles) for s in declared]
            else:
                services = [reconcile_docker(s, info["data"], roles) for s in declared]
                services += find_extra_containers(info["data"], declared_names, roles)
        else:
            services = declared

        services.sort(key=lambda s: (not s["declared"], s["name"]))
        per_host_services[host] = services

        counts: dict[str, int] = {}
        for service in services:
            counts[service["status"]] = counts.get(service["status"], 0) + 1

        hosts.append(
            {
                "name": host,
                "ip": hv.get("server_ip", ""),
                "platform": platform,
                "reachable": info["ok"],
                "error": info.get("error", ""),
                "services": services,
                "counts": counts,
                "declared_count": len(declared),
                "routed_count": sum(1 for s in services if s["hostname"]),
                "authelia_count": sum(1 for s in services if s["authelia"]),
            }
        )

    migration = classify_migration(
        per_host_services.get("daniel-box", []),
        per_host_services.get("daniel-server", []),
    )
    all_services = [s for host in hosts for s in host["services"]]
    totals = {
        "services": len(all_services),
        "healthy": sum(1 for s in all_services if s["status"] == "healthy"),
        "degraded": sum(1 for s in all_services if s["status"] == "degraded"),
        "down": sum(1 for s in all_services if s["status"] in ("down", "missing")),
        "job": sum(1 for s in all_services if s["status"] == "job"),
        "companion": sum(1 for s in all_services if s["status"] == "companion"),
        "undeclared": sum(1 for s in all_services if s["status"] == "undeclared"),
        "unknown": sum(1 for s in all_services if s["status"] == "unknown"),
    }
    return {
        "generated_at": generated_at,
        "hosts": hosts,
        "migration": migration,
        "totals": totals,
        "domain": global_vars.get("domain", ""),
        "hostname_suffix": global_vars.get("k8s_hostname_suffix", ""),
    }


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------

# Catppuccin Mocha, matching the terminal these pages are generated from.
STYLE = """
:root {
  --base: #1e1e2e; --mantle: #181825; --crust: #11111b;
  --surface0: #313244; --surface1: #45475a; --surface2: #585b70;
  --text: #cdd6f4; --subtext0: #a6adc8; --overlay0: #6c7086;
  --blue: #89b4fa; --green: #a6e3a1; --yellow: #f9e2af; --red: #f38ba8;
  --mauve: #cba6f7; --peach: #fab387; --teal: #94e2d5; --lavender: #b4befe;
  --mono: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace;
}
* { box-sizing: border-box; }
body {
  margin: 0; padding: 2.5rem 1.5rem 4rem;
  background: var(--base); color: var(--text);
  font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
  line-height: 1.6; font-size: 15px;
}
.wrap { max-width: 1180px; margin: 0 auto; }
h1 { font-size: 1.85rem; font-weight: 650; letter-spacing: -0.02em; margin: 0 0 .35rem; }
h2 {
  font-size: 1.05rem; font-weight: 600; letter-spacing: .04em; text-transform: uppercase;
  color: var(--subtext0); margin: 3rem 0 1rem; padding-bottom: .5rem;
  border-bottom: 1px solid var(--surface1);
}
h3 { font-size: 1.15rem; font-weight: 600; margin: 0; letter-spacing: -0.01em; }
p { margin: 0 0 1rem; }
.lede { color: var(--subtext0); max-width: 68ch; }
.meta { color: var(--overlay0); font-size: .85rem; font-family: var(--mono); }
code, .mono { font-family: var(--mono); font-size: .85em; }

.kpis { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: .75rem; margin: 1.75rem 0 0; }
.kpi { background: var(--mantle); border: 1px solid var(--surface0); border-radius: 10px; padding: .9rem 1rem; }
.kpi .n { font-size: 1.9rem; font-weight: 650; line-height: 1.1; font-variant-numeric: tabular-nums; }
.kpi .l { font-size: .78rem; color: var(--overlay0); text-transform: uppercase; letter-spacing: .05em; margin-top: .15rem; }
.n.good { color: var(--green); } .n.warn { color: var(--yellow); }
.n.bad { color: var(--red); } .n.info { color: var(--blue); } .n.alt { color: var(--mauve); }

.hosts { display: grid; grid-template-columns: repeat(auto-fit, minmax(430px, 1fr)); gap: 1.25rem; }
.host { background: var(--mantle); border: 1px solid var(--surface0); border-radius: 12px; overflow: hidden; }
.host-head { padding: 1.1rem 1.25rem; border-bottom: 1px solid var(--surface0); background: var(--crust); }
.host-head .row { display: flex; align-items: baseline; justify-content: space-between; gap: 1rem; flex-wrap: wrap; }
.host-sub { color: var(--overlay0); font-size: .85rem; font-family: var(--mono); margin-top: .3rem; }
.host-body { padding: .5rem .6rem 1rem; }

.svc { display: flex; align-items: flex-start; gap: .7rem; padding: .5rem .65rem; border-radius: 8px; }
.svc:hover { background: var(--surface0); }
.svc + .svc { border-top: 1px solid rgba(69,71,90,.45); }
.svc-main { flex: 1; min-width: 0; }
.svc-name { font-weight: 550; font-family: var(--mono); font-size: .92rem; }
.svc-detail { color: var(--overlay0); font-size: .8rem; overflow-wrap: anywhere; }
.svc-tags { display: flex; gap: .3rem; flex-wrap: wrap; margin-top: .25rem; }

.dot { width: 9px; height: 9px; border-radius: 50%; flex: 0 0 auto; margin-top: .45rem; box-shadow: 0 0 0 2px var(--mantle); }
.dot.healthy { background: var(--green); } .dot.degraded { background: var(--yellow); }
.dot.down, .dot.missing { background: var(--red); }
.dot.job { background: var(--teal); } .dot.companion { background: var(--blue); }
.dot.undeclared { background: var(--mauve); } .dot.unknown { background: var(--overlay0); }

.tag { font-size: .68rem; font-family: var(--mono); padding: .1rem .4rem; border-radius: 4px;
       background: var(--surface0); color: var(--subtext0); border: 1px solid var(--surface1); }
.tag.auth { background: var(--lavender); color: var(--crust); border-color: var(--lavender); }
.tag.route { background: var(--blue); color: var(--crust); border-color: var(--blue); }
.tag.net { background: transparent; color: var(--teal); border-color: var(--surface2); }
.tag.ns { background: transparent; color: var(--peach); border-color: var(--surface2); }

.legend { display: flex; gap: 1.1rem; flex-wrap: wrap; margin: 1rem 0 0; font-size: .82rem; color: var(--subtext0); }
.legend span { display: flex; align-items: center; gap: .4rem; }
.legend .dot { margin-top: 0; }

.mig { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 1rem; }
.mig-col { background: var(--mantle); border: 1px solid var(--surface0); border-radius: 10px; padding: 1rem 1.1rem; }
.mig-col h4 { margin: 0 0 .2rem; font-size: .95rem; font-weight: 600; }
.mig-col .why { color: var(--overlay0); font-size: .82rem; margin: 0 0 .75rem; }
.mig-col ul { margin: 0; padding: 0; list-style: none; display: flex; flex-wrap: wrap; gap: .35rem; }
.mig-col li { font-family: var(--mono); font-size: .8rem; background: var(--surface0);
              border: 1px solid var(--surface1); border-radius: 5px; padding: .12rem .45rem; }
.mig-col.cutover h4 { color: var(--green); }
.mig-col.dual h4 { color: var(--yellow); }
.mig-col.docker h4 { color: var(--blue); }

.warn-box { background: rgba(243,139,168,.1); border: 1px solid var(--red); border-radius: 8px;
            padding: .7rem .9rem; margin: .75rem 0 0; color: var(--text); font-size: .87rem; }

table { width: 100%; border-collapse: collapse; font-size: .85rem; }
th, td { text-align: left; padding: .45rem .6rem; border-bottom: 1px solid var(--surface0); vertical-align: top; }
th { color: var(--overlay0); font-size: .74rem; text-transform: uppercase; letter-spacing: .05em; font-weight: 600; }
td.mono { font-family: var(--mono); }
.scroll { overflow-x: auto; }
details > summary { cursor: pointer; color: var(--subtext0); font-size: .9rem; padding: .4rem 0; }
footer { margin-top: 3rem; padding-top: 1.25rem; border-top: 1px solid var(--surface0);
         color: var(--overlay0); font-size: .82rem; }
"""

STATUS_LABELS = {
    "healthy": "Healthy",
    "degraded": "Degraded",
    "down": "Down",
    "missing": "Missing",
    "job": "Scheduled job",
    "companion": "Role companion",
    "undeclared": "Undeclared",
    "unknown": "Unknown",
}


def e(value: Any) -> str:
    """Escape a value for HTML text content."""
    return html.escape("" if value is None else str(value))


def _service_row(service: dict) -> str:
    status = service["status"]
    tags = []
    if service["hostname"]:
        target = service["hostname"]
        tags.append(f'<span class="tag route">{e(target)}:{e(service["port"])}</span>')
    elif service["port"]:
        tags.append(f'<span class="tag">:{e(service["port"])}</span>')
    if service["authelia"]:
        tags.append('<span class="tag auth">SSO</span>')
    if service.get("namespace"):
        tags.append(f'<span class="tag ns">ns/{e(service["namespace"])}</span>')
    for net in service["networks"]:
        tags.append(f'<span class="tag net">{e(net)}</span>')
    if not service["declared"]:
        tags.append('<span class="tag">not in inventory</span>')

    detail = service["detail"] or STATUS_LABELS.get(status, status)
    return (
        f'<div class="svc">'
        f'<span class="dot {e(status)}" title="{e(STATUS_LABELS.get(status, status))}"></span>'
        f'<div class="svc-main">'
        f'<div class="svc-name">{e(service["name"])}</div>'
        f'<div class="svc-detail">{e(STATUS_LABELS.get(status, status))} &middot; {e(detail)}</div>'
        f'<div class="svc-tags">{"".join(tags)}</div>'
        f"</div></div>"
    )


def _host_panel(host: dict) -> str:
    counts = host["counts"]
    summary_bits = [
        f"{counts.get(key, 0)} {STATUS_LABELS[key].lower()}"
        for key in ("healthy", "degraded", "down", "missing", "undeclared", "unknown")
        if counts.get(key)
    ]
    platform_label = (
        "k3s / Kubernetes" if host["platform"] == "k8s" else "Docker Compose"
    )
    warn = ""
    if not host["reachable"]:
        warn = (
            f'<div class="warn-box"><strong>Live state unavailable</strong> — showing '
            f"declared inventory only. {e(host['error'])}</div>"
        )
    rows = "".join(_service_row(s) for s in host["services"])
    return (
        f'<section class="host"><div class="host-head">'
        f'<div class="row"><h3>{e(host["name"])}</h3>'
        f'<span class="meta">{e(platform_label)}</span></div>'
        f'<div class="host-sub">{e(host["ip"])} &middot; {host["declared_count"]} declared '
        f"&middot; {host['routed_count']} routed &middot; {host['authelia_count']} SSO-gated</div>"
        f'<div class="host-sub">{e(" &middot; ".join(summary_bits)) if summary_bits else ""}</div>'
        f"{warn}</div>"
        f'<div class="host-body">{rows}</div></section>'
    )


def _migration_section(migration: dict, suffix: str) -> str:
    columns = [
        (
            "cutover",
            "Cut over to k3s",
            "Only on daniel-box. The Docker entry is gone; these serve their real hostname.",
            migration["cutover"],
        ),
        (
            "dual",
            "Running in both",
            f"Mid-strangler: the k8s copy answers <code>{e(suffix)}</code> while the Docker twin still serves the unsuffixed name.",
            migration["dual"],
        ),
        (
            "docker",
            "Docker only",
            "Not yet migrated — still exclusively on daniel-server.",
            migration["docker_only"],
        ),
    ]
    out = []
    for css, title, why, names in columns:
        items = (
            "".join(f"<li>{e(n)}</li>" for n in names) or '<li class="mono">none</li>'
        )
        out.append(
            f'<div class="mig-col {css}"><h4>{title} ({len(names)})</h4>'
            f'<p class="why">{why}</p><ul>{items}</ul></div>'
        )
    return f'<div class="mig">{"".join(out)}</div>'


def _table_view(model: dict) -> str:
    rows = []
    for host in model["hosts"]:
        for service in host["services"]:
            rows.append(
                "<tr>"
                f'<td class="mono">{e(service["name"])}</td>'
                f"<td>{e(host['name'])}</td>"
                f"<td>{e(service['platform'])}</td>"
                f"<td>{e(STATUS_LABELS.get(service['status'], service['status']))}</td>"
                f'<td class="mono">{e(service["hostname"] or "—")}</td>'
                f'<td class="mono">{e(service["image"] or "—")}</td>'
                f"<td>{e(service['detail'] or '—')}</td>"
                "</tr>"
            )
    return (
        "<details><summary>Full service table (sortable by eye, copy-pasteable)</summary>"
        '<div class="scroll"><table><thead><tr>'
        "<th>Service</th><th>Host</th><th>Platform</th><th>Status</th>"
        "<th>Hostname</th><th>Image</th><th>Detail</th>"
        f"</tr></thead><tbody>{''.join(rows)}</tbody></table></div></details>"
    )


def render_html(model: dict) -> str:
    """Render the model to a single self-contained HTML document."""
    totals = model["totals"]
    kpis = [
        ("info", totals["services"], "Services"),
        ("good", totals["healthy"], "Healthy"),
        ("warn", totals["degraded"], "Degraded"),
        ("bad", totals["down"], "Down / missing"),
        ("alt", totals["undeclared"], "Undeclared"),
        ("info", len(model["migration"]["cutover"]), "Cut over to k3s"),
    ]
    kpi_html = "".join(
        f'<div class="kpi"><div class="n {css}">{value}</div><div class="l">{e(label)}</div></div>'
        for css, value, label in kpis
    )
    legend = "".join(
        f'<span><span class="dot {key}"></span>{label}</span>'
        for key, label in STATUS_LABELS.items()
    )
    hosts_html = "".join(_host_panel(h) for h in model["hosts"])

    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="{PAGE_REFRESH_SECONDS}">
<title>Homelab infrastructure — daniel-box &amp; daniel-server</title>
<style>{STYLE}</style>
</head><body><div class="wrap">
<h1>Homelab infrastructure</h1>
<p class="lede">Declared state from <code>ansible/inventory/host_vars/</code>, overlaid with
live state from <code>docker ps</code> and <code>kubectl</code>. Regenerated on a timer, so
edits to the inventory and drift in the running fleet both show up here on their own.</p>
<p class="meta">Generated {e(model["generated_at"])} &middot; page reloads every {PAGE_REFRESH_SECONDS // 60} min</p>
<div class="kpis">{kpi_html}</div>
<div class="legend">{legend}</div>

<h2>k3s migration</h2>
{_migration_section(model["migration"], model["hostname_suffix"])}

<h2>Hosts</h2>
<div class="hosts">{hosts_html}</div>

<h2>All services</h2>
{_table_view(model)}

<footer>
Sources: <code>ansible/inventory/host_vars/daniel-box.yml</code>,
<code>ansible/inventory/host_vars/daniel-server.yml</code>, live <code>docker ps</code> on
daniel-server and <code>kubectl get deployments -A</code> on daniel-box.
daniel-pi is not covered by this map.
Regenerate with <code>uv run python scripts/gen_infra_map.py</code>.
</footer>
</div></body></html>
"""


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def collect_live(local_hostname: str) -> dict[str, dict]:
    """Gather live state for both hosts, tolerating failures per host."""
    live: dict[str, dict] = {}
    for host in HOSTS:
        if host == "daniel-box":
            ok, data, err = collect_k8s(host, local_hostname)
        else:
            ok, data, err = collect_docker(host, local_hostname)
        live[host] = {"ok": ok, "data": data, "error": err}
    return live


def main(argv: list[str] | None = None) -> int:
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
    args = parser.parse_args(argv)

    global_vars, host_vars = load_inventory()
    local_hostname = socket.gethostname()
    try:
        live = (
            {h: {"ok": False, "data": {}, "error": "--no-live"} for h in HOSTS}
            if args.no_live
            else collect_live(local_hostname)
        )
    except MissingToolError as exc:
        # Deliberately leave the existing page alone. Overwriting it with a
        # declared-only render would replace real data with a page that looks
        # healthy and reports nothing wrong.
        print(f"error: {exc}; leaving the previous map in place", file=sys.stderr)
        return 2
    generated_at = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M %Z")
    model = build_model(global_vars, host_vars, live, generated_at, load_roles())

    if args.json:
        json.dump(model, sys.stdout, indent=2)
        return 0

    args.output.parent.mkdir(parents=True, exist_ok=True)
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
