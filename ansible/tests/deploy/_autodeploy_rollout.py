"""Is a Deployment or DaemonSet credited as gated -- the rollout half of the derivation.

A role's primary rollout is gated by `roles/k8s/manifests`; every other workload it renders
must be named in `manifests_extra_rollouts` or it deploys ungated. A role that sets
`manifests_rollout: ""` skips the primary gate entirely, so it is an offender unless every
workload it renders, batch included, is gated by something this module can see.
Consumed by `test_k8s_autodeploy_rollout_gates.py`.
"""

from __future__ import annotations

import re
from pathlib import Path

from _autodeploy import _LITERAL_NAME, _batch_templates, _deployment_templates
from _autodeploy_batch import _batch_gated_names


def _sets_empty_rollout(role: Path) -> bool:
    """Whether the role passes `manifests_rollout: ''`.

    Tolerates a trailing comment (`manifests_rollout: ''  # nothing to roll`) — this repo's
    house style comments nearly every var, and a raw-text matcher requiring end-of-line right
    after the closing quote would go blind to that shape while `roles/k8s/manifests` itself
    still evaluates the same value as empty and skips the rollout entirely. Without this, both
    `_rollout_gate_offender` and the Deployment gate below would read a commented empty rollout
    as "sets no `manifests_rollout` at all" rather than "sets it to nothing."
    """
    tasks = role / "tasks/main.yml"
    if not tasks.is_file():
        return False
    return bool(
        re.search(
            r"""manifests_rollout:\s*(""|'')\s*(?:#.*)?$""",
            tasks.read_text(),
            re.MULTILINE,
        )
    )


def _rollout_gate_offender(role: Path) -> bool:
    """Whether a role with `manifests_rollout: ''` has no gate at all.

    `manifests_rollout: ''` skips the PRIMARY rollout's wait and stability soak. The general
    rule (task-3-rulings-2.md S5, generalising R2 rather than special-casing it): such a role
    is an offender UNLESS every workload it renders — Deployment, DaemonSet and batch alike —
    is gated by some mechanism this file can see, and it renders at least one workload total.

    That single rule covers three shapes that used to need three different checks:

    - A batch-only role (no Deployment/DaemonSet at all) whose rendered Jobs/CronJobs are all
      credited by `_batch_gated_names` — this is R2's original case.
    - A role that skips the PRIMARY rollout but gates its Deployment through
      `manifests_extra_rollouts` instead. Extras roll and soak independently of the primary —
      `roles/k8s/manifests/tasks/main.yml`'s extra-rollout queue task carries no
      `manifests_rollout | length > 0` condition — so `_ungated_deployments(role) == []` is
      already proof this Deployment IS gated, and an unconditional "renders a Deployment ⇒
      offender" (R2's own rule, before this generalisation) falsely accused this shape: the
      reviewer measured `_rollout_gate_offender: True` while `_ungated_deployments: []`.
    - A role rendering an ungated Deployment `_deployment_templates` can't see by itself (a
      quoted or trailing-comment `kind:` line, or a second Deployment in a `---`-split
      template) — `_ungated_deployments` already resolves each rendered Deployment's name
      against the gated set, so this shape is caught the same way an ordinary ungated
      Deployment is, without a separate "renders any Deployment at all" check.

    The condition on ALL of that is `manifests_rollout: ''`, written literally in the role's
    tasks. A role that does not write it returns False at the top and is never judged here —
    including a role that renders no workload at all, which is the part this docstring used to
    overstate. It claimed a role rendering nothing is "still an offender"; that holds only when
    the role ALSO sets `manifests_rollout: ''`. `n8n-images` is the counterexample: it renders
    no Deployment and no batch template, and it is not an offender, because it never includes
    `k8s/manifests` and so never passes a `manifests_rollout` for `_sets_empty_rollout` to find.

    So the true statement is narrower, in two parts:

    - A role that never calls `k8s/manifests` is outside this guard entirely. Its workloads, if
      any, reach the cluster some other way, and whatever gates them is not
      `manifests_rollout`. `test_auto_deployable_roles_gate_every_batch_workload_they_render`
      and `_GATING_SHARED_ROLES` are what cover that shape.
    - A role that DOES write `manifests_rollout: ''` and renders no workload is an offender.
      Rendering nothing is not evidence of a gate, only an absence of anything to check, so it
      stays fail-closed and must declare `k8s_autodeploy: false` instead.
    """
    if not _sets_empty_rollout(role):
        return False
    if _ungated_deployments(role):
        return True
    batch = _batch_templates(role)
    total_workloads = len(_deployment_templates(role)) + len(batch)
    if not total_workloads:
        return True
    gated = _batch_gated_names(role)
    return any(not name or name not in gated for _, name in batch)


