"""Every long-running pod template names one of the four homelab PriorityClasses.

A pod with no priorityClassName sits at 0, so it is evicted before the things it outranks on
paper. The pod schedules and the deploy goes green, and the consequence shows only under
memory pressure. A grep for the field name cannot tell a template that omits it from one that
sets it inside a macro, so this guard reads the parsed pod spec.

Every Deployment and DaemonSet takes the field from `pod_shell` in
`ansible/templates/workload-shell.yml.j2`, and `test_workload_shell_uses_the_macros.py`
refuses a hand-written copy. This census is the other half: a macro cannot force its own
call, and a template that skips it renders without the field, which only the parsed pod spec
can see. Jobs and CronJobs are out of scope on purpose: each is a probe or a GC pass that
completes in seconds, and a priority buys nothing for a pod that is gone before pressure can
build.

The two other pod-template fields every template sets, `automountServiceAccountToken` and
`enableServiceLinks`, are the `sa-less-pods-refuse-the-default-token` and
`pods-disable-service-link-env-vars` rows of `_workload_property_rows.py`. This one keeps its
own file for two reasons. It reads the four tier names from a second render, the setup
plane's `priorityclass.yaml.j2`. Its exemption is also value-specific: dri-device-plugin may
name `system-node-critical` and nothing else, where a table row's `allow` would exempt any
offence on that key.

Rendering goes through `_k8s_render.rendered_docs()`, the same corpus
`test_container_security_context.py` and `test_readiness_coverage.py` census. That corpus
inherits `validate.k8s_manifests.SKIP_ROLES`, and `_UNCOVERED_ROLES` in
`test_container_security_context.py` pins which roles that leaves out. The one pod spec among
them, `image-builder/templates/build-job.yaml.j2`, is a Job and out of scope here.
"""

import pytest
from lib import yaml_fast

from _k8s_render import pod_spec, rendered_docs
from _setup_render import rendered_setup_text

_LONG_RUNNING = {"Deployment", "DaemonSet", "StatefulSet"}

# The setup-plane template that defines the four tiers, read as it renders.
_PRIORITYCLASS_TEMPLATE = ("k3s", "priorityclass.yaml.j2")

# Long-running pod templates allowed to name a class OUTSIDE the four homelab tiers, with the
# reason. A new entry here is the decision the guard exists to force.
_SYSTEM_TIER = {
    # Losing the device plugin makes jellyfin and tdarr unschedulable rather than degraded, so
    # it must outrank every workload that consumes `devic.es/dri` — the template says so at
    # the line. `system-node-critical` is the class k8s reserves for exactly that shape.
    ("dri-device-plugin", "daemonset.yaml.j2"): "system-node-critical",
}

# Non-vacuity. The census renders 70 long-running pod templates. A floor far below the live
# count cannot tell "the collector broke" from "half the fleet dropped out of the render", so
# this one sits close enough to notice a contraction.
_MIN_LONG_RUNNING = 60

# Roles the census must contain, so a missing member is named rather than counted: a guard
# that stopped seeing them would read green for the exact regression it was written against.
_MUST_CONTAIN = frozenset(
    {
        "observability",
        "valheim",
        "game-stats",
        "dri-device-plugin",
        "traefik",
        "authelia",
    }
)


def _homelab_tiers() -> frozenset[str]:
    """The four class names, read from the manifest that defines them rather than restated."""
    names = frozenset(
        d["metadata"]["name"]
        for d in yaml_fast.safe_load_all(rendered_setup_text(*_PRIORITYCLASS_TEMPLATE))
        if d
    )
    assert len(names) == 4, (
        f"expected 4 PriorityClasses in setup/{'/'.join(_PRIORITYCLASS_TEMPLATE)}: {names}"
    )
    return names


def _pod_templates(kinds: set[str]):
    """(role, template, "<kind>/<name>", pod spec) for every rendered doc of the given kinds.

    The object name is part of the label because one template can carry two Deployments
    (pihole's does), and an offender line naming only the file would read as a duplicate.
    """
    for role, tpl, doc in rendered_docs():
        if doc.get("kind") not in kinds:
            continue
        label = f"{doc['kind']}/{doc.get('metadata', {}).get('name', '<unnamed>')}"
        yield role, tpl, label, pod_spec(doc)


