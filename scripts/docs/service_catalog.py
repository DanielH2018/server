#!/usr/bin/env python3
"""Generate a single HTML page answering "what runs in this homelab".

THE PROBLEM. ~60 services are declared across two inventory files
(ansible/inventory/host_vars/{daniel-box,daniel-pi}.yml), and today the only way to
answer "what runs here, on which host, behind which auth, backed up how" is to read
through 62 Ansible roles by hand. Every fact already lives in the repo — nothing
assembles them into one place, so the answer drifts out of date the moment someone
reads it instead of the current tree.

WHAT THIS DOES. Statically parses containers_list, the per-role IngressRoute macro
call, the k3s role's Longhorn backup-tier volume lists, and each k8s role's
k8s_autodeploy declaration, then renders one self-contained HTML table — grouped by
host, one row per service. A fact this cannot derive prints "unknown" (or a more
specific reason) rather than a guess or a silently missing row: see FIELD NOTES below
for exactly which facts that is, and why.

WHY STATIC PARSING ONLY. This must never shell out to ansible or kubectl. A fresh
worktree has no Ansible collections installed (fresh-worktree-has-no-ansible-collections
in project memory), and ansible/inventory/*.yml contains SOPS-lookup and other Jinja
expressions that do not render outside a real deploy. Every value below is read with
`yaml.safe_load` and plain regex over template text — never executed.

FIELD NOTES (what is genuinely undecidable from the repo alone, and why):

  - Route domain suffix. `ingressroute.yml.j2` builds the hostname as
    "{{ hostname }}.local.{{ domain }}" (and, when k8s_public_route is on and the role
    does not pass public=false, also the bare domain). `domain` is SOPS-sourced with no
    static default, so the catalog writes the suffix as the literal "<domain>". On the
    docs site those placeholders become links, resolved in the browser against the URL
    the reader is on — see scripts/docs/route_facts.py. WHICH names a service answers on is
    derivable and is stated outright; only the suffix is not.
  - Docker (Pi) routes. daniel-pi sets `expose_mode: lan` — its services are bound to
    the LAN IP directly rather than routed through Traefik (see host_vars comment), so
    "route" for a docker service is a fixed LAN-direct marker, never a hostname. A
    future non-lan docker host would need its own derivation; this only handles `lan`.
  - Backup tier PVC claim names. A PVC's `metadata.name` is very often a Jinja var
    (`{{ foo_k8s_claim }}`) rather than a literal string. This script resolves a
    single-variable reference by grepping that role's own defaults/main.yml for a
    literal scalar; if the var lives elsewhere (group_vars, a computed expression) the
    claim name — and therefore the tier — is reported unknown rather than guessed. A
    role (home-assistant is the one found) can also mount a PVC it never declares as
    its own `kind: PersistentVolumeClaim` — the claim is provisioned elsewhere and only
    referenced by `claimName:` in a pod's volumes list; this script also scans for that
    reference, but a claim referenced with neither form (e.g. hardcoded past a
    yet-undiscovered third pattern) would still read as "no PVC (stateless)".
  - k8s_autodeploy. Every role/k8s/<name>/defaults/main.yml is SUPPOSED to declare this
    (ansible/filter_plugins/k8s_autodeploy.py enforces it at deploy time), but this
    script tolerates a missing declaration by reporting unknown, because a stray role
    mid-edit should not crash report generation the way it correctly crashes a deploy.

Run: uv run python scripts/docs/service_catalog.py --out /tmp/service_catalog.html
Tests: uv run pytest scripts/docs/tests/test_service_catalog.py
"""

from __future__ import annotations

import argparse
import html
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


# Reach the sibling package directories: a directly-invoked script gets only its own
# directory on sys.path, and pyproject's `pythonpath` is a pytest setting.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

from lib.docs_provenance import md_cell as _md_cell
from lib.render_guard import (
    ALL_VARS,
    HOST_VARS,
    REPO,
    containers_entries,
    host_files,
    load_yaml as _load_yaml,
)
from route_facts import linkify_fqdns, reachability, route_cell

K8S_ROLES = REPO / "ansible" / "roles" / "k8s"
K3S_DEFAULTS = REPO / "ansible" / "roles" / "setup" / "k3s" / "defaults" / "main.yml"

UNKNOWN = "unknown"


