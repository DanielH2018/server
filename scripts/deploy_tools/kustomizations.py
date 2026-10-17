"""Fields of a Kustomization as `kubectl get -o json` lists it: its origin commit, Ready, edges.

`promote.py land-wait` reads them off the live list (`wait.py`), and `verify` and the chained
timeout read the `dependsOn` graph off a bundle's documents in the same shape. Every reader
tolerates a malformed item, because the list comes from the cluster.

Run: imported only. `uv run pytest scripts/deploy_tools/tests/test_promote_land_wait.py`
"""

import re

# The `lastAppliedOriginRevision` form that names a commit: the `--revision` that `flux push
# artifact` writes (contract C6). promote.py reads the same form off `prod`'s manifest.
REVISION = re.compile(r"master@sha1:([0-9a-f]{40})")


def origin_commit(kustomization: dict) -> str | None:
    """The commit `status.lastAppliedOriginRevision` names, or None for any other value.

    Only the `master@sha1:<40 hex>` form names a commit. `status.lastAppliedRevision` holds
    `<tag>@sha256:<digest>`, which no git command takes, so it is never read (contract C6).
    """
    status = kustomization.get("status")
    revision = (
        status.get("lastAppliedOriginRevision") if isinstance(status, dict) else None
    )
    match = REVISION.fullmatch(revision) if isinstance(revision, str) else None
    return match.group(1) if match else None


def is_ready(kustomization: dict) -> bool:
    """Whether the Kustomization's `Ready` condition reads `True`."""
    status = kustomization.get("status")
    conditions = status.get("conditions") if isinstance(status, dict) else None
    return isinstance(conditions, list) and any(
        isinstance(c, dict) and c.get("type") == "Ready" and c.get("status") == "True"
        for c in conditions
    )


def kustomization_name(kustomization: dict) -> str:
    """`metadata.name`, or '' when the item carries none."""
    metadata = kustomization.get("metadata")
    name = metadata.get("name") if isinstance(metadata, dict) else None
    return name if isinstance(name, str) else ""


def kustomization_graph(items: list[dict]) -> tuple[dict[str, list[str]], set[str]]:
    """Each Kustomization's `dependsOn` names, and the names that are suspended."""
    depends_on: dict[str, list[str]] = {}
    suspended: set[str] = set()
    for item in items:
        name = kustomization_name(item)
        if not name:
            continue
        spec = item.get("spec")
        spec = spec if isinstance(spec, dict) else {}
        deps = spec.get("dependsOn")
        depends_on[name] = [
            d["name"]
            for d in (deps if isinstance(deps, list) else [])
            if isinstance(d, dict) and isinstance(d.get("name"), str)
        ]
        if spec.get("suspend") is True:
            suspended.add(name)
    return depends_on, suspended


def with_dependents(
    names: frozenset[str], depends_on: dict[str, list[str]], suspended: set[str]
) -> frozenset[str]:
    """`names`, plus every Kustomization that depends on one of them, directly or not.

    A suspended dependent is left out, because Flux applies nothing for it.
    """
    found = set(names)
    grew = True
    while grew:
        grew = False
        for name, deps in depends_on.items():
            if name not in found and name not in suspended and found.intersection(deps):
                found.add(name)
                grew = True
    return frozenset(found)
