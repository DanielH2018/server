"""Three pod-spec fields every template must set, censused over the RENDERED fleet.

Each one fails silently when omitted. The pod schedules, the deploy goes green, and the
consequence is only visible later — as a workload evicted before the things it outranks on
paper, a token mounted into a pod that never uses it, or an app reading a Service's env vars
as its own configuration. A grep for the field name cannot tell a template that omits it from
one that sets it inside a macro, and it counts the `automountServiceAccountToken: true` on a
ServiceAccount OBJECT as if it were the pod's — so these guards read the parsed pod spec.

    priorityClassName        every long-running pod template names one of the four homelab
                             tiers (#1851). Jobs and CronJobs are out of scope on purpose:
                             each is a probe or a GC pass that completes in seconds, and a
                             priority buys nothing for a pod that is gone before pressure
                             can build.
    automountServiceAccountToken
                             a pod that names no serviceAccountName runs as the namespace's
                             `default` SA and never uses its token, so it must not mount one
                             (#1852). A pod that names an SA is left alone — the SA object
                             may set the field, and the pod's own value then means nothing.
    enableServiceLinks       every pod template, of every kind, sets it false (#1858). The
                             guard used to read `deployment.yaml.j2` only, so 16 Deployments
                             in other filenames and every DaemonSet, Job and CronJob were
                             unchecked.

Rendering goes through `_k8s_render.rendered_docs()` — the same corpus
`test_container_security_context.py` and `test_readiness_coverage.py` census — so coverage
here cannot drift from theirs. That corpus inherits `validate.k8s_manifests.SKIP_ROLES`, and
`_UNCOVERED_ROLES` in `test_container_security_context.py` pins which roles that leaves out.
The one pod spec among them, `image-builder/templates/build-job.yaml.j2`, sets both boolean
fields and is a Job, so nothing here would say anything about it that
`test_image_builder_security_context.py` does not.

Every predicate is a pure function over one pod spec, tested on an inline dict it must accept
and one it must reject, so each guard carries its own proof that it can go red.
"""

import pytest
from lib import yaml_fast

from _helpers import ROLES
from _k8s_render import rendered_docs

_LONG_RUNNING = {"Deployment", "DaemonSet", "StatefulSet"}
_POD_KINDS = _LONG_RUNNING | {"Job", "CronJob"}

_PRIORITYCLASS_TEMPLATE = (
    ROLES / "setup" / "k3s" / "templates" / "priorityclass.yaml.j2"
)

# Long-running pod templates allowed to name a class OUTSIDE the four homelab tiers, with the
# reason. A new entry here is the decision the guard exists to force.
_SYSTEM_TIER = {
    # Losing the device plugin makes jellyfin and tdarr unschedulable rather than degraded, so
    # it must outrank every workload that consumes `devic.es/dri` — the template says so at
    # the line. `system-node-critical` is the class k8s reserves for exactly that shape.
    ("dri-device-plugin", "daemonset.yaml.j2"): "system-node-critical",
}

# Non-vacuity. The census renders 70 long-running pod templates and 86 with Jobs and CronJobs
# (2026-09-17). A floor far below the live count cannot tell "the collector broke" from "half
# the fleet dropped out of the render", so these sit close enough to notice a contraction.
_MIN_LONG_RUNNING = 60
_MIN_ALL_KINDS = 78

# Roles the census must contain, so a missing member is named rather than counted. The three
# the issues named as defective are here on purpose: a guard that stopped seeing them would
# read green for the exact regression it was written against.
_MUST_CONTAIN = frozenset(
    {
        "claude-otel",
        "valheim",
        "valheim-stats",
        "dri-device-plugin",
        "traefik",
        "authelia",
    }
)


def _homelab_tiers() -> frozenset[str]:
    """The four class names, read from the manifest that defines them rather than restated."""
    names = frozenset(
        d["metadata"]["name"]
        for d in yaml_fast.safe_load_all(_PRIORITYCLASS_TEMPLATE.read_text())
        if d
    )
    assert len(names) == 4, (
        f"expected 4 PriorityClasses in {_PRIORITYCLASS_TEMPLATE}: {names}"
    )
    return names


def _pod_spec(doc: dict) -> dict:
    spec = doc.get("spec", {})
    if doc["kind"] == "CronJob":
        spec = spec.get("jobTemplate", {}).get("spec", {})
    return spec.get("template", {}).get("spec", {})