@dataclass
class ServiceRow:
    """One row of the service catalog: a single service's derived facts, host-scoped.

    Attributes:
        route: The IngressRoute/Traefik reachability derivation, or an `UNKNOWN` explanation.
        auth_tier: Whether the route sits behind Authelia, read from `use_authelia`.
        backup_tier: The Longhorn backup tier(s) its PVC(s) fall into, or "n/a" off k8s.
        autodeploy: Whether the GitOps deployer will auto-apply this service's image bumps.
    """

    name: str
    host: str
    platform: str  # "k8s" or "docker"
    route: str
    auth_tier: str
    backup_tier: str
    autodeploy: str


# containers_list — same source and shape scripts/deploy_tools/deploy_tags.py already parses.


# Kept as a name of its own: `describe` and the row builder both call it, and the tests import
# it directly. It is now a thin alias for the shared reader rather than a second copy of the
# `_`-prefix exclusion.
iter_host_files = host_files


def host_expose_mode(host_data: dict[str, Any]) -> str | None:
    return host_data.get("expose_mode")


# Route


def k8s_route(
    entry: dict[str, Any],
    k8s_roles: Path = K8S_ROLES,
    all_vars: Path = ALL_VARS,
) -> str:
    """Derive a k8s service's route cell from its role's IngressRoute template, if any.

    Args:
        entry: The service's `containers_list` entry.
        k8s_roles: Root directory of the k8s roles.
        all_vars: Path to `group_vars/all.yml`, read for the public-route default.

    Returns:
        A route cell string, or "no route (infra role)" when the role has no IngressRoute.
    """
    name = entry["name"]
    role_dir = k8s_roles / name
    if not (role_dir / "templates" / "ingressroute.yaml.j2").is_file():
        return "no route (infra role)"
    # ingressroute.yml.j2's own macro call is uniformly
    # `container_item.hostname | default(container_item.name)` — see the shared macro
    # docstring at ansible/templates/ingressroute.yml.j2.
    label = entry.get("hostname") or name
    # Reachability comes from route_facts so this page and networking.md cannot disagree
    # about the same service. It reads the role's own `public=false` and the cluster-wide
    # k8s_public_route together, rather than hedging with "if k8s_public_route" — that flag
    # has a value in plaintext group_vars, so printing the condition instead of the answer
    # made the reader do a lookup this generator can do for them.
    return route_cell(label, reachability(role_dir, all_vars))


def docker_route(entry: dict[str, Any], host_data: dict[str, Any]) -> str:
    if host_expose_mode(host_data) == "lan":
        return "LAN-direct (no Traefik route)"
    return UNKNOWN + " (docker route derivation only handles expose_mode: lan)"


def route_for(
    entry: dict[str, Any],
    platform: str,
    host_data: dict[str, Any],
    k8s_roles: Path = K8S_ROLES,
    all_vars: Path = ALL_VARS,
) -> str:
    """Derive `entry`'s route cell, dispatching to `k8s_route` or `docker_route` by platform."""
    if platform == "k8s":
        return k8s_route(entry, k8s_roles, all_vars)
    return docker_route(entry, host_data)


# Auth tier — containers_list.use_authelia is read directly by the IngressRoute macro
# (`container_item.use_authelia`) and by the docker traefik.yml.j2 macro alike, so this
# is a direct field read, not an inference from the route template.


def auth_tier(entry: dict[str, Any]) -> str:
    if "use_authelia" not in entry:
        return UNKNOWN + " (use_authelia not declared on this entry)"
    return "Authelia" if entry["use_authelia"] else "none (public/no-auth)"


# Backup tier (k8s / Longhorn only — Pi's Docker volumes are not Longhorn-backed)

_PVC_BLOCK_RE = re.compile(
    r"kind:\s*PersistentVolumeClaim.*?metadata:\s*\n\s*name:\s*(\{\{.*?\}\}|\S+)",
    re.DOTALL,
)
# Fallback for a role (home-assistant is the one seen so far) that mounts a PVC it never
# declares as its own `kind: PersistentVolumeClaim` object — the claim is provisioned
# elsewhere and only referenced by `claimName:` in a pod spec's volumes list.
_CLAIM_NAME_RE = re.compile(r"claimName:\s*(\{\{.*?\}\}|\S+)")
_SIMPLE_VAR_RE = re.compile(r"^\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}$")


