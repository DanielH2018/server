#!/usr/bin/env python3
"""Live-object drift check — managed by Ansible (k3s role); edits overwritten.

Answers a question nothing else in this repo asks: has a live object been changed since
Ansible last applied it?

The neighbouring `manifest-prune-check.sh` compares live object EXISTENCE against the staged
manifests — it catches an object whose template entry was deleted. It says nothing about an
object that still exists and whose SPEC no longer matches. A hand-run `kubectl scale`,
`kubectl patch` or `kubectl edit` survives silently until that role is next deployed, and a
`kubectl apply` is the only thing that would notice.

HOW IT READS WITHOUT WRITE ACCESS. The obvious tool is `kubectl diff`, which cannot be used
here: it works by sending the manifest to the API server as a dry-run apply, so it needs
`create` and `patch`. This host's kubectl authenticates as the read-only ServiceAccount
(`get list watch`), and `kubectl auth can-i create deployments` answers no. Widening that
credential for one cron was the alternative and is not worth it.

Instead each object is compared against its OWN `kubectl.kubernetes.io/last-applied-
configuration` annotation, which `kubectl apply` writes and which holds the manifest exactly
as Ansible last applied it. That is a plain `get` — no dry-run, no write verb, nothing the
read-only SA cannot do. 168 of the 188 objects in scope carry the annotation.

WHAT IT COMPARES. Only the keys the applied manifest declares. Everything else on a live
object is defaults, status and controller bookkeeping, none of which is drift. So a manifest
that never mentions `replicas` will not report a replica change — correctly, because nothing
in the repo asserts a value for it.

WHAT IT CANNOT SEE, and this is the honest limit: a hand-run `kubectl apply` UPDATES the
annotation, so it masks itself. `patch`, `edit`, `scale` and a controller writing to a
declared field do not, and those are the realistic drift sources. It also cannot see a
manifest that changed in git but was never deployed — that is the deploy-staleness guard's
job (scripts/deploy_staleness.py), not this one.

The read-only SA is Forbidden on Secrets — `get list watch` reads as covering them and does
not. The kinds below are what it can actually read, and the run prints the uncovered kind
rather than quietly omitting it.

Exit codes: 0 clean, 1 drift or a new object with no apply baseline, 2 the check itself
failed.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import syslog
import urllib.parse
import urllib.request

# Deliberately absent below: the read-only SA cannot read this kind (see the module
# docstring). Named so the run can say so rather than leaving the gap unstated.
UNCOVERED_KINDS = ("secret",)

KINDS = (
    "deployment",
    "daemonset",
    "statefulset",
    "service",
    "configmap",
    "cronjob",
    "ingress",
)

LAST_APPLIED = "kubectl.kubernetes.io/last-applied-configuration"

# Namespaces this repo never applies into. The trigger is authorship, not ownership: nothing
# under ansible/ writes these objects, so a difference between their live state and their
# baseline is not a signal anyone here can act on.
#   longhorn-system  — Longhorn's own bundle (csi-*, engine-image-*, the backup CronJobs)
#   kube-system      — k3s's bundled components
FOREIGN_NAMESPACES = frozenset(
    {"longhorn-system", "kube-system", "kube-public", "kube-node-lease", "default"}
)

# Objects a controller creates inside a namespace we DO manage. Same trigger as above —
# nothing here applies them — but they need naming individually because the namespace is ours.
CONTROLLER_OBJECTS = frozenset({"kube-root-ca.crt"})

# Objects Ansible maintains by a mechanism OTHER than `kubectl apply`, so their last-applied
# annotation is frozen at whatever first created them and will never match the live content.
#
# Each entry names the mechanism and where it lives, deliberately: if that task is ever
# switched to `apply`, the exemption reads as obviously stale instead of as a permanent truth
# about the object.
PATCH_MAINTAINED = {
    ("configmap", "longhorn-system", "longhorn-storageclass"): (
        "written by `kubectl patch --type=merge` at roles/setup/k3s/tasks/longhorn.yml:197, "
        'so its baseline is still Longhorn\'s upstream bundle (numberOfReplicas: "3")'
    ),
}

# Objects that exist with no apply baseline at all. NOT an exemption list — a floor. `kubectl
# apply` only prunes removed map keys on objects it has a baseline for, so an unannotated
# object silently keeps keys forever after they leave the manifest. That is the class that bit
# the static-monitors Secret (2026-08-10) and monitor-bridge-env twice (2026-08-14). The two
# grafana-dashboards ConfigMaps are built with `kubectl create` — the same reason they sit in
# k8s_dry_run_unsupported — and are counted here, not excused. A count above this floor means
# a new object arrived with no baseline.
UNANNOTATED_FLOOR = 2

_SUFFIXES = {
    "": 1.0,
    "m": 1e-3,
    "k": 1e3,
    "M": 1e6,
    "G": 1e9,
    "T": 1e12,
    "P": 1e15,
    "Ki": 2.0**10,
    "Mi": 2.0**20,
    "Gi": 2.0**30,
    "Ti": 2.0**40,
    "Pi": 2.0**50,
}
_QUANTITY = re.compile(r"^(\d+(?:\.\d+)?)(m|[kMGTP]i?)?$")


def parse_quantity(value) -> float | None:
    """Parse a Kubernetes quantity (``1Gi``, ``500m``, ``0.5``) to a float, else None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        return None
    match = _QUANTITY.match(value.strip())
    if not match:
        return None
    return float(match.group(1)) * _SUFFIXES[match.group(2) or ""]