def _pod_templates(kinds: set[str]):
    """(role, template, "<kind>/<name>", pod spec) for every rendered doc of the given kinds.

    The object name is part of the label because one template can carry two Deployments
    (pihole's does), and an offender line naming only the file would read as a duplicate.
    """
    for role, tpl, doc in rendered_docs():
        if doc.get("kind") not in kinds:
            continue
        label = f"{doc['kind']}/{doc.get('metadata', {}).get('name', '<unnamed>')}"
        yield role, tpl, label, _pod_spec(doc)


# ── the predicates ────────────────────────────────────────────────────────────────────────
# Each returns None for a clean spec and a one-line reason otherwise.


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


def automount_offence(pod: dict) -> str | None:
    if pod.get("serviceAccountName"):
        return None
    if pod.get("automountServiceAccountToken") is False:
        return None
    return (
        "names no serviceAccountName and does not set automountServiceAccountToken: false, "
        "so the default SA's token is mounted into a pod that never uses it"
    )


def service_links_offence(pod: dict) -> str | None:
    if pod.get("enableServiceLinks") is False:
        return None
    return "inherits Docker-link env vars for every Service in the namespace"


# ── red proofs: one spec each predicate accepts, one it rejects ────────────────────────────


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


@pytest.mark.parametrize(
    "pod",
    [
        {"automountServiceAccountToken": False},
        {"serviceAccountName": "prometheus"},
        {"serviceAccountName": "headlamp", "automountServiceAccountToken": True},
    ],
    ids=["no-sa-and-false", "sa-and-unset", "sa-and-true"],
)
def test_automount_offence_is_clean(pod):
    assert automount_offence(pod) is None


@pytest.mark.parametrize(
    "pod",
    [
        {},
        {"automountServiceAccountToken": True},
        {"automountServiceAccountToken": "false"},
    ],
    ids=["absent", "true", "string-false"],
)
def test_automount_offence_is_flagged(pod):
    assert automount_offence(pod) is not None


def test_service_links_offence_is_clean():
    assert service_links_offence({"enableServiceLinks": False}) is None


@pytest.mark.parametrize(
    "pod",
    [{}, {"enableServiceLinks": True}, {"enableServiceLinks": "false"}],
    ids=["absent", "true", "string-false"],
)
def test_service_links_offence_is_flagged(pod):
    assert service_links_offence(pod) is not None


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
    as a real class rather than as the absence of one. The whole claude-otel plane read that
    way until 2026-09-17 (#1851)."""
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


def test_every_sa_less_pod_template_refuses_the_default_token():
    """A pod that names no ServiceAccount runs as `default`, whose token grants nothing this
    cluster's RBAC hands out — and a token that grants nothing is still a bearer credential
    sitting on a tmpfs in every container. valheim and valheim-stats mounted one until
    2026-09-17 (#1852) while their terraria siblings did not."""
    offenders, seen_roles, count = [], set(), 0
    for role, tpl, label, pod in _pod_templates(_POD_KINDS):
        seen_roles.add(role)
        count += 1
        reason = automount_offence(pod)
        if reason:
            offenders.append(f"{role}/{tpl} {label}: {reason}")
    _assert_not_vacuous(seen_roles, count, _MIN_ALL_KINDS)
    assert not offenders, "\n".join(offenders)


def test_every_pod_template_disables_service_link_env_vars():
    """Kubernetes' legacy Docker-link env vars are read as config by some apps.

    It injects <NAME>_SERVICE_HOST, <NAME>_PORT_<n>_TCP and so on for every Service in the
    namespace. Any app that reads its own config from <NAME>_* env vars then picks them up as
    configuration. Authelia did, and exited before serving anything (daniel-box, 2026-08-02):

        error occurred performing deprecation mapping for keys 'server.host', 'server.port',
        and 'server.path' to new key server.address: the new key already exists with value
        'tcp4://:9091' but the deprecated keys and the new key can't both be configured

    Triggering it needs only that a Service name match an app's env-var prefix, which is the
    normal case in this namespace — so the guard covers every pod template of every kind,
    not just `deployment.yaml.j2`. It lived in `test_k8s_manifests.py` reading that one
    filename until 2026-09-17 (#1858), when three templates in other filenames turned out to
    omit the field.
    """
    offenders, seen_roles, count = [], set(), 0
    for role, tpl, label, pod in _pod_templates(_POD_KINDS):
        seen_roles.add(role)
        count += 1
        reason = service_links_offence(pod)
        if reason:
            offenders.append(f"{role}/{tpl} {label}: {reason}")
    _assert_not_vacuous(seen_roles, count, _MIN_ALL_KINDS)
    assert not offenders, "\n".join(offenders)