def _pvc_claim_names(role_dir: Path) -> list[str]:
    """Literal or single-variable PVC claim name expressions a k8s role depends on.

    Returns the raw expression text (e.g. "authelia-config" or "{{ media_volume_claim }}")
    for every PersistentVolumeClaim declared in the role's templates/*.j2 files, plus any
    `claimName:` reference to a PVC declared elsewhere (see _CLAIM_NAME_RE above).
    """
    templates = role_dir / "templates"
    if not templates.is_dir():
        return []
    names = []
    for tmpl in sorted(templates.glob("*.j2")):
        text = tmpl.read_text()
        names.extend(_PVC_BLOCK_RE.findall(text))
        names.extend(_CLAIM_NAME_RE.findall(text))
    # De-duplicate while keeping order — a role that both declares its own PVC and
    # references it by claimName would otherwise double-count.
    seen: list[str] = []
    for name in names:
        if name not in seen:
            seen.append(name)
    return seen


def _resolve_claim_name(expr: str, role_dir: Path) -> str | None:
    """Resolve a PVC name expression to a literal string.

    Returns None if it can't be resolved from this role's own defaults/main.yml alone
    (see FIELD NOTES).
    """
    if not expr.startswith("{{"):
        return expr  # already literal
    match = _SIMPLE_VAR_RE.match(expr)
    if not match:
        return None
    var = match.group(1)
    defaults = _load_yaml(role_dir / "defaults" / "main.yml")
    value = defaults.get(var)
    if isinstance(value, str) and "{{" not in value:
        return value
    return None


def load_longhorn_tier_lists(
    k3s_defaults: Path = K3S_DEFAULTS,
) -> tuple[set[str], set[str]]:
    """(r2_volumes, weekly_volumes) — "namespace/claim" strings, from the k3s role's defaults.

    Everything else with a PVC falls into the `default` RecurringJob group, which Longhorn applies
    to any volume with no job of its own (daily, to B2) — see
    ansible/roles/setup/k3s/templates/longhorn-recurringjob.yaml.j2.
    """
    data = _load_yaml(k3s_defaults)
    r2 = set(data.get("k3s_longhorn_r2_volumes") or [])
    weekly = set(data.get("k3s_longhorn_weekly_volumes") or [])
    return r2, weekly


def backup_tier(
    entry: dict[str, Any],
    platform: str,
    k8s_namespace: str,
    r2_volumes: set[str],
    weekly_volumes: set[str],
    k8s_roles: Path = K8S_ROLES,
) -> str:
    """Derive `entry`'s Longhorn backup tier(s) from its role's PVC claim names.

    Resolves each PVC the role declares (or references by `claimName:`) to a literal claim
    name, then classifies `namespace/claim` against the R2 and weekly volume lists. A role
    with multiple PVCs in different tiers reports all of them, de-duplicated.

    Args:
        entry: The service's `containers_list` entry.
        platform: "k8s" or "docker" — only "k8s" is Longhorn-backed.
        k8s_namespace: The cluster namespace PVCs are classified under.
        r2_volumes: "namespace/claim" strings backed up daily to R2.
        weekly_volumes: "namespace/claim" strings backed up weekly to B2.
        k8s_roles: Root directory of the k8s roles.

    Returns:
        A semicolon-joined string of tier labels, "no PVC (stateless)", or "n/a" off k8s.
    """
    if platform != "k8s":
        return "n/a (Docker/Pi, not Longhorn-backed)"
    role_dir = k8s_roles / entry["name"]
    claim_exprs = _pvc_claim_names(role_dir)
    if not claim_exprs:
        return "no PVC (stateless)"
    tiers = []
    for expr in claim_exprs:
        claim = _resolve_claim_name(expr, role_dir)
        if claim is None:
            tiers.append(
                UNKNOWN
                + f" (PVC present, claim name not statically resolvable: {expr})"
            )
            continue
        full = f"{k8s_namespace}/{claim}"
        if full in r2_volumes:
            tiers.append("daily -> R2")
        elif full in weekly_volumes:
            tiers.append("weekly -> B2 (default target)")
        else:
            tiers.append("daily -> B2 (default group)")
    # Multiple PVCs on one role (e.g. pihole) can land in different tiers; report all,
    # de-duplicated, rather than picking one and hiding the rest.
    seen: list[str] = []
    for tier in tiers:
        if tier not in seen:
            seen.append(tier)
    return "; ".join(seen)