def values_equal(live, applied) -> bool:
    """Compare one leaf value, allowing the rewrite the API server performs on admission.

    Quantities are canonicalised: a manifest saying ``1024Mi`` comes back as ``1Gi`` and
    ``0.5`` comes back as ``500m``. Comparing those as strings accounted for 24 of this
    fleet's 25 apparent differences, none of them real.
    """
    if live == applied:
        return True
    live_quantity, applied_quantity = parse_quantity(live), parse_quantity(applied)
    return (
        live_quantity is not None
        and applied_quantity is not None
        and live_quantity == applied_quantity
    )


def subset_diff(live, applied, path: str = "") -> list[tuple[str, object, object]]:
    """Differences in ``live``, restricted to what ``applied`` declares.

    Recursing only into the applied manifest's own keys is what makes the result signal
    rather than noise: a live object carries defaults, status and managedFields no manifest
    ever mentioned, and none of that is drift.
    """
    diffs: list[tuple[str, object, object]] = []
    if isinstance(applied, dict):
        if not isinstance(live, dict):
            return [(path or "<root>", live, applied)]
        for key, want in applied.items():
            if key == LAST_APPLIED:
                continue
            child = f"{path}.{key}"
            if key not in live:
                # An explicit null is dropped on admission rather than stored, so
                # absent-live against null-applied is the API server agreeing with the
                # manifest, not a missing field. longhorn-frontend's `nodePort: null` is
                # the live instance of this.
                if want is None:
                    continue
                diffs.append((child, "<absent>", want))
            else:
                diffs += subset_diff(live[key], want, child)
    elif isinstance(applied, list):
        if not isinstance(live, list) or len(live) != len(applied):
            return [(path or "<root>", live, applied)]
        for index, (live_item, want) in enumerate(zip(live, applied)):
            diffs += subset_diff(live_item, want, f"{path}[{index}]")
    elif not values_equal(live, applied):
        diffs.append((path or "<root>", live, applied))
    return diffs


def is_foreign(kind: str, namespace: str, name: str) -> str | None:
    """Return why this object is out of scope, or None if it should be checked.

    Specific reasons are tested BEFORE the namespace sweep, which matters even though both
    skip the object. longhorn-storageclass lives in longhorn-system but IS written from this
    repo, so the namespace reason ("not applied from this repo") would be false for it — and
    a wrong reason on a live exemption is worse than a coarse one, because the entry that
    holds the true reason becomes unreachable and rots unnoticed.
    """
    specific = PATCH_MAINTAINED.get((kind, namespace, name))
    if specific:
        return specific
    if name in CONTROLLER_OBJECTS:
        return f"{name} is created by a controller, not by Ansible"
    if namespace in FOREIGN_NAMESPACES:
        return f"namespace {namespace} is not applied from this repo"
    return None


