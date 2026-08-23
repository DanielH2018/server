"""Every workload container drops its capabilities — asserted at the CONTAINER, not the pod.

A pod-level `securityContext` and a container-level one are different objects governing
different things: `runAsUser`/`fsGroup` are pod-level, while `capabilities`,
`allowPrivilegeEscalation` and `readOnlyRootFilesystem` only exist per container. A container
with no `securityContext` keeps the runtime's default capability set (CHOWN, DAC_OVERRIDE,
SETUID, SETGID, NET_RAW, ...) however locked-down the pod block looks.

That distinction is why this went unnoticed. A grep for `securityContext` across the fleet
matched the pod block and reported the templates clean, and a 2026-08-15 review recorded
securityContext as verified across all 48 deployment templates on that basis. Five templates
had no container block at all — loki-homelab plus four of claude-otel's — and one role
(claude-otel) was internally inconsistent, since its kube-state-metrics manifest did carry one.

Nothing else enforces this: the cluster has no PodSecurity admission labels on any namespace,
so an absent container securityContext is simply honoured.

Rendering goes through validate_k8s_manifests' own machinery rather than a second stub set, so
this cannot drift from what that validator considers a renderable manifest.
"""

from __future__ import annotations

from _k8s_render import rendered_docs

_POD_KINDS = {"Deployment", "DaemonSet", "StatefulSet", "Job", "CronJob"}

# Containers that run `privileged: true`, each for a hardware reason k8s cannot express any
# other way. Asking a privileged container to drop capabilities is meaningless — privileged
# restores them — so these skip the two checks below and are allowlisted instead. That makes
# the allowlist the real guard: a NEW privileged container fails until it is added here with a
# justification, which is the decision worth forcing.
_PRIVILEGED = {
    # Raw USB access to the UPS; k8s has no equivalent of compose's `devices:`. Contained by a
    # single-node pin and a loopback-only hostPort. See roles/k8s/nut/CLAUDE.md.
    ("nut", "nut"),
    # Registers a gRPC socket in the kubelet's device-plugin dir and hands it device file
    # descriptors. It is privileged so the nine media workloads consuming `devic.es/dri` do
    # not have to be.
    ("dri-device-plugin", "generic-device-plugin"),
    # SMART reads: the Docker cap set plus a CharDevice hostPath is not enough under k8s, as
    # the device cgroup still refuses the open. Resolved as a deliberate trade 2026-08-10.
    ("scrutiny", "collector"),
}


# Roles this guard's corpus does NOT contain, each with the reason it is out.
#
# THE BLIND SPOT THIS PINS (2026-08-23): rendered_docs() filters on
# validate_k8s_manifests.SKIP_ROLES — a list maintained for a DIFFERENT purpose, namely what the
# manifest validator can render standalone. Coverage of this security guard was therefore a side
# effect of someone else's list. seed-volume is on it, and seed-pod.yaml.j2 runs `runAsUser: 0`
# with no container securityContext at all, so every assertion in this file passed while that pod
# was never examined. Pinning the exempt set turns a role joining it into a failure here instead
# of a silent contraction; anything added below needs a justification and, if it renders a pod
# spec, its own test.
_UNCOVERED_ROLES = {
    # Renders no manifests of its own — it is the shared apply/rollout machinery.
    "manifests",
    # Per-deploy state, not a service manifest set. seed-pod.yaml.j2 IS a pod spec and is
    # deliberately not covered here; test_seed_pod_security_context.py owns it.
    "seed-volume",
    # Builds images in-cluster; its Job carries reasoned Unconfined seccomp/AppArmor for
    # rootless BuildKit (build-job.yaml.j2).
    "image-builder",
    # No manifest templates — each resolves a fact or drives kubectl/the Longhorn API directly.
    "rollout-drain",
    "cronjob-gate",
    "volume-snapshot",
    "longhorn-api",
    "volume-revert",
    # Dockerfiles and app config only — no Kubernetes objects at all.
    "n8n-images",
}

# The real container count is ~103. A floor of 40 cannot distinguish a broken collector from
# half the fleet dropping out, which is the failure this file exists to prevent. Kept a little
# below the live count so adding a workload never breaks the build, but close enough to notice
# a contraction.
_MIN_CONTAINERS = 90


def _pod_specs(doc: dict):
    spec = doc.get("spec", {})
    if doc["kind"] == "CronJob":
        spec = spec.get("jobTemplate", {}).get("spec", {})
    return spec.get("template", {}).get("spec", {})


def _containers():
    """(role, template, container name, securityContext) for every container in the fleet."""
    for role, tpl, doc in rendered_docs():
        if doc.get("kind") not in _POD_KINDS:
            continue
        pod = _pod_specs(doc)
        for key in ("initContainers", "containers"):
            for container in pod.get(key) or []:
                yield (
                    role,
                    tpl,
                    container.get("name", "<unnamed>"),
                    container.get("securityContext") or {},
                )


def test_privileged_containers_are_allowlisted():
    privileged = {
        (role, name)
        for role, _tpl, name, sc in _containers()
        if sc.get("privileged") is True
    }
    assert privileged == _PRIVILEGED, (
        "the set of privileged containers changed — added: "
        f"{sorted(privileged - _PRIVILEGED)}, removed: {sorted(_PRIVILEGED - privileged)}. "
        "A privileged container holds every capability regardless of what it drops, so each "
        "one needs a justification recorded in _PRIVILEGED."
    )


def test_every_container_drops_all_capabilities():
    offenders = []
    seen = 0

    for role, tpl, name, sc in _containers():
        if (role, name) in _PRIVILEGED:
            continue
        seen += 1
        dropped = (sc.get("capabilities") or {}).get("drop") or []
        if "ALL" not in [str(d).upper() for d in dropped]:
            offenders.append(f"{role}/{tpl}:{name}")

    assert seen > 40, (
        f"only inspected {seen} containers — the collector stopped matching"
    )
    assert seen >= _MIN_CONTAINERS, (
        f"only inspected {seen} containers, expected at least {_MIN_CONTAINERS} — coverage "
        "shrank. A floor far below the real count cannot tell 'the collector broke' from "
        "'half the fleet stopped being rendered'."
    )
    assert not offenders, (
        "these containers keep the runtime's default capability set, because a pod-level "
        "securityContext does not grant one: " + ", ".join(sorted(offenders))
    )


def test_the_corpus_covers_every_role_except_a_named_set():
    """Coverage is asserted, not assumed — see _UNCOVERED_ROLES."""
    from pathlib import Path

    repo = Path(__file__).resolve().parents[2]
    all_roles = {
        p.parent.parent.name
        for p in (repo / "ansible/roles/k8s").glob("*/tasks/main.yml")
    }
    covered = {role for role, _, _ in rendered_docs()}
    unexpected = (all_roles - covered) - _UNCOVERED_ROLES
    assert not unexpected, (
        "these roles are not in the security corpus and are not declared uncovered, so their "
        "containers are unchecked: " + ", ".join(sorted(unexpected))
    )
    stale = _UNCOVERED_ROLES - all_roles
    assert not stale, "_UNCOVERED_ROLES names roles that no longer exist: " + ", ".join(
        sorted(stale)
    )


def test_no_container_allows_privilege_escalation():
    offenders = [
        f"{role}/{tpl}:{name}"
        for role, tpl, name, sc in _containers()
        if (role, name) not in _PRIVILEGED
        and sc.get("allowPrivilegeEscalation") is not False
    ]
    assert not offenders, (
        "these containers can gain privileges through a setuid binary: "
        + ", ".join(sorted(offenders))
    )