def _extra_rollouts(role: Path) -> set[str]:
    """Deployment names the role gates via `manifests_extra_rollouts`.

    Parsed with a regex rather than yaml.safe_load: tasks/main.yml is Jinja-templated, and a
    role is free to build the list from a variable. A name this cannot see reads as ungated,
    which fails the guard — the safe direction.
    """
    tasks = role / "tasks/main.yml"
    if not tasks.is_file():
        return set()
    block = re.search(
        r"^\s*manifests_extra_rollouts:\s*$\n((?:\s*-\s*name:.*\n?)+)",
        tasks.read_text(),
        re.MULTILINE,
    )
    if not block:
        return set()
    return set(re.findall(r"-\s*name:\s*(\S+)", block.group(1)))


def _primary_rollout_name(role: Path) -> str:
    """The Deployment name `roles/k8s/manifests` waits on as this role's primary rollout.

    Mirrors `manifests_rollout | default(manifests_service)` from
    `roles/k8s/manifests/tasks/main.yml`. Every role that calls the shared role sets
    `manifests_service` to a literal string, and every `manifests_rollout` override is
    likewise a literal (checked repo-wide), so a regex match is safe here. A role that never
    calls k8s/manifests resolves to '', which matches no real Deployment name.

    Tolerates a trailing comment after the value, the same widening R3/`_sets_empty_rollout`
    made — without it, `manifests_rollout: ''  # nothing to roll` disagreed between the two
    matchers reading the same variable: `_sets_empty_rollout` said "empty" while this one, still
    anchored at end-of-line right after the closing quote, fell through to `manifests_service`
    and returned the real service name instead (task-3-rulings-2.md S4).
    """
    tasks = role / "tasks/main.yml"
    if not tasks.is_file():
        return ""
    text = tasks.read_text()
    rollout = re.search(
        r"""^\s*manifests_rollout:\s*(?:"([^"]*)"|'([^']*)'|(\S+))\s*(?:#.*)?$""",
        text,
        re.MULTILINE,
    )
    if rollout:
        return next(g for g in rollout.groups() if g is not None)
    service = re.search(r"^\s*manifests_service:\s*(\S+)\s*$", text, re.MULTILINE)
    return service.group(1) if service else ""


def _primary_rollout_kind(role: Path) -> str:
    """The kubectl kind `roles/k8s/manifests` will use for this role's primary rollout.

    Mirrors `manifests_rollout_kind | default('deploy')`. Same regex-is-safe reasoning as
    `_primary_rollout_name`: every caller sets this to a literal, and the shared role asserts
    the value is one of 'deploy'/'daemonset' before anything reads it.
    """
    tasks = role / "tasks/main.yml"
    if not tasks.is_file():
        return "deploy"
    match = re.search(
        r"""^\s*manifests_rollout_kind:\s*(?:"([^"]*)"|'([^']*)'|(\S+))\s*$""",
        tasks.read_text(),
        re.MULTILINE,
    )
    if not match:
        return "deploy"
    return next(g for g in match.groups() if g is not None)


_DEPLOYMENT_NAME = re.compile(
    r"^kind:\s*(?:Deployment|DaemonSet)\s*$\n\s*metadata:\s*$\n\s*name:\s*(.+?)\s*$",
    re.MULTILINE,
)


def _deployment_name(template: Path) -> str | None:
    """The rendered Deployment's `metadata.name`, or None if it isn't a static literal.

    Every Deployment template puts `name:` two lines under `kind: Deployment` (`metadata:` in
    between) — checked against all 56 Deployment templates in the repo. A non-literal value (a
    Jinja expression, e.g. pihole's `{{ inst.name }}`) can't be resolved without rendering, so
    this returns None rather than a guess; the caller treats None as ungated, the fail-closed
    direction.
    """
    match = _DEPLOYMENT_NAME.search(template.read_text())
    if not match:
        return None
    name = match.group(1)
    return name if _LITERAL_NAME.match(name) else None


def _gated_names(role: Path) -> set[str]:
    """Deployment names this role's rollout gate actually waits on."""
    return {_primary_rollout_name(role)} | _extra_rollouts(role)


def _ungated_deployments(role: Path) -> list[str]:
    """Rendered Deployment templates whose resolved name is not in the gated set.

    Matches by identity, not count: a rendered Deployment is gated only if its own resolved
    name equals the primary rollout name or appears in manifests_extra_rollouts. A typo'd or
    drifted extra name, or a Deployment whose name can't be resolved statically, both count as
    ungated — count alone let a mismatched name through as long as the totals lined up.
    """
    gated = _gated_names(role)
    return [
        template
        for template in _deployment_templates(role)
        if _deployment_name(role / "templates" / template) not in gated
    ]


def _ungated_deployment_count(role: Path) -> int:
    return len(_ungated_deployments(role))


_MANIFEST_KIND_TO_ROLLOUT_KIND = {"Deployment": "deploy", "DaemonSet": "daemonset"}


def _deployments_missing_readiness_probe(role: Path) -> list[str]:
    """Rendered Deployment template names with no readinessProbe.

    Was `any()` across the role's templates, so a probe on the primary Deployment satisfied
    the whole role and a probe-less *extra* passed unchecked — exactly the gap
    `manifests_extra_rollouts` opened. Checks every rendered Deployment individually instead.
    """
    return [
        name
        for name in _deployment_templates(role)
        if "readinessProbe" not in (role / "templates" / name).read_text()
    ]