def fetch(kind: str) -> tuple[list[dict], str | None]:
    """``kubectl get <kind> -A -o json``. Returns (items, error)."""
    proc = subprocess.run(
        ["kubectl", "get", kind, "-A", "-o", "json"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if proc.returncode != 0:
        first = proc.stderr.strip().splitlines()
        return [], first[0] if first else "kubectl failed"
    try:
        return json.loads(proc.stdout).get("items", []), None
    except json.JSONDecodeError as exc:
        return [], f"unparseable kubectl output: {exc}"


def verdict(drifted: list, unannotated: list, errors: list) -> tuple[int, str]:
    """Exit code and the one-line message pushed to Kuma.

    A read failure is exit 2, NOT a clean run: a check that cannot read the cluster must not
    report the cluster as clean. Fail closed, the same shape as the CI gate in gitops_deploy.
    """
    if errors:
        return 2, f"check failed: {'; '.join(errors[:2])}"
    parts = []
    if drifted:
        shown = ", ".join(f"{kind} {ns}/{name}" for kind, ns, name, _ in drifted[:3])
        parts.append(f"{len(drifted)} object(s) drifted from last-applied: {shown}")
    if len(unannotated) > UNANNOTATED_FLOOR:
        extra = ", ".join(unannotated[UNANNOTATED_FLOOR:][:3])
        parts.append(
            f"{len(unannotated)} with no apply baseline (floor {UNANNOTATED_FLOOR}) — apply "
            f"cannot prune removed keys on these: {extra}"
        )
    if parts:
        return 1, "; ".join(parts)
    return 0, (
        f"no drift; {len(unannotated)} object(s) without an apply baseline (at floor); "
        f"{', '.join(UNCOVERED_KINDS)} not covered (read-only SA is Forbidden)"
    )


def push(status: str, message: str) -> None:
    """Push the result to Uptime Kuma, if a token is configured, and record it in syslog.

    The syslog line is not a duplicate of the push. Kuma keeps CURRENT state only, so a DOWN
    that has since recovered leaves no trace there — `probe.py alerts` and the Alert History
    board reconstruct episodes for host-cron pushers by matching `status=down` in syslog, and
    without this line this check was invisible to both (2026-08-22 review M3).

    Emitted BEFORE the push and outside the token guard, so it still lands when the push fails
    or no token is configured — the cases where syslog is the only record there will ever be.
    """
    syslog.openlog(ident="live-drift-check", facility=syslog.LOG_DAEMON)
    try:
        syslog.syslog(
            syslog.LOG_WARNING if status == "down" else syslog.LOG_INFO,
            f"status={status} {message[:900]}",
        )
    finally:
        syslog.closelog()

    token = os.environ.get("PUSH_TOKEN", "")
    host = os.environ.get("KUMA_HOST", "")
    if not token or not host:
        return
    query = urllib.parse.urlencode({"status": status, "msg": message[:900], "ping": ""})
    url = f"https://{host}/api/push/{token}?{query}"
    try:
        urllib.request.urlopen(url, timeout=10).read()  # noqa: S310 — https, fixed host
    except Exception as exc:  # noqa: BLE001 — a failed push must not fail the check
        print(f"kuma push failed: {exc}", file=sys.stderr)


def main() -> int:
    drifted: list[tuple[str, str, str, list]] = []
    unannotated: list[str] = []
    errors: list[str] = []
    checked = 0

    for kind in KINDS:
        items, error = fetch(kind)
        if error:
            errors.append(f"{kind}: {error}")
            continue
        for obj in items:
            meta = obj.get("metadata", {})
            namespace, name = meta.get("namespace", ""), meta.get("name", "")
            if is_foreign(kind, namespace, name):
                continue
            baseline = (meta.get("annotations") or {}).get(LAST_APPLIED)
            if not baseline:
                unannotated.append(f"{kind} {namespace}/{name}")
                continue
            checked += 1
            try:
                applied = json.loads(baseline)
            except json.JSONDecodeError as exc:
                errors.append(f"{kind} {namespace}/{name}: unparseable baseline: {exc}")
                continue
            diffs = subset_diff(obj, applied)
            if diffs:
                drifted.append((kind, namespace, name, diffs))

    code, message = verdict(drifted, sorted(unannotated), errors)

    for kind, namespace, name, diffs in drifted:
        print(f"[DRIFT] {kind} {namespace}/{name}", file=sys.stderr)
        for path, live, applied in diffs[:5]:
            print(
                f"    {path}: live={live!r} last-applied={applied!r}", file=sys.stderr
            )
    for entry in sorted(unannotated):
        print(f"[NO-BASELINE] {entry}", file=sys.stderr)
    for error in errors:
        print(f"[ERROR] {error}", file=sys.stderr)
    print(
        f"{checked} object(s) compared against their last-applied baseline. {message}"
    )

    push("up" if code == 0 else "down", message)
    return code


if __name__ == "__main__":
    sys.exit(main())
