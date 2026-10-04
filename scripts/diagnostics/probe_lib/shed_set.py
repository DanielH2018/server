"""`probe.py shed-set` — the k8s workloads to scale down when one node is lost.

Each tier in `filter_plugins/service_tier.py` names a service the house, SSO or the platform
needs. Everything else is the shed set: the untiered services an operator scales to zero so
the tiered ones fit on the surviving node (#3481). The set is derived from the `tier` field on
daniel-box's `containers_list`, through `lib.service_tiers.shed_set`, so a service joins or
leaves it by losing or gaining a `tier:` and never by an edit here.

A service is a role, not a workload, so each one is rendered and only its Deployments and
StatefulSets are printed: those are what `kubectl scale` reaches. A role that renders neither
(a DaemonSet, a CronJob, a volume or a policy set) is named on its own line as having nothing
to scale, rather than dropped. The render is `health.py`'s, the same one the deploy gate uses,
and no kubectl call is made: the set is a property of the declared tiers, not of where pods
happen to be scheduled.

It prints names rather than `kubectl scale` commands because plain kubectl is read-only here.
"""

# `probe_lib` is a namespace package under `scripts/`, so reaching a sibling by package name
# needs `scripts/` on sys.path — a module gets only its importer's path otherwise, and
# pyproject's `pythonpath` is a pytest setting. This has to sit ABOVE the imports below.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

from diagnostics.probe_lib import core
from diagnostics.probe_lib.health import role_scalable_targets
from lib.service_tiers import shed_set


def format_shed_set(targets_by_service):
    """Render `{service: [(namespace, kind, name)] or None}` as the shed list.

    Exit 2 when no service is in the set, or when none renders a scalable workload. Either
    means the read most likely missed the inventory or the renderer, not that the cluster has
    nothing to shed.
    """
    scalable = {svc: t for svc, t in targets_by_service.items() if t}
    if not scalable:
        return (
            "INCONCLUSIVE: no untiered k8s service rendered a Deployment or StatefulSet, so "
            "there is nothing to scale. Check that the inventory read returned daniel-box's "
            "containers_list before believing this."
        ), 2
    lines = [
        "Scale these to zero when one node is lost, so the tiered services fit on the "
        "survivor:",
    ]
    for service, targets in scalable.items():
        for namespace, kind, name in targets:
            lines.append(f"  {service:<18} {namespace}/{kind.lower()}/{name}")
    idle = [svc for svc, t in targets_by_service.items() if not t]
    if idle:
        lines.append("")
        lines.append(
            "Untiered, with no Deployment or StatefulSet to scale: " + ", ".join(idle)
        )
    return "\n".join(lines), 0


def run_shed_set(ns):
    """Print the shed set derived from the inventory's `tier` fields."""
    namespace = core.k8s_namespace()
    targets = {svc: role_scalable_targets(svc, namespace) for svc in shed_set()}
    text, code = format_shed_set(targets)
    print(text)
    return code
