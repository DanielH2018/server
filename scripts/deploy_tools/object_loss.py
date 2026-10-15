"""`promote.py decide`'s object-loss check: a candidate may lose an object only on purpose.

A render that drops a NetworkPolicy, an IngressRoute, a CronJob, a Service or an RBAC object
gets it pruned while its Kustomization stays Ready, so `verify` passes. `decide` therefore
compares the objects, as `<kind>/<namespace>/<name>`, in candidate T's `cluster/` with those in
`prod`'s. T is eligible only when every object it loses is declared by a line

    Removes-Object: <kind>/<namespace>/<name>

in the message of some commit in (`prod`, T] (`lib/removes_object.py` holds the grammar). A
cluster-scoped object leaves the namespace empty, as in `ClusterRole//<name>`. An object whose whole Kustomization leaves T's bundle, as
at a handback, is exempt.

`decide` reads `prod`'s bundle once per run and each weighed candidate's once, and skips an
ineligible candidate with the line `refusal` returns. The plan is
docs/target-architecture/deploy/d2-first-leaf.md, *Publication and verification*, and the
operator's page docs/flux-deploy-plane.md, *Removing an object on purpose*.
"""

import yaml

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))  # scripts/
from lib import yaml_fast

from deploy_tools.promote_lib import verify
from deploy_tools.promote_lib.prune import ReadError
from renderer.renderer_lib.bundle import CLUSTER_DIR

# The kustomize file each bundle directory carries is not an object the cluster holds.
_KUSTOMIZE_API = "kustomize.config.k8s.io/"


def bundle_objects(files: dict[str, bytes]) -> dict[str, str]:
    """Each object under a bundle's `cluster/`, as `<kind>/<namespace>/<name>`.

    Returns:
        Each object mapped to the `cluster/<name>/` directory holding it, which is the name
        of the Kustomization that applies it.

    Raises:
        ReadError: a manifest does not parse, so the bundle cannot be compared.
    """
    objects = {}
    for path in sorted(files):
        parts = path.split("/")
        if parts[0] != CLUSTER_DIR or len(parts) < 3 or not path.endswith(".yaml"):
            continue
        try:
            docs = list(yaml_fast.safe_load_all(files[path]))
        except yaml.YAMLError as err:
            raise ReadError(f"bundle file {path} does not parse: {err}") from err
        for doc in docs:
            if not isinstance(doc, dict) or str(doc.get("apiVersion", "")).startswith(
                _KUSTOMIZE_API
            ):
                continue
            meta = doc.get("metadata") or {}
            if doc.get("kind") and meta.get("name"):
                ident = f"{doc['kind']}/{meta.get('namespace') or ''}/{meta['name']}"
                objects[ident] = parts[1]
    return objects


def undeclared_losses(
    prod: dict[str, bytes], target: dict[str, bytes], declared: frozenset[str]
) -> list[str]:
    """The objects `prod`'s bundle holds and `target`'s lacks, that no line declares.

    An object whose Kustomization is absent from `target`'s `flux/` is exempt: a handback
    removes the whole Kustomization, and its `deletionPolicy: Orphan` prunes nothing. The
    exemption holds only while C3's Orphan policy does, and D6.7's switch away from Orphan
    must remove it.

    Raises:
        ReadError: a manifest in either bundle does not parse.
    """
    try:
        kustomizations = verify.flux_kustomizations(target)
    except yaml.YAMLError as err:
        raise ReadError(f"a flux/ file of T's bundle does not parse: {err}") from err
    kept = {str((doc.get("metadata") or {}).get("name")) for doc in kustomizations}
    held = bundle_objects(target)
    return sorted(
        ident
        for ident, owner in bundle_objects(prod).items()
        if ident not in held and owner in kept and ident not in declared
    )


def refusal(
    prod_bundle: dict[str, bytes],
    target_bundle: dict[str, bytes],
    declared: frozenset[str],
    prod: str,
    target: str,
) -> str:
    """The line `decide` logs when it skips `target` for a lost object, or '' when eligible.

    Raises:
        ReadError: a manifest in either bundle does not parse.
    """
    lost = undeclared_losses(prod_bundle, target_bundle, declared)
    if not lost:
        return ""
    return (
        f"promote.py: skip {target}: it loses {', '.join(lost)}, which no "
        f"Removes-Object line in ({prod}, {target}] declares"
    )
