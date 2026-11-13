#!/usr/bin/env python3
"""Generate a single HTML page answering "what runs in this homelab".

THE PROBLEM. ~60 services are declared across two files
(cluster/services.yml and inventory/host_vars/daniel-pi.yml), and the only way to
answer "what runs here, on which host, behind which auth, backed up how" is to read
through 62 Ansible roles by hand. Every fact already lives in the repo — nothing
assembles them into one place, so the answer drifts out of date the moment someone
reads it instead of the current tree.

WHAT THIS DOES. Statically parses containers_list, the per-role IngressRoute macro
call and the k3s role's Longhorn backup-tier volume lists, then renders one self-contained HTML table — grouped by
host, one row per service. A fact this cannot derive prints "unknown" (or a more
specific reason) rather than a guess or a silently missing row: see FIELD NOTES below
for exactly which facts that is, and why.

WHY STATIC PARSING ONLY. This must never shell out to ansible or kubectl. A fresh
worktree has no Ansible collections installed (fresh-worktree-has-no-ansible-collections
in project memory), and inventory/*.yml contains SOPS-lookup and other Jinja
expressions that do not render outside a real deploy. Every value below is read with
`yaml.safe_load` and plain regex over template text — never executed.

FIELD NOTES (what is genuinely undecidable from the repo alone, and why):

  - Route domain suffix. `ingressroute.yml.j2` builds the hostname as
    "{{ hostname }}.local.{{ domain }}" (and, when k8s_public_route is on and the role
    does not pass public=false, also the bare domain). A baked suffix would pick one tier
    for every reader, so the catalog writes the suffix as the literal "<domain>". On the
    docs site those placeholders become links, resolved in the browser against the URL
    the reader is on — see scripts/docs/catalog_lib/route_facts.py. WHICH names a service answers on is
    derivable and is stated outright; only the suffix is not.
  - Docker (Pi) routes. daniel-pi is the only Docker host and is LAN-only — its services
    are bound to the LAN IP directly by their own compose `ports:` list rather than routed
    through Traefik, so "route" for a docker service is a fixed LAN-direct marker, never a
    hostname.
  - Backup tier PVC claim names. A PVC's `metadata.name` is very often a Jinja var
    (`{{ foo_k8s_claim }}`) rather than a literal string. This script resolves a
    single-variable reference by grepping that role's own defaults/main.yml for a
    literal scalar; if the var lives elsewhere (group_vars, a computed expression) the
    claim name — and therefore the tier — is reported unknown rather than guessed. A
    role (home-assistant is the one found) can also mount a PVC it never declares as
    its own `kind: PersistentVolumeClaim` — the claim is provisioned elsewhere and only
    referenced by `claimName:` in a pod's volumes list; this script also scans for that
    reference, but a claim referenced with neither form (e.g. hardcoded past a
    yet-undiscovered third pattern) would still read as "no PVC (stateless)". A claim named
    by a shell variable (`${DRILL}`, in a script's own manifest) reads as no declared tier.
  - owner. A k8s entry is not Ansible's to deploy, so its Auto-deploy cell names what applies
    it (`catalog_backup.owner_stance`): Flux through `promote`, gated by `land.sh`.

Run: uv run python scripts/docs/service_catalog.py --out /tmp/service_catalog.html
Tests: uv run pytest scripts/docs/tests/test_service_catalog.py
"""

from pathlib import Path


# Reach the sibling package directories: a directly-invoked script gets only its own
# directory on sys.path, and pyproject's `pythonpath` is a pytest setting.
from pathlib import Path as _Path
import sys as _sys

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

from docs.catalog_lib.catalog_backup import (
    backup_tier,
    claim_index,
    load_longhorn_tier_lists,
    owner_stance,
)
from docs.catalog_lib.catalog_facts import auth_tier, k8s_route
from docs.catalog_lib.catalog_model import K3S_DEFAULTS, K8S_ROLES, ServiceRow
from docs.catalog_lib.catalog_render import render_html, render_markdown
from lib.cli_help import ArgumentParser
from lib.estate import Estate, Inventory
from lib.inventory_lists import K8S_HOST
from lib.repo_paths import ALL_VARS, K8S_SERVICES

# The service list comes from lib.estate, the loader every docs generator reads the
# inventory through.


# Assembly


def build_rows(
    services: Path = K8S_SERVICES,
    k8s_roles: Path = K8S_ROLES,
    k3s_defaults: Path = K3S_DEFAULTS,
    all_vars: Path = ALL_VARS,
) -> list[ServiceRow]:
    """Build one `ServiceRow` per k8s service, each on the k8s host.

    Args:
        services: The k8s service list.
        k8s_roles: Root directory of the k8s roles.
        k3s_defaults: Path to the k3s role's `defaults/main.yml`, for the Longhorn tier lists.
        all_vars: Path to `group_vars/all.yml`.

    Returns:
        One `ServiceRow` per service, in `services` order.
    """
    estate = Estate(Inventory(all_vars=all_vars, services=services))
    tiers = load_longhorn_tier_lists(
        k3s_defaults, estate.group_vars, list(estate.k8s_entries().values())
    )
    # Built once: a claim mounted by one role and declared by another (media-data) resolves
    # through this index, and every k8s row reads it.
    claim_classes = claim_index(k8s_roles)
    k8s_namespace = estate.group_vars.get("k8s_namespace", "homelab")

    rows: list[ServiceRow] = []
    autodeploy = f"n/a: applied by {owner_stance()}"
    for entry in estate.services:
        rows.append(
            ServiceRow(
                name=entry["name"],
                host=K8S_HOST,
                route=k8s_route(entry, k8s_roles, all_vars),
                auth_tier=auth_tier(entry),
                backup_tier=backup_tier(
                    entry,
                    k8s_namespace,
                    tiers,
                    k8s_roles,
                    claim_classes,
                ),
                autodeploy=autodeploy,
            )
        )
    return rows


def main(argv: list[str] | None = None) -> int:
    """Build the service rows and write them as HTML or Markdown, per `--format`.

    The Markdown path writes through `finish_generator` (only on a body change, for the
    committed reference page); the HTML path writes unconditionally to the artifacts dir,
    which is not committed.

    Returns:
        0 on success.
    """
    parser = ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, required=True, help="output file path")
    parser.add_argument(
        "--format",
        choices=("html", "markdown"),
        default="html",
        help="output format (default: html, for the standalone artifact page)",
    )
    parser.add_argument(
        "--services",
        type=Path,
        default=K8S_SERVICES,
        help="override cluster/services.yml (tests)",
    )
    parser.add_argument(
        "--k8s-roles",
        type=Path,
        default=K8S_ROLES,
        help="override k8s/roles dir (tests)",
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

    rows = build_rows(args.services, args.k8s_roles, args.k3s_defaults, args.all_vars)

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
