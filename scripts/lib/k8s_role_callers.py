#!/usr/bin/env python3
"""Which k8s roles reach another k8s role, read from the live tree: `role_callers`.

A helper role has no `containers_list` entry and so no deploy tag of its own. Every reader of
this graph asks the same question of it: which tags does a change to the helper reach? The
key readers' tag mapping (`narrow_lib/role_reads.py`) reads it. `lib.k8s_roles` re-exports it, where its
readers import it from.

A task file that does not load raises `CannotNarrow`, because it can hold the only edge to a
helper. The key readers refuse, the
landing dies naming the file, and the `--tags` expansion leaves the name for the tag validation
to refuse.

Split out of `lib.k8s_roles`, which sat at its length cap.

Run: uv run pytest scripts/lib/tests/test_k8s_role_callers.py
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

import re
from pathlib import Path

from lib import yaml_fast
from lib.narrow_git import CannotNarrow
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
# The same shape as `k8s_lookups.K8S_LOOKUP_RE`, which captures the whole file path rather
# than the owning role.
_ROLE_LOOKUP = re.compile(
    r"lookup\(\s*'(?:ansible\.builtin\.)?(?:file|template)'\s*,\s*playbook_dir\s*[+~]\s*"
    r"'/roles/k8s/([^/']+)/[^']+'\s*(?:,[^)]*)?\)"
)

# The third edge: a key in a role's defaults makes the renderer read a helper role for it.
# `image_builder_builds` reads image-builder's context template, and
# `k8s_autodeploy_snapshot_pvcs` reads volume-snapshot's defaults for the role's snapshot Job.
_DEFAULTS_KEY_ROLES = {
    "image_builder_builds": "image-builder",
    "k8s_autodeploy_snapshot_pvcs": "volume-snapshot",
}


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
        try:
            found.update(_ROLE_LOOKUP.findall(src.read_text(errors="ignore")))
        except OSError as exc:
            raise CannotNarrow(f"{src} does not load: {exc}") from exc
    return found


def _read_through_defaults(role_dir: Path) -> set[str]:
    """Every helper role the renderer reads for a key `role_dir`'s defaults set."""
    defaults = role_dir / "defaults" / "main.yml"
    if not defaults.is_file():
        return set()
    text = defaults.read_text(errors="ignore")
    return {
        role
        for key, role in _DEFAULTS_KEY_ROLES.items()
        if re.search(rf"^{key}:", text, re.MULTILINE)
    }


def role_callers(repo: Path | str | None = None) -> dict[str, set[str]]:
    """For each k8s role another role includes, `lookup()`s or reads through its defaults, those roles.

    A helper role has no `containers_list` entry and so no deploy tag of its own, but a
    caller's deploy applies it: an include runs its tasks under the caller's tag, and a lookup
    or a defaults key renders its file into the caller's manifests. A change to it is therefore applied by
    deploying its callers — which is what `scripts/deploy_tools/land_lib/land_tags.py` asks
    this to decide, so that landing a helper-role change alongside all of its callers stops
    reading as `needs-manual-apply`.

    Only ``roles/k8s`` is walked. The Pi's deleted Compose roles included `containers/common`, not a k8s
    role, and no k8s role is reachable from them.

    ``repo`` names the checkout to walk, for a caller that reads a tree other than the one
    this module was imported from (``role_reads.context_for(ref, cwd)``). Omitted, it walks
    ``lib.repo_paths.K8S_ROLES``, which is this file's own checkout.

    Raises:
        CannotNarrow: a task file does not load. It can hold the only edge from a caller to a
            helper, and a graph short by that edge names too few tags.
    """
    roles_dir = K8S_ROLES if repo is None else Path(repo) / K8S_ROLES.relative_to(REPO)
    callers: dict[str, set[str]] = {}
    if not roles_dir.is_dir():
        return callers
    # DECIDED: a lookup into a DECLARED role is an edge too (authelia and traefik ->
    # crowdsec), rather than filtered out. Filtering would need the
    # walked tree's host_vars, which the throwaway tree a test walks does not have. The
    # landing, the deployer, the broad narrowing and the `--tags` expansion stop at a declared
    # role, so they never read such an edge. `releases_consumers._closure` does not stop, and
    # it reads the edge only for a census role whose closure passes through crowdsec or
    # monitor-bridge.
    for role_dir in sorted(p for p in roles_dir.iterdir() if p.is_dir()):
        caller = role_dir.name
        reached = _looked_up(role_dir) | _read_through_defaults(role_dir)
        for task_file in sorted(role_dir.glob("tasks/*.yml")):
            try:
                reached |= _callees(yaml_fast.parse_yaml(task_file.read_text()))
            except (
                OSError,
                UnicodeDecodeError,
                yaml_fast.UnparsableYaml,
                RecursionError,
            ) as exc:
                raise CannotNarrow(f"{task_file} does not load: {exc}") from exc
        for callee in reached - {caller}:
            callers.setdefault(callee, set()).add(caller)
    return callers
