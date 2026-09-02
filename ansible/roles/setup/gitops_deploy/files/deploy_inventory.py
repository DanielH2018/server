# ansible/roles/setup/gitops_deploy/files/deploy_inventory.py
"""What this host declares, read from host_vars text.

`declared_services` and `declared_k8s_services` parse `containers_list` without yaml (the
unit runs under `uv run --no-project`), `reroute_k8s_services` moves a role the Docker regexes
matched onto the k8s plane when the host declares it there, and `stale_rendered_services`
finds a rendered compose the inventory no longer owns.
"""

from __future__ import annotations

import re
from dataclasses import replace

from deploy_changes import ChangeSet

# One containers_list entry: the `- name:` line plus everything indented under it up to the next
# `- name:` at the same (2-space) indent, or EOF. Two-space list indent is the repo-wide inventory
# convention; matching on it (rather than YAML-parsing) keeps this module stdlib-only and immune to
# the Jinja expressions inventory values carry.
_DECLARED_ENTRY = re.compile(
    r"^  - name: (\S+)(.*?)(?=^  - name: |\Z)", re.MULTILINE | re.DOTALL
)
# `platform: <value>` at the sub-key indent (4 spaces) within one entry's block.
_ENTRY_PLATFORM = re.compile(r"^    platform:\s*(\S+)", re.MULTILINE)


def declared_services(hostvars_text: str) -> set[str]:
    """Docker-platform service names declared in a host's containers_list.

    `platform: k8s` entries (default `docker` when the key is absent — see
    `ansible/inventory/host_vars/_example.yml`) are deliberately excluded: this deployer's
    stale-compose watchdog (`stale_rendered_services`) diffs against `containers/<svc>/` dirs
    rendered by deploy.yml's DOCKER play only, so a platform: k8s entry counting as "declared"
    here would let a leftover rendered compose for a service that migrated to k8s (a real stale
    dir) hide behind it as phantom-declared, instead of being flagged.
    """
    out: set[str] = set()
    for m in _DECLARED_ENTRY.finditer(hostvars_text):
        name, block = m.group(1), m.group(2)
        pm = _ENTRY_PLATFORM.search(block)
        platform = pm.group(1) if pm else "docker"
        if platform == "docker":
            out.add(name)
    return out


def declared_k8s_services(hostvars_text: str) -> set[str]:
    """The `platform: k8s` service names declared in a host's containers_list.

    The counterpart to declared_services(), used to catch a same-named Docker role that is
    actually k8s on THIS host (see reroute_k8s_services).
    """
    out: set[str] = set()
    for m in _DECLARED_ENTRY.finditer(hostvars_text):
        name, block = m.group(1), m.group(2)
        pm = _ENTRY_PLATFORM.search(block)
        platform = pm.group(1) if pm else "docker"
        if platform == "k8s":
            out.add(name)
    return out


def reroute_k8s_services(cs: ChangeSet, k8s_services: set[str]) -> ChangeSet:
    """Move any `cs.services` entry that is platform: k8s on THIS host into `cs.k8s`.

    services_from_changed_paths maps ansible/roles/containers/<svc>/{templates,files}/ changes to
    <svc> by NAME ALONE, with no knowledge of which platform this host actually runs that service
    under (e.g. wg-easy is a Docker role used by daniel-pi, but platform: k8s on daniel-box).
    Deploying such a match with `--tags <svc>` resolves to deploy.yml's K8S play, not the Docker
    one _ACTIVE_CONFIG assumed — an idempotent no-op whose health gate is silently skipped
    (containers_for() finds no rendered docker-compose.yml for a k8s entry), instead of the
    defer-and-alert a k8s-platform change should get (same as ansible/roles/k8s/** changes).
    """
    moved = cs.services & k8s_services
    if not moved:
        return cs
    return replace(cs, services=cs.services - moved, k8s=cs.k8s | moved)


def stale_rendered_services(rendered: list[str], declared: set[str]) -> list[str]:
    """Rendered compose dirs with no containers_list entry — the stale-compose trap.

    A service retired or migrated off this host leaves containers/<svc>/ behind unless the
    cutover cleans it up; the phantom compose then feeds containers_for(), the health gate
    polls a container that will never run again, and a healthy push rolls back with a hold
    (code-server 2026-08-10, then the kopia/terraria cutover the same day — the second
    occurrence is why this is now a machine check instead of an operator memory).
    """
    return sorted(set(rendered) - declared)