# Auto-deploy eligibility (k8s only — daniel-pi sets has_gitops: false)


def autodeploy_eligibility(
    entry: dict[str, Any],
    platform: str,
    host_data: dict[str, Any],
    k8s_roles: Path = K8S_ROLES,
) -> str:
    """Derive `entry`'s GitOps auto-deploy eligibility from its role's `k8s_autodeploy` default.

    Args:
        entry: The service's `containers_list` entry.
        platform: "k8s" or "docker" — only "k8s" has a GitOps auto-deploy path.
        host_data: The host's parsed `host_vars`, read for `has_gitops` off k8s.
        k8s_roles: Root directory of the k8s roles.

    Returns:
        "eligible", "denylisted (<reason>)", or an `UNKNOWN`/"n/a" explanation.
    """
    if platform != "k8s":
        if host_data.get("has_gitops") is False:
            return "n/a (host has no GitOps auto-deploy path)"
        return UNKNOWN + " (docker host's has_gitops not declared)"
    role_dir = k8s_roles / entry["name"]
    defaults = _load_yaml(role_dir / "defaults" / "main.yml")
    if "k8s_autodeploy" not in defaults:
        return UNKNOWN + " (role declares no k8s_autodeploy stance)"
    value = defaults["k8s_autodeploy"]
    if value is True:
        return "eligible"
    reason = defaults.get("k8s_autodeploy_reason")
    if isinstance(reason, str) and reason.strip():
        return f"denylisted ({reason.strip()})"
    return "denylisted (no reason given)"


# Assembly


def build_rows(
    host_vars: Path = HOST_VARS,
    k8s_roles: Path = K8S_ROLES,
    k3s_defaults: Path = K3S_DEFAULTS,
    all_vars: Path = ALL_VARS,
) -> list[ServiceRow]:
    """Build one `ServiceRow` per `containers_list` entry across every host.

    Args:
        host_vars: Directory holding each host's `host_vars` file.
        k8s_roles: Root directory of the k8s roles.
        k3s_defaults: Path to the k3s role's `defaults/main.yml`, for the Longhorn tier lists.
        all_vars: Path to `group_vars/all.yml`.

    Returns:
        One `ServiceRow` per service, across every host in `host_vars`.
    """
    r2_volumes, weekly_volumes = load_longhorn_tier_lists(k3s_defaults)
    k8s_namespace = _load_yaml(all_vars).get("k8s_namespace", "homelab")

    rows: list[ServiceRow] = []
    for path in iter_host_files(host_vars):
        host_data = _load_yaml(path)
        host = path.stem
        for entry in containers_entries(path):
            name = entry["name"]
            platform = entry.get("platform", "docker")
            rows.append(
                ServiceRow(
                    name=name,
                    host=host,
                    platform=platform,
                    route=route_for(entry, platform, host_data, k8s_roles, all_vars),
                    auth_tier=auth_tier(entry),
                    backup_tier=backup_tier(
                        entry,
                        platform,
                        k8s_namespace,
                        r2_volumes,
                        weekly_volumes,
                        k8s_roles,
                    ),
                    autodeploy=autodeploy_eligibility(
                        entry, platform, host_data, k8s_roles
                    ),
                )
            )
    return rows


# HTML rendering — Catppuccin Mocha, self-contained, inline CSS.

