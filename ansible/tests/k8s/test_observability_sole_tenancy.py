"""Guards the sole-tenancy invariant slice 3's intra-namespace peer leans on.

`networkpolicy-observability.yaml.j2` admits a bare `podSelector: {}` as the intra-namespace
ingress peer for the `observability` namespace — every pod there may reach every other pod
there. That is sound only because the namespace is SOLE-TENANT today: `claude-otel` is the only
role that renders a workload into it. A second role landing a pod in this namespace would get
free, unrestricted ingress to every already-fenced workload here, with no policy change of its
own — the fence would look intact while quietly covering less than it claims to.

This is the third time this finding has surfaced in review (see the "Slice 3 specifics" section
of docs/networkpolicy-default-deny.md and the comment above `podSelector: {}` in
networkpolicy-observability.yaml.j2) — per this repo's own escalate-on-recurrence rule, a third
occurrence becomes a check, not another paragraph.

Run: uv run pytest ansible/tests/test_observability_sole_tenancy.py
"""

from __future__ import annotations

from _k8s_render import rendered_docs

# Same pod-producing kinds test_netpol_baseline_labels.py guards against.
POD_KINDS = {"Deployment", "DaemonSet", "StatefulSet", "CronJob", "Job"}

OBSERVABILITY_NAMESPACE = "observability"
SOLE_TENANT_ROLE = "claude-otel"


def test_only_claude_otel_renders_a_workload_into_observability() -> None:
    intruders = sorted(
        f"{role}/{doc.get('kind')}/{doc.get('metadata', {}).get('name', '?')}"
        for role, _tpl, doc in rendered_docs()
        if doc.get("kind") in POD_KINDS
        and doc.get("metadata", {}).get("namespace") == OBSERVABILITY_NAMESPACE
        and role != SOLE_TENANT_ROLE
    )
    assert not intruders, (
        f"a role other than '{SOLE_TENANT_ROLE}' renders a pod into the "
        f"'{OBSERVABILITY_NAMESPACE}' namespace: {intruders}\n"
        "networkpolicy-observability.yaml.j2's intra-namespace ingress peer is a bare "
        "`podSelector: {}` — every pod in this namespace is trusted to reach every other pod in "
        "it. That is sound only because the namespace is sole-tenant. A second role landing a "
        "workload here gets unrestricted ingress to every already-fenced pod in this namespace, "
        "with no policy change of its own. Either give the new workload its own namespace, or "
        "replace the bare selector in networkpolicy-observability.yaml.j2 with an explicit "
        "per-workload peer list before merging it in."
    )
