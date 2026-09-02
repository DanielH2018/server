"""`probe.py readonly-rbac` — is the read-only ServiceAccount still read-only?

Split out of probe_health.py, which carried three unrelated subcommands (health, readonly-rbac,
vip-placement) in one file. Ansible is meant to be the only write path to this cluster, and
every agent session's kubectl authenticates as
system:serviceaccount:kube-system:homelab-readonly — this is the check that notices if that
ever widens.

A probe that only checks DENIALS passes when kubectl is broken outright — no kubeconfig, no
cluster, wrong context — because every call fails then too. `READONLY_ALLOWED` is the positive
control: prove the tool works before reading anything into what it refuses, the same
control-first pattern the netpol probe Jobs use.
"""

import subprocess

# `core.<name>` for anything the tests monkeypatch — binding those into this module's
# globals with a `from probe_core import ...` would take a snapshot the patch never reaches.
import probe_core as core

# What plain `kubectl` must NEVER be able to do here. Ansible is the only write path to this
# cluster, and every agent session's kubectl authenticates as
# system:serviceaccount:kube-system:homelab-readonly. If that widens, the guarantee every
# session relies on is gone and nothing says so.
READONLY_DENIED = (
    ("get", "secrets"),
    ("list", "secrets"),
    ("create", "pods"),
    ("delete", "pods"),
)

# The positive control. A probe that only checks DENIALS passes when kubectl is broken
# outright — no kubeconfig, no cluster, wrong context — because every call fails then too.
# This is the control-first pattern the netpol probe Jobs use: prove the tool works before
# reading anything into what it refuses.
READONLY_ALLOWED = (("get", "pods"), ("list", "deployments"))


def can_i_argv(verb, resource, namespace):
    """`kubectl auth can-i`, which answers from RBAC without attempting the operation.

    Asking RBAC rather than trying the write is deliberate: a `create pods` probe that
    actually created a pod would be a write to a cluster whose whole point is that Ansible is
    the only write path.
    """
    return [
        "kubectl",
        "auth",
        "can-i",
        verb,
        resource,
        "-n",
        namespace,
        "--quiet",
    ]


def format_readonly_rbac(denied_results, allowed_results):
    """Render the RBAC verdict.

    `denied_results` / `allowed_results` map (verb, resource) -> bool "the SA is permitted".
    Fails when a denied verb became permitted (privilege creep) OR when a control verb is
    refused (the probe cannot conclude anything).
    """
    lines = []
    creep = sorted(f"{v} {r}" for (v, r), ok in denied_results.items() if ok)
    lost = sorted(f"{v} {r}" for (v, r), ok in allowed_results.items() if not ok)

    for (verb, resource), ok in sorted(allowed_results.items()):
        lines.append(
            f"  control  {verb:<7} {resource:<12} {'allowed' if ok else 'REFUSED'}"
        )
    for (verb, resource), ok in sorted(denied_results.items()):
        lines.append(
            f"  denied   {verb:<7} {resource:<12} {'PERMITTED' if ok else 'forbidden'}"
        )

    lines.append("")
    if lost:
        lines.append(
            "INCONCLUSIVE: the control verbs were refused, so the denials below prove "
            "nothing — a broken kubeconfig refuses everything. Fix access first: "
            + ", ".join(lost)
        )
        return "\n".join(lines), 2
    if creep:
        lines.append(
            "FAIL: the read-only ServiceAccount is now PERMITTED verbs it must never hold. "
            "Ansible is meant to be the only write path to this cluster: "
            + ", ".join(creep)
        )
        return "\n".join(lines), 1
    lines.append("OK: read-only ServiceAccount holds none of the denied verbs.")
    return "\n".join(lines), 0


def run_readonly_rbac(ns):
    """Assert plain kubectl is still read-only (read-only itself: `auth can-i`, no writes)."""
    namespace = getattr(ns, "namespace", None) or core.k8s_namespace()

    def permitted(verb, resource):
        proc = subprocess.run(
            can_i_argv(verb, resource, namespace), capture_output=True, text=True
        )
        return proc.returncode == 0

    if getattr(ns, "dry_run", False):
        for verb, resource in READONLY_ALLOWED + READONLY_DENIED:
            print(" ".join(can_i_argv(verb, resource, namespace)))
        return 0

    allowed = {pair: permitted(*pair) for pair in READONLY_ALLOWED}
    denied = {pair: permitted(*pair) for pair in READONLY_DENIED}
    text, code = format_readonly_rbac(denied, allowed)
    print(text)
    return code
