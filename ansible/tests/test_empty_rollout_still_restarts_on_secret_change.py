"""Emptying `manifests_rollout` must not silently drop the config-change restart.

`roles/k8s/manifests` restarts a workload after a ConfigMap/Secret change, because
`kubectl apply` updates the object and nothing else — env is resolved once at pod start, so a
rotated secret reaches the Secret while the running pods keep using the old value.

That restart is gated on `manifests_rollout | default(manifests_service) | length > 0`. A role
that ships several Deployments, none named after the service, sets `manifests_rollout: ""` to
opt out of the shared WAIT — and takes the RESTART with it, without saying so. The deploy is
green either way, which is what makes this worth a test rather than a comment.

Found on 2026-08-30: rotating the two cloudflare-ddns Kuma push tokens updated the Secret,
restarted nothing, left both pods 7d14h old, and both monitors went DOWN behind `ok=340
changed=31 failed=0`.

The fix is `manifests_extra_rollouts`, whose own restart task carries no such gate. This guard
requires it of any role in that shape.

Both halves are asserted, per the repo's paired accept/reject rule: a role in the shape must
declare the extras (the reject case, which this guard exists to catch), and the gate the fix
depends on must stay ungated (the accept case — if someone adds the same
`manifests_rollout`-length condition to the extras task, the fix becomes inert and every role
here goes back to being silently broken while this file still passes).
"""

from __future__ import annotations

import re

import yaml
from _helpers import REPO as _REPO

_K8S_ROLES = _REPO / "ansible/roles/k8s"
_MANIFESTS = _K8S_ROLES / "manifests/tasks/main.yml"


def _include_vars(role_tasks) -> list[dict]:
    """The `vars:` of every `include_role: k8s/manifests` in a role's task file."""
    out = []
    for task in yaml.safe_load(role_tasks.read_text()) or []:
        if not isinstance(task, dict):
            continue
        include = (
            task.get("ansible.builtin.include_role") or task.get("include_role") or {}
        )
        if include.get("name") == "k8s/manifests":
            out.append(task.get("vars") or {})
    return out


def _renders_a_deployment(role_dir) -> bool:
    templates = role_dir / "templates"
    if not templates.is_dir():
        return False
    return any(
        "kind: Deployment" in path.read_text() for path in templates.glob("*.j2")
    )


def _restarts_privately(role_dir) -> bool:
    """Does the role run its own `rollout restart`, anywhere in its task files?

    Two roles legitimately do: claude-otel rolls a Deployment and a DaemonSet together in a
    private loop, and pihole rolls its two instances one at a time from an included
    `roll_one.yml`. Searching the whole `tasks/` tree rather than `main.yml` is the point —
    pihole's restart lives in the include, so a main.yml-only check would call it broken.
    """
    return any(
        "rollout restart" in path.read_text()
        for path in (role_dir / "tasks").rglob("*.yml")
    )


def _roles_that_empty_the_rollout() -> list[tuple[str, dict]]:
    found = []
    for tasks in sorted(_K8S_ROLES.glob("*/tasks/main.yml")):
        role_dir = tasks.parent.parent
        for variables in _include_vars(tasks):
            if variables.get("manifests_rollout") != "":
                continue
            if not variables.get("manifests_secret_files"):
                continue
            if not _renders_a_deployment(role_dir):
                continue
            if _restarts_privately(role_dir):
                continue
            found.append((role_dir.name, variables))
    return found


def test_a_role_that_empties_the_rollout_still_names_its_deployments():
    """The reject half: a Secret-rendering Deployment role must declare the extras."""
    missing = [
        name
        for name, variables in _roles_that_empty_the_rollout()
        if not variables.get("manifests_extra_rollouts")
    ]
    assert not missing, (
        "these roles set manifests_rollout: '' while rendering a Secret and a Deployment, so "
        "the shared config-change restart is skipped and a rotated secret never reaches the "
        f"running pods: {missing}. Name each Deployment in manifests_extra_rollouts."
    )


def test_the_shape_this_guard_protects_actually_exists():
    """A guard that matches nothing cannot fail. Prove it still selects a real role."""
    assert _roles_that_empty_the_rollout(), (
        "no role matches the shape this guard checks — either the shape is gone (delete this "
        "file) or the selector broke and the guard is now inert"
    )


def test_the_extra_rollouts_restart_is_not_gated_on_manifests_rollout():
    """The accept half: the escape hatch must stay ungated, or the fix above is inert."""
    tasks = yaml.safe_load(_MANIFESTS.read_text()) or []
    extras = [
        task
        for task in tasks
        if isinstance(task, dict)
        and "extra deployments" in (task.get("name") or "")
        and "rollout restart" in str(task.get("ansible.builtin.command", ""))
    ]
    assert len(extras) == 1, (
        "expected exactly one extra-rollouts restart task in k8s/manifests; found "
        f"{len(extras)} — the selector below is matching the wrong thing"
    )
    conditions = " ".join(str(c) for c in extras[0].get("when") or [])
    # `manifests_rollout_kind` is a DIFFERENT variable and legitimately appears here, so match
    # the name only where no identifier character follows it. A bare substring test flags the
    # kind and fails on correct code — which it did on the first run of this file.
    gated = re.search(r"\bmanifests_rollout\b(?!_)", conditions)
    assert not gated, (
        "the extra-rollouts restart has grown a manifests_rollout condition. That is the exact "
        "gate roles escape by naming their Deployments here, so adding it makes the fix inert "
        "and re-breaks every role in the shape — silently, behind a green deploy."
    )