_CSS = """
:root {
  --base: #1e1e2e; --text: #cdd6f4; --surface0: #313244; --surface1: #45475a;
  --overlay0: #6c7086; --blue: #89b4fa; --green: #a6e3a1; --yellow: #f9e2af;
  --red: #f38ba8; --mauve: #cba6f7; --crust: #11111b;
}
* { box-sizing: border-box; }
body {
  background: var(--base); color: var(--text); font-family: system-ui, sans-serif;
  margin: 0; padding: 2rem;
}
h1 { margin-top: 0; }
p.subtitle { color: var(--overlay0); max-width: 60rem; }
.stats { display: flex; gap: 1rem; flex-wrap: wrap; margin: 1.5rem 0; }
.stat {
  background: var(--surface0); border-radius: 0.5rem; padding: 0.75rem 1rem; min-width: 9rem;
}
.stat .n { font-size: 1.5rem; font-weight: 700; display: block; }
.stat .l { color: var(--overlay0); font-size: 0.8rem; }
h2 { border-bottom: 1px solid var(--surface1); padding-bottom: 0.3rem; margin-top: 2.5rem; }
table { border-collapse: collapse; width: 100%; margin-bottom: 1rem; }
th, td { text-align: left; padding: 0.5rem 0.75rem; border-bottom: 1px solid var(--surface0); }
th { color: var(--overlay0); font-weight: 600; font-size: 0.85rem; text-transform: uppercase; }
tr:hover td { background: var(--surface0); }
.badge {
  display: inline-block; border-radius: 0.35rem; padding: 0.1rem 0.5rem;
  font-size: 0.8rem; font-weight: 600; color: var(--crust);
}
.badge-k8s { background: var(--blue); }
.badge-docker { background: var(--mauve); }
.badge-auth { background: var(--green); }
.badge-noauth { background: var(--yellow); }
.badge-unknown { background: var(--red); }
.badge-na { background: var(--overlay0); }
.muted { color: var(--overlay0); }
"""


def _badge(text: str, cls: str) -> str:
    return f'<span class="badge {cls}">{html.escape(text)}</span>'


def _platform_badge(platform: str) -> str:
    return _badge(platform, "badge-k8s" if platform == "k8s" else "badge-docker")


def _auth_badge(tier: str) -> str:
    if tier.startswith(UNKNOWN):
        return _badge(tier, "badge-unknown")
    if tier == "Authelia":
        return _badge(tier, "badge-auth")
    return _badge(tier, "badge-noauth")


def _autodeploy_badge(text: str) -> str:
    if text.startswith(UNKNOWN):
        cls = "badge-unknown"
    elif text.startswith("eligible"):
        cls = "badge-auth"
    elif text.startswith("n/a"):
        cls = "badge-na"
    else:
        cls = "badge-noauth"
    return _badge(text, cls)


def _count_unknown(rows: list[ServiceRow], field: str) -> int:
    return sum(1 for r in rows if getattr(r, field).startswith(UNKNOWN))


def render_markdown(rows: list[ServiceRow]) -> str:
    """The service catalogue as a MkDocs page, grouped by host.

    A sibling of render_html over the same rows, not a second derivation: build_rows()
    stays the only place that reads the tree.

    Ordering is by host then name rather than by input order. An unstable page would
    make the docs-refresh cron commit on every run.
    """
    from lib.docs_provenance import generated_banner

    hosts = sorted({r.host for r in rows})
    parts = [generated_banner("scripts/docs/service_catalog.py")]
    parts.append("# Services\n")
    parts.append(f"{len(rows)} service(s) declared across {len(hosts)} host(s).\n")

    header = "| Service | Platform | Route | Auth | Backup tier | Auto-deploy |"
    divider = "|---|---|---|---|---|---|"

    for host in hosts:
        host_rows = sorted((r for r in rows if r.host == host), key=lambda r: r.name)
        parts.append(f"\n## {host}\n")
        parts.append(f"{len(host_rows)} service(s).\n")
        parts.append(header)
        parts.append(divider)
        for row in host_rows:
            cells = (
                row.name,
                row.platform,
                # Markdown only. The stored value stays plain text for render_html and
                # every text consumer; only the docs site can resolve these into links.
                linkify_fqdns(row.route),
                row.auth_tier,
                row.backup_tier,
                row.autodeploy,
            )
            parts.append("| " + " | ".join(_md_cell(c) for c in cells) + " |")

    unknowns = sum(
        _count_unknown(rows, field)
        for field in ("route", "auth_tier", "backup_tier", "autodeploy")
    )
    parts.append(
        f"\n## Underivable facts\n\n{unknowns} field(s) read `{UNKNOWN}`. "
        "A fact with no machine-readable source prints its reason rather than a guess — "
        "see the FIELD NOTES section of `scripts/docs/service_catalog.py` for which facts "
        "those are and why.\n"
    )
    # rstrip before the final newline: the parts already end in one, and a file ending
    # "\n\n" is rewritten by the end-of-file-fixer hook. A generated file a hook keeps
    # rewriting would fail the docs-refresh cron's commit on every run.
    return "\n".join(parts).rstrip("\n") + "\n"


