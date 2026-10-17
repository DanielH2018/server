"""`promote.py decide`'s object-loss check: a candidate may lose an object only on purpose.

A render that drops a NetworkPolicy, an IngressRoute, a CronJob, a Service or an RBAC object
gets it pruned while its Kustomization stays Ready, so `verify` passes. `decide` therefore
compares the objects, as `<kind>/<namespace>/<name>`, in candidate T's `cluster/` with those in
`prod`'s. T is eligible only when every object it loses is declared by a line

    Removes-Object: <kind>/<namespace>/<name>

in the message of some commit in (`prod`, T] (`lib/removes_object.py` holds the grammar). A
cluster-scoped object leaves the namespace empty, as in `ClusterRole//<name>`. An object whose whole Kustomization leaves T's bundle, as
at a handback, is exempt. A rename is not a removal: an object whose name, with the
renderer's trailing `-<digest8>` removed, still renders in T passes, as the soak Job and its
ConfigMap do on every revision. Nor is an object labelled or annotated
`kustomize.toolkit.fluxcd.io/prune: disabled`, which Flux's prune leaves in place (D5.1f,
*What it exempts*). The PR gate's arm 3, `scripts/validate/cross_plane_order.py`, applies both
exemptions through `stem` and `prune_disabled` here, so the two checks cannot drift (#5291).

`decide` reads `prod`'s bundle once per run and each weighed candidate's once, and skips an
ineligible candidate with the line `refusal` returns. The plan is
docs/target-architecture/deploy/d2-first-leaf.md, *Publication and verification*, and the
operator's page docs/flux-deploy-plane.md, *Removing an object on purpose*.
"""

import re
from collections.abc import Iterator, Mapping

import yaml

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))  # scripts/
from lib import yaml_fast

from deploy_tools.promote_lib import verify_promotion as verify
from deploy_tools.promote_lib.prune import ReadError
from renderer.renderer_lib.bundle import CLUSTER_DIR

# The kustomize file each bundle directory carries is not an object the cluster holds.
_KUSTOMIZE_API = "kustomize.config.k8s.io/"
# The suffix `digest8.py:job_spec_digest8` and `configmap_digest8` give a name: a rename by
# digest moves it, and both this check and the PR gate's arm 3 strip it before comparing.
_DIGEST_SUFFIX = re.compile(r"-[0-9a-f]{8}$")
PRUNE_KEY = "kustomize.toolkit.fluxcd.io/prune"


def stem(ident: str) -> str:
    """`<kind>/<namespace>/<name>` with the name's trailing `-<digest8>` removed."""
    return _DIGEST_SUFFIX.sub("", ident)


def prune_disabled(doc: Mapping) -> bool:
    """Whether Flux's prune skips `doc`, which it does by a label or an annotation."""
    meta = doc.get("metadata") or {}
    return any(
        str((meta.get(key) or {}).get(PRUNE_KEY, "")) == "disabled"
        for key in ("labels", "annotations")
    )


def bundle_documents(files: dict[str, bytes]) -> Iterator[tuple[str, str, dict]]:
    """Each object under a bundle's `cluster/`, with its directory and its document.

    Yields:
        The object as `<kind>/<namespace>/<name>`, the `cluster/<name>/` directory holding
        it, which is the name of the Kustomization that applies it, and the parsed document.

    Raises:
        ReadError: a manifest does not parse, so the bundle cannot be compared.
    """
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
                yield ident, parts[1], doc


def bundle_objects(files: dict[str, bytes]) -> dict[str, str]:
    """Each object under a bundle's `cluster/`, as `<kind>/<namespace>/<name>`.

    Returns:
        Each object mapped to the `cluster/<name>/` directory holding it, which is the name
        of the Kustomization that applies it.

    Raises:
        ReadError: a manifest does not parse, so the bundle cannot be compared.
    """
    return {ident: owner for ident, owner, _ in bundle_documents(files)}


def undeclared_losses(
    prod: dict[str, bytes], target: dict[str, bytes], declared: frozenset[str]
) -> list[str]:
    """The objects `prod`'s bundle holds and `target`'s lacks, that no line declares.

    An object whose Kustomization is absent from `target`'s `flux/` is exempt: a handback
    removes the whole Kustomization, and its `deletionPolicy: Orphan` prunes nothing. The
    exemption holds only while C3's Orphan policy does, and D6.7's switch away from Orphan
    must remove it. An object `prune_disabled` in `prod` is exempt, as prune never deletes
    it, and so is one whose `stem` still renders in `target`.

    Raises:
        ReadError: a manifest in either bundle does not parse.
    """
    try:
        kustomizations = verify.flux_kustomizations(target)
    except yaml.YAMLError as err:
        raise ReadError(f"a flux/ file of T's bundle does not parse: {err}") from err
    kept = {str((doc.get("metadata") or {}).get("name")) for doc in kustomizations}
    held = bundle_objects(target)
    renamed = {stem(ident) for ident in held}
    return sorted(
        {
            ident
            for ident, owner, doc in bundle_documents(prod)
            if ident not in held
            and stem(ident) not in renamed
            and not prune_disabled(doc)
            and owner in kept
            and ident not in declared
        }
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
