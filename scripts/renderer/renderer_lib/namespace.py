"""The shared kind `homelab-namespace`: the `homelab` Namespace and its LimitRange (D5 step 30).

`bundle.bundle_files` renders it into every bundle, and it is the only writer of both objects.
No role template writes them, so no second writer overlaps this kind. The bundle gets:

- `flux/homelab-namespace.yaml`, the Kustomization `homelab-namespace`, with a tag's fields
  and no `dependsOn`. Its `deletionPolicy: Orphan` keeps both objects if the kind ever leaves
  the bundle;
- `cluster/homelab-namespace/`: the Namespace `k8s_namespace`, labelled with `k8s_psa_labels`
  and `kustomize.toolkit.fluxcd.io/prune: disabled`, and the LimitRange
  `default-container-limits` from `k8s_default_limitrange` with `type: Container`. An empty
  `k8s_default_limitrange` renders no LimitRange.

Every flux-owned tag that renders into the namespace depends on this Kustomization
(`ordering.kustomization_edges`). This module imports neither Ansible nor Jinja, like
`bundle.py`, which imports it. The plan is docs/archive/deploy/d5-bulk-move.md,
step 30.
"""

from lib.estate import shared_vars

HOMELAB_NAMESPACE = "homelab-namespace"
LIMITRANGE = "default-container-limits"
NAMESPACE_FILE = "namespace.yaml"
LIMITRANGE_FILE = "limitrange.yaml"
# That module imports Jinja through `cluster.py`, so these are spelled out and a renderer test holds
# the two equal.
PRUNE_EXEMPT = {"kustomize.toolkit.fluxcd.io/prune": "disabled"}


def namespace_settings() -> dict:
    """`k8s_namespace`, `k8s_psa_labels` and `k8s_default_limitrange` from the group variables."""
    found = shared_vars()
    return {
        "namespace": str(found["k8s_namespace"]),
        "labels": dict(found.get("k8s_psa_labels") or {}),
        "limitrange": dict(found.get("k8s_default_limitrange") or {}),
    }


def namespace_documents() -> dict[str, dict]:
    """The kind's manifests by file name: the Namespace, and the LimitRange unless it is empty."""
    settings = namespace_settings()
    name = settings["namespace"]
    documents = {
        NAMESPACE_FILE: {
            "apiVersion": "v1",
            "kind": "Namespace",
            "metadata": {
                "name": name,
                "labels": {**settings["labels"], **PRUNE_EXEMPT},
            },
        }
    }
    if settings["limitrange"]:
        # Defaults only: see k8s_default_limitrange in group_vars/all.yml for why no min, max or
        # ResourceQuota.
        documents[LIMITRANGE_FILE] = {
            "apiVersion": "v1",
            "kind": "LimitRange",
            "metadata": {"name": LIMITRANGE, "namespace": name},
            "spec": {"limits": [{**settings["limitrange"], "type": "Container"}]},
        }
    return documents
