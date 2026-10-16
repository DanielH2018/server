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

Each service also carries its owner, read through `lib.k8s_roles.tag_owners`: `ansible`, or
the `owner` its `containers_list` entry sets. Flux's drift correction restores a flux-owned
service's rendered replicas, so for each one the output also prints the suspend that must
come before the scale and the resume that comes after it, both with the field manager
`homelab-operator` (D5.1b, docs/break-glass.md's *one node is down and the survivor runs short
of memory*). Both are operator-only, since they need the k3s admin kubeconfig.
"""

# `probe_lib` is a namespace package under `scripts/`, so reaching a sibling by package name
# needs `scripts/` on sys.path — a module gets only its importer's path otherwise, and
# pyproject's `pythonpath` is a pytest setting. This has to sit ABOVE the imports below.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

from diagnostics.probe_lib import core
from diagnostics.probe_lib.health import role_scalable_targets
from lib.k8s_roles import k8s_entries, tag_owners
from lib.render_guard import entry_tags
from lib.service_tiers import shed_set

ANSIBLE = "ansible"
FLUX = "flux"
# The hand suspend and resume of D5.1b. `homelab-operator` matches none of the managers
# kustomize-controller cleans, so the next reconcile of the root leaves the field alone.
SUSPEND_PATCH = (
    "sudo k3s kubectl -n flux-system patch kustomization {tag} --type merge "
    '--field-manager=homelab-operator -p \'{{"spec":{{"suspend":{value}}}}}\''
)


def suspend_command(tag: str, suspend: bool = True) -> str:
    """The operator's patch that suspends, or with `suspend=False` resumes, the tag `tag`."""
    return SUSPEND_PATCH.format(tag=tag, value="true" if suspend else "false")


def service_owners(entries, owners):
    """Each service in `entries` mapped to `{tag: owner}`, an untagged owner read as `ansible`.

    Args:
        entries: the `containers_list` entries by service name.
        owners: each deploy tag's `owner` (`tag_owners`); a tag it does not map is
            Ansible-owned.
    """
    return {
        name: {tag: owners.get(tag, ANSIBLE) for tag in entry_tags(entry)}
        for name, entry in entries.items()
    }


def _owner(tags):
    found = sorted(set((tags or {}).values()))
    return ",".join(found) if found else ANSIBLE


def format_shed_set(targets_by_service, owners_by_service=None):
    """Render `{service: [(namespace, kind, name)] or None}` as the shed list.

    `owners_by_service` maps each service to `{tag: owner}` (`service_owners`); a service it
    does not map is Ansible-owned. Each flux-owned tag of a scalable service gets its suspend
    and resume commands.

    Exit 2 when no service is in the set, or when none renders a scalable workload. Either
    means the read most likely missed the inventory or the renderer, not that the cluster has
    nothing to shed.
    """
    owners_by_service = owners_by_service or {}
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
        owner = _owner(owners_by_service.get(service))
        for namespace, kind, name in targets:
            lines.append(
                f"  {service:<18} {namespace}/{kind.lower()}/{name:<40} owner={owner}"
            )
    flux_tags = [
        tag
        for service in scalable
        for tag, owner in sorted((owners_by_service.get(service) or {}).items())
        if owner == FLUX
    ]
    if flux_tags:
        lines += [
            "",
            "Flux-owned: suspend each tag before scaling it, or Flux restores its replicas "
            "(operator-only, docs/break-glass.md):",
            *(f"  {suspend_command(tag)}" for tag in flux_tags),
            "Resume each once the node is back, after reading DEPLOY_HOLD "
            "(operator-only, docs/break-glass.md step 4):",
            *(f"  {suspend_command(tag, suspend=False)}" for tag in flux_tags),
        ]
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
    owners = service_owners(k8s_entries(), tag_owners())
    text, code = format_shed_set(targets, owners)
    print(text)
    return code