def render_html(rows: list[ServiceRow]) -> str:
    """Render `rows` as the standalone HTML service-catalog artifact, one table per host.

    Args:
        rows: Service rows as returned by `build_rows`.

    Returns:
        A complete, self-contained HTML document.
    """
    hosts = sorted({r.host for r in rows})
    total = len(rows)
    unknown_fields = ["route", "auth_tier", "backup_tier", "autodeploy"]
    stats = "".join(
        f'<div class="stat"><span class="n">{_count_unknown(rows, f)}</span>'
        f'<span class="l">unknown {f.replace("_", " ")}</span></div>'
        for f in unknown_fields
    )
    stats = (
        f'<div class="stat"><span class="n">{total}</span><span class="l">services</span></div>'
        + f'<div class="stat"><span class="n">{len(hosts)}</span><span class="l">hosts</span></div>'
        + stats
    )

    sections = []
    for host in hosts:
        host_rows = sorted((r for r in rows if r.host == host), key=lambda r: r.name)
        body = "".join(
            "<tr>"
            f"<td>{html.escape(r.name)}</td>"
            f"<td>{_platform_badge(r.platform)}</td>"
            f'<td class="muted">{html.escape(r.route)}</td>'
            f"<td>{_auth_badge(r.auth_tier)}</td>"
            f'<td class="muted">{html.escape(r.backup_tier)}</td>'
            f"<td>{_autodeploy_badge(r.autodeploy)}</td>"
            "</tr>"
            for r in host_rows
        )
        sections.append(
            f'<h2>{html.escape(host)} <span class="muted">({len(host_rows)} services)</span></h2>'
            "<table><thead><tr><th>Service</th><th>Platform</th><th>Route</th>"
            "<th>Auth</th><th>Backup tier</th><th>Auto-deploy</th></tr></thead>"
            f"<tbody>{body}</tbody></table>"
        )

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Homelab Service Catalog</title>
<style>{_CSS}</style>
</head>
<body>
<h1>Homelab Service Catalog</h1>
<p class="subtitle">Generated statically from ansible/inventory and ansible/roles/k8s —
never from a live deploy. "unknown" means the fact genuinely cannot be derived from the
repo alone; see scripts/docs/service_catalog.py's FIELD NOTES for why.</p>
<div class="stats">{stats}</div>
{"".join(sections)}
</body>
</html>
"""


def main(argv: list[str] | None = None) -> int:
    """Build the service rows and write them as HTML or Markdown, per `--format`.

    The Markdown path writes through `finish_generator` (only on a body change, for the
    committed reference page); the HTML path writes unconditionally to the artifacts dir,
    which is not committed.

    Returns:
        0 on success.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, required=True, help="output file path")
    parser.add_argument(
        "--format",
        choices=("html", "markdown"),
        default="html",
        help="output format (default: html, for the standalone artifact page)",
    )
    parser.add_argument(
        "--host-vars",
        type=Path,
        default=HOST_VARS,
        help="override host_vars dir (tests)",
    )
    parser.add_argument(
        "--k8s-roles",
        type=Path,
        default=K8S_ROLES,
        help="override roles/k8s dir (tests)",
    )
    parser.add_argument(
        "--k3s-defaults",
        type=Path,
        default=K3S_DEFAULTS,
        help="override k3s defaults/main.yml",
    )
    parser.add_argument(
        "--all-vars", type=Path, default=ALL_VARS, help="override group_vars/all.yml"
    )
    args = parser.parse_args(argv)

    rows = build_rows(args.host_vars, args.k8s_roles, args.k3s_defaults, args.all_vars)

    if args.format == "markdown":
        from lib.docs_provenance import finish_generator

        # Not write_text: the banner's timestamp moves on every run, so an
        # unconditional write would make the docs-refresh cron commit on every run
        # for no content change.
        return finish_generator(
            "service_catalog", args.out, rows, render_markdown, "service"
        )

    # The HTML path targets ~/.claude/artifacts/, which is not committed and has no
    # diff to protect, so it stays an unconditional write.
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(render_html(rows))
    print(f"service_catalog: wrote {len(rows)} service(s) to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
