"""The shared kind `homelab-namespace`: the `homelab` Namespace and its LimitRange (D5 step 30).

`bundle.bundle_files` renders it into every bundle, and it is the only writer of both objects.
`ansible/deploy.yml` applied them from two pre_tasks until D5's namespace PR deleted them,
because a gate on role ownership would let a single handback re-arm Ansible's apply beside
this kind with no end to the overlap. The bundle gets:

- `flux/homelab-namespace.yaml`, the Kustomization `homelab-namespace`, with a tag's fields
  and no `dependsOn`. Its `deletionPolicy: Orphan` keeps both objects if the kind ever leaves
  the bundle;
- `cluster/homelab-namespace/`: the Namespace `k8s_namespace`, labelled with `k8s_psa_labels`
  and `kustomize.toolkit.fluxcd.io/prune: disabled`, and the LimitRange
  `default-container-limits` from `k8s_default_limitrange` with `type: Container`. Those are the
  inputs the deleted pre_task read, and an empty `k8s_default_limitrange` renders no LimitRange,
  as it applied none.

Every flux-owned tag that renders into the namespace depends on this Kustomization
(`ordering.kustomization_edges`). This module imports neither Ansible nor Jinja, like
`bundle.py`, which imports it. The plan is docs/target-architecture/deploy/d5-bulk-move.md,
step 30.
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

from pathlib import Path

from lib import yaml_fast
from lib.repo_paths import ALL_VARS

HOMELAB_NAMESPACE = "homelab-namespace"
LIMITRANGE = "default-container-limits"
NAMESPACE_FILE = "namespace.yaml"
LIMITRANGE_FILE = "limitrange.yaml"
# `prune_label.PRUNE_LABEL` and `PRUNE_DISABLED`. That module imports Jinja through
# `cluster.py`, so this one spells them out, and a renderer test holds the two equal by running
# `prune_label.label` over the render and finding nothing to add.
PRUNE_EXEMPT = {"kustomize.toolkit.fluxcd.io/prune": "disabled"}


def namespace_settings(group_vars: Path = ALL_VARS) -> dict:
    """`k8s_namespace`, `k8s_psa_labels` and `k8s_default_limitrange` from the group variables."""
    found = yaml_fast.safe_load(group_vars.read_text()) or {}
    return {
        "namespace": str(found["k8s_namespace"]),
        "labels": dict(found.get("k8s_psa_labels") or {}),
        "limitrange": dict(found.get("k8s_default_limitrange") or {}),
    }


def namespace_documents(group_vars: Path = ALL_VARS) -> dict[str, dict]:
    """The kind's manifests by file name: the Namespace, and the LimitRange unless it is empty."""
    settings = namespace_settings(group_vars)
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
        # Defaults only: see k8s_default_limitrange in group_vars/all.yml for why there is no
        # min or max here and no ResourceQuota anywhere.
        documents[LIMITRANGE_FILE] = {
            "apiVersion": "v1",
            "kind": "LimitRange",
            "metadata": {"name": LIMITRANGE, "namespace": name},
            "spec": {"limits": [{**settings["limitrange"], "type": "Container"}]},
        }
    return documents
