"""The seed pod's security shape, which the fleet-wide guard structurally cannot see.

`seed-volume` is in validate_k8s_manifests.SKIP_ROLES, so `_k8s_render.rendered_docs()` filters
it out before any kind matching — and `test_container_security_context.py` consumes exactly that
corpus. seed-pod.yaml.j2 is a real Pod spec running `runAsUser: 0`, and every assertion in that
file passed for as long as it existed while this pod was never examined (found 2026-08-23).
That role cannot simply be removed from SKIP_ROLES: it mutates, is on `k8s_dry_run_unsupported`,
and renders per-deploy state rather than a service's manifest set. So it gets its own guard here.

WHAT THIS DELIBERATELY DOES NOT ASSERT: that the container drops capabilities. It currently has
no container `securityContext` at all, so it keeps the runtime default set. That is a real gap,
and the fix is NOT a one-line `drop: ALL` — the container runs `tar -x -p --numeric-owner` via
`kubectl exec`, and under busybox tar the sufficient set is unsettled (setuid/setgid bits need
FSETID, device nodes need MKNOD, file caps need SETFCAP). Worse, nothing in this repo can
validate such a change before it ships: SKIP_ROLES excludes the role from prek's render,
`k8s_dry_run_unsupported` excludes it from `--dry-run`, and seed.yml's already-seeded
short-circuit means a steady-state deploy creates no seed pod at all. A wrong cap set would
therefore go green through every gate and detonate on the next genuinely-new seeded service, as
a half-extracted volume with wrong ownership — across a dependency of ~25 roles. Establishing
the correct set needs a real forced re-seed, which is an operator action, not a test.

What IS asserted here is the boundary that must not move meanwhile: this pod stays unprivileged
and stays off the host's namespaces and filesystem. Those are the properties that would turn the
default cap set from "no incremental privilege over the Ansible caller that created it" into a
genuine escalation path.
"""

from __future__ import annotations

import sys

import yaml
from _helpers import REPO

_REPO = REPO
sys.path.insert(0, str(_REPO / "scripts"))

from validate_k8s_manifests import (  # noqa: E402 — needs the path insert above
    ALL_VARS,
    ANSIBLE,
    BASE_CONTEXT,
    K8S_ROLES,
    SHARED_TPL,
    load_yaml,
    make_env,
    make_lookup,
    register_ansible_filters,
    render_or_error,
    resolve_vars,
    role_defaults,
)

_ROLE = "seed-volume"
_TEMPLATE = "seed-pod.yaml.j2"


def _seed_pod() -> dict:
    """The rendered seed Pod, using the validator's own machinery rather than a second stub set."""
    base = {**BASE_CONTEXT, **load_yaml(ALL_VARS), "playbook_dir": str(ANSIBLE)}
    base = resolve_vars(base, base)
    role_dir = K8S_ROLES / _ROLE
    ctx = {
        **base,
        **role_defaults(_ROLE, base),
        # Supplied by the play at run time, not by defaults.
        "seed_volume_claim": "example-config",
        "seed_volume_mount": "/data",
        "seed_volume_node": "daniel-box",
    }
    env = make_env([role_dir / "templates", SHARED_TPL])
    env.globals["lookup"] = make_lookup(ctx)
    register_ansible_filters(env)
    text, err = render_or_error(env, _TEMPLATE, ctx)
    assert err is None, f"{_TEMPLATE} failed to render: {err}"
    doc = yaml.safe_load(text)
    assert doc and doc.get("kind") == "Pod", (
        f"{_TEMPLATE} no longer renders a Pod — this guard is measuring nothing"
    )
    return doc


def _container(doc: dict) -> dict:
    containers = doc["spec"]["containers"]
    assert len(containers) == 1, (
        "seed pod grew a second container; review its security shape"
    )
    return containers[0]


def test_seed_pod_is_not_privileged():
    sc = _container(_seed_pod()).get("securityContext") or {}
    assert sc.get("privileged") is not True, (
        "the seed pod must never be privileged — it mounts arbitrary Longhorn PVCs"
    )


def test_seed_pod_does_not_join_host_namespaces():
    spec = _seed_pod()["spec"]
    for key in ("hostNetwork", "hostPID", "hostIPC"):
        assert spec.get(key) is not True, f"seed pod must not set {key}"


def test_seed_pod_mounts_no_host_path():
    """Its only volume is the PVC being seeded. A hostPath here would put the default capability
    set (DAC_OVERRIDE, SETUID) onto the node's own filesystem, which is the difference between
    'no more privilege than the caller' and a real escalation."""
    doc = _seed_pod()
    offenders = [v["name"] for v in doc["spec"].get("volumes", []) if "hostPath" in v]
    assert not offenders, f"seed pod mounts hostPath volume(s): {', '.join(offenders)}"


def test_seed_pod_runs_as_root_deliberately_and_says_why():
    """runAsUser: 0 is load-bearing — `tar -p --numeric-owner` restores the source's UIDs. Pinned
    so that removing it is a deliberate act, since doing so silently corrupts seeded ownership
    rather than failing."""
    doc = _seed_pod()
    assert doc["spec"].get("securityContext", {}).get("runAsUser") == 0
    raw = (K8S_ROLES / _ROLE / "templates" / _TEMPLATE).read_text()
    assert "numeric-owner" in raw, (
        "the reason runAsUser: 0 is required must stay written at the line that requires it"
    )


def test_seed_pod_does_not_add_capabilities():
    """The absence of `capabilities.add` — which is NOT the barred cap-set question above.

    This file's docstring rules out proposing `drop: ALL`, because the sufficient set under
    busybox `tar -p --numeric-owner` is unsettled and no gate here can validate a guess. Asserting
    that nothing is *added* needs neither answer: it does not change the running cap set, and a
    forced re-seed cannot disprove it.

    The gap it closes is real. None of the four tests above reference `capabilities` or
    `allowPrivilegeEscalation`, and `test_container_security_context.py:57-62` hands ownership of
    this pod to this file — so an edit adding `capabilities: {add: [SYS_ADMIN]}` passed every
    guard in the repo. That is the whole distance between "keeps the runtime default set" and
    "privileged in all but name", on a pod that mounts arbitrary Longhorn PVCs (2026-08-23b
    review M16).
    """
    sc = _container(_seed_pod()).get("securityContext") or {}
    added = (sc.get("capabilities") or {}).get("add") or []
    assert not added, (
        f"seed pod adds capabilities {added}. The default set is deliberate and unexamined; "
        f"adding to it is a privilege change no other guard in this repo would catch."
    )
    assert sc.get("allowPrivilegeEscalation") is not True, (
        "seed pod must not set allowPrivilegeEscalation: true — with runAsUser: 0 and the "
        "default cap set, that is privileged in all but name."
    )


def test_seed_pod_does_not_mount_a_service_account_token():
    """It runs `sleep` and is written into by `kubectl exec` from the Ansible controller
    (seed.yml:224), making no API calls of its own. The default SA token grants only discovery
    today — nothing in this repo binds a Role to the `default` SA — but it is still a credential
    mounted into a root container that mounts arbitrary Longhorn PVCs (2026-08-23b review L1)."""
    assert _seed_pod()["spec"].get("automountServiceAccountToken") is False, (
        "seed pod must set automountServiceAccountToken: false — it makes no API calls."
    )
