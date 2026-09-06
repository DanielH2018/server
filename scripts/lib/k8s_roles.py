#!/usr/bin/env python3
"""Which roles under ``ansible/roles/k8s/`` the manifest validator renders, and which it skips.

Split out of ``scripts/validate/k8s_manifests.py`` on 2026-09-04; that module re-exports every
name here, so an existing importer keeps working. The two exemption sets and
``is_manifest_template`` are the half other guards ask about
(``scripts/validate/tests/test_skip_roles_classes_hold.py``,
``scripts/diagnostics/probe_lib/health.py``), which is why they are their own module rather
than private to the validator.

``K8S_ROLES`` comes from ``lib.repo_paths`` rather than being derived a second time here.
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

import re
from pathlib import Path

import yaml

from lib import yaml_fast
from lib.render_guard import HOST_VARS as HOST_VARS_DIR, load_yaml
from lib.repo_paths import K8S_ROLES

__all__ = [
    "CALLER_RENDERED_ROLES",
    "HOST_VARS",
    "K8S_ROLES",
    "NO_MANIFEST_ROLES",
    "SKIP_ROLES",
    "is_manifest_template",
    "k8s_entries",
    "role_callers",
]

# The one host that declares k8s services, so this is a single file where the other inventory
# readers walk the whole directory.
HOST_VARS = HOST_VARS_DIR / "daniel-box.yml"
# Helper roles, included by service roles rather than deployed on their own. They have no
# containers_list entry because they are not services, so the platform check below would always
# fail for them.
#
# rollout-drain is pure tasks, waiting on the rollouts a batch of roles queued into
# k8s_pending_rollouts; it lives under roles/k8s/ only so that both deploy.yml and configarr can
# include it by name. cronjob-gate creates a one-off Job from the CALLER's CronJob with `kubectl
# create job --from=cronjob/<name>`, so the pod spec it runs is the caller's rendered manifest.
# volume-snapshot applies one Longhorn Snapshot CR per claim, built inline and piped to `kubectl
# apply -f -` — per-deploy state rather than part of a service's manifest set, and
# `ansible/tests/longhorn/test_volume_snapshot.py` is what checks its shape instead.
#
# Split into the two classes the comment above already distinguishes, because they rot in
# opposite directions and a single set cannot be checked either way. NO_MANIFEST_ROLES makes a
# claim about the tree that `test_skip_roles_classes_hold.py` asserts, so a role that grows its
# first manifest template stops being silently exempt. CALLER_RENDERED_ROLES makes the opposite
# claim, asserted the opposite way, so an entry that stops carrying templates is caught as a
# stale exemption rather than left as a name nobody can justify.
NO_MANIFEST_ROLES = {
    "manifests",
    "rollout-drain",
    "cronjob-gate",
    "volume-snapshot",
    "longhorn-api",  # resolves a fact only, same as cronjob-gate/volume-snapshot
    "volume-revert",  # reverts a volume through kubectl and the Longhorn API
    "game-stats-lib",  # ships stats_lib.py into valheim-stats/terraria-stats' ConfigMaps
    # declares sonarr's/radarr's Discord Connect notification over the app's own API — a row in
    # the app's database, which no manifest can express
    "arr-notification",
}

# These DO carry manifest templates; they are exempt for a different reason. Their templates
# render only with vars a CALLING role passes on its `include_role` task — which image, which
# Dockerfile, which claim — and this validator reads role defaults and inventory, not task-level
# `vars:` overrides. Rendering them standalone produces STUB-filled manifests that prove nothing.
#
# That exemption is a coverage gap, not a clean bill: four manifests (volume-claim's pvc and
# seed-pod, image-builder's build-job and context-configmap) are parsed as YAML nowhere, and are
# covered only by one securityContext property each in test_seed_pod_security_context.py and
# test_image_builder_security_context.py. Closing it means rendering them against a fixture of
# the caller vars; until then this names the gap where someone will look for it.
CALLER_RENDERED_ROLES = {
    "volume-claim",
    "image-builder",
}

SKIP_ROLES = NO_MANIFEST_ROLES | CALLER_RENDERED_ROLES


def is_manifest_template(path: Path) -> bool:
    """True if this `.j2` under a role's templates/ is a manifest this script should parse.

    A role may also ship a helper script (claude-otel's telemetry-health.sh.j2) or a Dockerfile
    for image-builder (homelab-mcp). Shell is rendered and linted by validate/shell_templates.py;
    a Dockerfile is consumed by buildctl. Parsing either here reports a comment line as malformed
    YAML.

    Module-level so `test_skip_roles_classes_hold.py` asks the same question this script does.
    A test that reimplemented the predicate would keep passing after this one changed.
    """
    return not path.name.endswith(".sh.j2") and not path.name.startswith("Dockerfile")


def k8s_entries() -> dict[str, dict]:
    """containers_list entries for the k8s platform, keyed by service name."""
    entries = load_yaml(HOST_VARS).get("containers_list") or []
    return {c["name"]: c for c in entries if c.get("platform") == "k8s"}


# The two ways one k8s role reaches another's tasks. `include_role`/`import_role` name it as
# `k8s/<role>`; the game-stats-lib consumers instead `import_tasks` a sibling role's file by
# path (`{{ role_path }}/../game-stats-lib/tasks/stage.yml`), which names no role at all. Both
# are real edges: whoever deploys the caller runs the callee's tasks.
_ROLE_KEYS = (
    "ansible.builtin.include_role",
    "include_role",
    "ansible.builtin.import_role",
    "import_role",
)
_TASKS_KEYS = (
    "ansible.builtin.import_tasks",
    "import_tasks",
    "ansible.builtin.include_tasks",
    "include_tasks",
)
_ROLE_NAME = re.compile(r"^k8s/([^/\s]+)$")
_SIBLING_TASKS = re.compile(r"\.\./([^/]+)/tasks/")


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
    for key in _TASKS_KEYS:
        value = node.get(key)
        path = value.get("file") if isinstance(value, dict) else value
        if isinstance(path, str) and (m := _SIBLING_TASKS.search(path)):
            found.add(m.group(1))
    for value in node.values():
        if isinstance(value, list | dict):
            found |= _callees(value)
    return found


def role_callers() -> dict[str, set[str]]:
    """For each k8s role reached from another role's tasks, the roles that reach it.

    The deploy-coverage question a helper role poses: it has no `containers_list` entry and so
    no deploy tag of its own, but `deploy.yml` runs it under every caller's tag. A change to it
    is therefore applied by deploying its callers — which is what
    `scripts/deploy_tools/land_tags.py` asks this to decide, so that landing a helper-role
    change alongside all of its callers stops reading as `needs-manual-apply` (issue #1397).

    Only ``roles/k8s`` is walked. The Pi's Compose roles include `containers/common`, not a k8s
    role, and no k8s role is reachable from them.
    """
    callers: dict[str, set[str]] = {}
    for tasks_dir in sorted(K8S_ROLES.glob("*/tasks")):
        caller = tasks_dir.parent.name
        for task_file in sorted(tasks_dir.glob("*.yml")):
            try:
                tasks = yaml_fast.safe_load(task_file.read_text())
            except yaml.YAMLError:
                continue
            for callee in _callees(tasks):
                if callee != caller:
                    callers.setdefault(callee, set()).add(caller)
    return callers
