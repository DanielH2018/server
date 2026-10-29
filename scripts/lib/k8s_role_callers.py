#!/usr/bin/env python3
"""Which k8s roles reach another k8s role, read from the live tree: `role_callers`.

A helper role has no `containers_list` entry and so no deploy tag of its own. Every reader of
this graph asks the same question of it: which tags does a change to the helper reach? The
landing (`land_lib/land_shared.py`), the deployer's `owned_only` split
(`deploy_tools/shared_role_callers.py`), the broad narrowing (`narrow_lib/broad.py`), the
`--tags` expansion (`deploy_lib/run.py`) and the release-staleness consumers
(`probe_lib/releases_consumers.py`) all read it. `lib.k8s_roles` re-exports it, where those
readers import it from.

Split out of `lib.k8s_roles`, which sat at its length cap.

Run: uv run pytest scripts/lib/tests/test_k8s_role_callers.py
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

import re
from pathlib import Path

import yaml

from lib import yaml_fast
from lib.repo_paths import K8S_ROLES, REPO

__all__ = ["role_callers"]

# How one k8s role reaches another's tasks: `include_role`/`import_role` naming it as
# `k8s/<role>`. Whoever deploys the caller runs the callee's tasks.
#
# An `import_tasks` of a sibling role's file by path
# (`{{ role_path }}/../<role>/tasks/<file>.yml`) is the same edge spelled without a role name,
# and the walk does not cover it: no role reaches a sibling by path.
_ROLE_KEYS = (
    "ansible.builtin.include_role",
    "include_role",
    "ansible.builtin.import_role",
    "import_role",
)
_ROLE_NAME = re.compile(r"^k8s/([^/\s]+)$")

# The second edge: a role's template or task `lookup()`s a file another k8s role owns, so the
# owner's bytes render into the reader's manifests. sonarr's and radarr's
# `templates/verify/notification.yaml.j2` read arr-notification's script and defaults this way,
# and since their includes went (#5561) that lookup is the only edge arr-notification has.
# The same shape as the deployer's `deploy_cross_role._K8S_LOOKUP_RE`, which answers the
# per-file question for a changed path; `scripts/lib` imports nothing from `deployer_lib`.
_ROLE_LOOKUP = re.compile(
    r"lookup\(\s*'(?:ansible\.builtin\.)?(?:file|template)'\s*,\s*playbook_dir\s*[+~]\s*"
    r"'/roles/k8s/([^/']+)/[^']+'\s*(?:,[^)]*)?\)"
)


def _callees(node) -> set[str]:
    """Every k8s role the tasks under `node` reach. Walks blocks, which nest tasks."""
    found: set[str] = set()
    if isinstance(node, list):
        for item in node:
            found |= _callees(item)
        return found
    if not isinstance(node, dict):
        return found
    for key in _ROLE_KEYS:
        value = node.get(key)
        name = value.get("name") if isinstance(value, dict) else value
        if isinstance(name, str) and (m := _ROLE_NAME.match(name.strip())):
            found.add(m.group(1))
    for value in node.values():
        if isinstance(value, list | dict):
            found |= _callees(value)
    return found


def _looked_up(role_dir: Path) -> set[str]:
    """Every k8s role whose file a template or task of `role_dir` `lookup()`s by path."""
    found: set[str] = set()
    for src in [*role_dir.glob("templates/**/*.j2"), *role_dir.glob("tasks/*.yml")]:
        found.update(_ROLE_LOOKUP.findall(src.read_text(errors="ignore")))
    return found


def role_callers(repo: Path | str | None = None) -> dict[str, set[str]]:
    """For each k8s role another role includes or `lookup()`s, the roles that do.

    A helper role has no `containers_list` entry and so no deploy tag of its own, but a
    caller's deploy applies it: an include runs its tasks under the caller's tag, and a lookup
    renders its file into the caller's manifests. A change to it is therefore applied by
    deploying its callers — which is what `scripts/deploy_tools/land_lib/land_tags.py` asks
    this to decide, so that landing a helper-role change alongside all of its callers stops
    reading as `needs-manual-apply`.

    Only ``roles/k8s`` is walked. The Pi's deleted Compose roles included `containers/common`, not a k8s
    role, and no k8s role is reachable from them.

    ``repo`` names the checkout to walk, for a caller that reads a tree other than the one
    this module was imported from (``narrow_broad.narrow(cwd=...)``). Omitted, it walks
    ``lib.repo_paths.K8S_ROLES``, which is this file's own checkout.
    """
    roles_dir = K8S_ROLES if repo is None else Path(repo) / K8S_ROLES.relative_to(REPO)
    callers: dict[str, set[str]] = {}
    if not roles_dir.is_dir():
        return callers
    # DECIDED: a lookup into a DECLARED role is an edge too (uptime-kuma -> monitor-bridge,
    # authelia and traefik -> crowdsec), rather than filtered out. Filtering would need the
    # walked tree's host_vars, which the throwaway tree a test walks does not have. The
    # landing, the deployer, the broad narrowing and the `--tags` expansion stop at a declared
    # role, so they never read such an edge. `releases_consumers._closure` does not stop, and
    # it reads the edge only for a census role whose closure passes through crowdsec or
    # monitor-bridge. `manifests` is the one such role, and that reader skips it by name.
    for role_dir in sorted(p for p in roles_dir.iterdir() if p.is_dir()):
        caller = role_dir.name
        reached = _looked_up(role_dir)
        for task_file in sorted(role_dir.glob("tasks/*.yml")):
            try:
                tasks = yaml_fast.safe_load(task_file.read_text())
            except yaml.YAMLError:
                continue
            reached |= _callees(tasks)
        for callee in reached - {caller}:
            callers.setdefault(callee, set()).add(caller)
    return callers