# ── the predicate: None for a clean spec, a one-line reason otherwise ────────────────────


def priority_offence(
    pod: dict, tiers: frozenset[str], allowed: str | None = None
) -> str | None:
    name = pod.get("priorityClassName")
    if name in tiers:
        return None
    if allowed is not None and name == allowed:
        return None
    if name is None:
        return "names no priorityClassName, so it sits at 0 — below homelab-best-effort"
    return f"names {name!r}, which is not one of {sorted(tiers)}"


# ── red proof: the specs the predicate accepts and the ones it rejects ────────────────────


def test_priority_offence_is_clean_on_a_homelab_tier():
    assert (
        priority_offence({"priorityClassName": "homelab-critical"}, _homelab_tiers())
        is None
    )


def test_priority_offence_is_clean_on_an_allowlisted_system_class():
    assert (
        priority_offence(
            {"priorityClassName": "system-node-critical"},
            _homelab_tiers(),
            allowed="system-node-critical",
        )
        is None
    )


@pytest.mark.parametrize(
    "pod",
    [
        {},
        {"priorityClassName": "system-node-critical"},
        {"priorityClassName": "homelab-vip"},
    ],
    ids=["absent", "system-class-without-allowlist", "unknown-name"],
)
def test_priority_offence_is_flagged(pod):
    assert priority_offence(pod, _homelab_tiers()) is not None


# ── the census ────────────────────────────────────────────────────────────────────────────


def _assert_not_vacuous(seen_roles: set[str], count: int, floor: int):
    assert count >= floor, (
        f"only inspected {count} pod templates, expected at least {floor} — coverage shrank, "
        "or the collector stopped matching"
    )
    missing = _MUST_CONTAIN - seen_roles
    assert not missing, f"the census no longer contains {sorted(missing)}"


def test_every_long_running_pod_template_names_a_homelab_tier():
    """A pod with no priorityClassName sits at 0 — which is not "below tier 4" but outside the
    model altogether, in the pile with unclassified kube-system plumbing.
    `roles/setup/k3s/templates/priorityclass.yaml.j2` explains why homelab-best-effort exists
    as a real class rather than as the absence of one."""
    tiers = _homelab_tiers()
    offenders, seen_roles, count = [], set(), 0
    for role, tpl, label, pod in _pod_templates(_LONG_RUNNING):
        seen_roles.add(role)
        count += 1
        reason = priority_offence(pod, tiers, allowed=_SYSTEM_TIER.get((role, tpl)))
        if reason:
            offenders.append(f"{role}/{tpl} {label}: {reason}")
    _assert_not_vacuous(seen_roles, count, _MIN_LONG_RUNNING)
    assert not offenders, "\n".join(offenders)


def test_system_tier_allowlist_names_only_templates_that_use_it():
    """The allowlist is the real guard, so a stale entry — a template that moved back to a
    homelab tier, or was deleted — must not linger as a silent permission.

    A template naming NO class is the census test's finding, not this one's, so it is
    filtered out here rather than reported twice. Two objects in one template that name
    different system classes would collapse under a `(role, tpl)` key, so the key carries
    the object label and is reduced to the allowlist's shape only after the check that
    every value agrees."""
    tiers = _homelab_tiers()
    actual: dict[tuple[str, str], set[str]] = {}
    for role, tpl, _label, pod in _pod_templates(_LONG_RUNNING):
        name = pod.get("priorityClassName")
        if name is not None and name not in tiers:
            actual.setdefault((role, tpl), set()).add(name)
    split = {k: v for k, v in actual.items() if len(v) > 1}
    assert not split, f"one template names several system classes: {split}"
    flat = {k: next(iter(v)) for k, v in actual.items()}
    assert flat == _SYSTEM_TIER, (
        f"long-running pod templates outside the homelab tiers: {flat}; "
        f"allowlisted: {_SYSTEM_TIER}"
    )
