# ansible/roles/setup/gitops_deploy/files/deploy_logic.py
"""Pure decision logic for the GitOps deployer (no I/O — unit-tested).

`services_from_changed_paths` maps a git-diff file list to the set of active
container services to redeploy, or flags a "broad" change (shared template /
inventory) that the deployer must defer to a manual full deploy.

`next_action` decides what a poll tick should do given the local/origin HEADs
and any recorded known-bad (hold) SHA.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# Active container template: roles/containers/<svc>/templates/docker-compose.yml.j2
# (the negative lookahead excludes archive/<svc>/...).
_ACTIVE_TPL = re.compile(
    r"^ansible/roles/containers/(?!archive/)([^/]+)/templates/docker-compose\.yml\.j2$"
)
# A `container_name:` line in a rendered docker-compose.yml.
_CONTAINER_NAME = re.compile(r'^\s*container_name:\s*["\']?([^\s"\']+)["\']?\s*$')
# Changes whose blast radius we don't try to scope automatically.
_BROAD_PREFIXES = (
    "ansible/templates/",                 # shared macros (traefik/networks/resources/...)
    "ansible/inventory/",                 # host_vars / group_vars
    "ansible/roles/containers/common/",   # shared deploy path
    "ansible/deploy.yml",
    "ansible/filter_plugins/",            # toposort
)


@dataclass
class ChangeSet:
    services: set[str] = field(default_factory=set)
    broad: bool = False


def services_from_changed_paths(paths: list[str]) -> ChangeSet:
    cs = ChangeSet()
    for p in paths:
        if any(p.startswith(prefix) for prefix in _BROAD_PREFIXES):
            cs.broad = True
            continue
        m = _ACTIVE_TPL.match(p)
        if m:
            cs.services.add(m.group(1))
    return cs


def next_action(local_head: str, origin_head: str, hold_sha: str | None) -> str:
    if origin_head == local_head:
        return "noop"
    if hold_sha is not None and origin_head == hold_sha:
        return "skip_hold"
    return "deploy"


def container_names(compose_text: str) -> list[str]:
    """Every `container_name:` declared in a rendered docker-compose.yml, in order.

    The deployer health-gates these, not the role/service name: a single role
    often runs several containers and the Renovate-bumped image's container is
    usually NOT the role-named one (e.g. `cadvisor` lives in the `prometheus`
    role, `scrutiny-influxdb` in `scrutiny`).
    """
    out: list[str] = []
    for line in compose_text.splitlines():
        m = _CONTAINER_NAME.match(line)
        if m and m.group(1) not in out:
            out.append(m.group(1))
    return out
