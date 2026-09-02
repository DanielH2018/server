"""The image-builder Job's security shape — the fleet's least-confined container.

`test_container_security_context.py:55-56` sets the contract for anything it exempts: it "needs a
justification and, if it renders a pod spec, its own test." `image-builder` renders `kind: Job`
with a full pod spec and got only the justification. Its exemption reads "reasoned Unconfined
seccomp/AppArmor for rootless BuildKit", and that reason is narrower than the gate it excuses —
joining `_UNCOVERED_ROLES` also silently waives uid, `privileged`, `capabilities.add`, hostPath
and host namespaces, none of which the sentence mentions (2026-08-23b review M17). The role is
also in `validate.k8s_manifests.SKIP_ROLES`, so the manifest validator does not see it either.

The manifest is sound today: uid 1000, not privileged, no added capabilities, no hostPath,
`automountServiceAccountToken: false`. Every one of those is prose with no executable backing,
which is the entire finding. This file gives them one.

WHAT THIS DELIBERATELY DOES NOT ASSERT: that seccomp and AppArmor are Confined, or that
`allowPrivilegeEscalation` is false. Both are load-bearing and build-job.yaml.j2:94-100 records
why at the line — setting `allowPrivilegeEscalation: false` was tried and fails before buildkitd
starts (`newuidmap: Could not set caps`), because rootlesskit maps subuids through a setuid
helper whose job is to raise capabilities. Blocking it does not yield a safer builder, it yields
a root or privileged one. This file pins the boundaries around that decision, not the decision.
"""

from __future__ import annotations

import sys

import yaml
from _helpers import REPO

_REPO = REPO
sys.path.insert(0, str(_REPO / "scripts"))

from validate.k8s_manifests import (  # noqa: E402 — needs the path insert above
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

_ROLE = "image-builder"
_TEMPLATE = "build-job.yaml.j2"


def _build_job() -> dict:
    """The rendered build Job, via the validator's own machinery rather than a second stub set."""
    base = {**BASE_CONTEXT, **load_yaml(ALL_VARS), "playbook_dir": str(ANSIBLE)}
    base = resolve_vars(base, base)
    role_dir = K8S_ROLES / _ROLE
    ctx = {
        **base,
        **role_defaults(_ROLE, base),
        # Supplied by the play at run time, not by defaults.
        "image_builder_name": "example-image",
        "image_builder_context_dir": "/tmp/example-context",
        "image_builder_tag": "abc1234",
        "image_builder_dockerfile": "Dockerfile",
    }
    env = make_env([role_dir / "templates", SHARED_TPL])
    env.globals["lookup"] = make_lookup(ctx)
    register_ansible_filters(env)
    text, err = render_or_error(env, _TEMPLATE, ctx)
    assert err is None, f"{_TEMPLATE} failed to render: {err}"
    doc = yaml.safe_load(text)
    assert doc and doc.get("kind") == "Job", (
        f"{_TEMPLATE} no longer renders a Job — this guard is measuring nothing"
    )
    return doc


def _pod_spec(doc: dict) -> dict:
    return doc["spec"]["template"]["spec"]


def _containers(doc: dict) -> list[dict]:
    spec = _pod_spec(doc)
    return list(spec.get("initContainers") or []) + list(spec["containers"])


def test_build_job_is_not_privileged():
    for container in _containers(_build_job()):
        sc = container.get("securityContext") or {}
        assert sc.get("privileged") is not True, (
            f"image-builder container {container['name']!r} is privileged. The Unconfined "
            f"seccomp/AppArmor pair is reasoned; `privileged: true` is a different question and "
            f"would make rootless BuildKit pointless."
        )


def test_build_job_runs_unprivileged_uid():
    """uid 1000, not root.

    Rootless BuildKit is the entire justification for the Unconfined profiles, and it stops being
    rootless the moment this becomes 0.
    """
    doc = _build_job()
    pod_uid = (_pod_spec(doc).get("securityContext") or {}).get("runAsUser")
    for container in _containers(doc):
        uid = (container.get("securityContext") or {}).get("runAsUser", pod_uid)
        assert uid not in (0, None), (
            f"image-builder container {container['name']!r} runs as {uid!r}. It must pin a "
            f"non-root uid: the Unconfined seccomp/AppArmor profiles are justified by the "
            f"builder being rootless, and root plus Unconfined is a different exposure entirely."
        )


def test_build_job_adds_no_capabilities():
    for container in _containers(_build_job()):
        sc = container.get("securityContext") or {}
        added = (sc.get("capabilities") or {}).get("add") or []
        assert not added, (
            f"image-builder container {container['name']!r} adds capabilities {added}. "
            f"build-job.yaml.j2 states 'adds no capabilities' as the reason this Job does not "
            f"grant root on the node; that claim is what this asserts."
        )


def test_build_job_mounts_no_host_path():
    """Its volumes are the build context and buildkitd's emptyDir state.

    A hostPath would put the builder onto the node's own filesystem, which is the escalation the uid
    pin exists to avoid.
    """
    offenders = [
        v["name"] for v in _pod_spec(_build_job()).get("volumes", []) if "hostPath" in v
    ]
    assert not offenders, (
        f"image-builder mounts hostPath volume(s): {', '.join(offenders)}"
    )


def test_build_job_does_not_join_host_namespaces():
    spec = _pod_spec(_build_job())
    for key in ("hostNetwork", "hostPID", "hostIPC"):
        assert spec.get(key) is not True, f"image-builder must not set {key}"


def test_build_job_does_not_mount_a_service_account_token():
    """It talks to the in-cluster registry over the network and needs no API access.

    A mounted token on a container running Unconfined is a credential inside the least-confined
    thing in the fleet.
    """
    spec = _pod_spec(_build_job())
    assert spec.get("automountServiceAccountToken") is False, (
        "image-builder must set automountServiceAccountToken: false — it needs no Kubernetes "
        "API access, and it is the fleet's least-confined container."
    )


def test_unconfined_profiles_stay_documented_at_the_line():
    """The Unconfined pair is the one thing the exemption in test_container_security_context.py
    actually names. Keep its justification where the trade-off is made, not only in that list."""
    raw = (K8S_ROLES / _ROLE / "templates" / _TEMPLATE).read_text()
    assert "Unconfined" in raw
    assert "newuidmap" in raw, (
        "the empirical reason the Unconfined profiles and allowPrivilegeEscalation cannot be "
        "tightened must stay written at the line that sets them"
    )
