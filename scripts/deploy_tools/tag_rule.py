"""The tag rule: a deploy tag leaves the bundle only with its role, and `flux` never leaves.

A Kustomization that leaves the bundle takes its objects with it once it follows the default
deletion policy, so a tag dropped by a render bug or a rename would delete a running
service. The rule compares the tags of a reference bundle with those of the head
render and refuses:

- a tag in the reference that the head lacks while its role directory stays;
- `flux`, whenever the head lacks it, because its inventory holds the Flux CRDs;
- a tag the head lacks whose render in the reference holds a PersistentVolume, whether or not
  its role directory stays. C3's *Prune exemptions* does not name PersistentVolumes, so this is
  the fallback of contracts.md's *Wider prune exemptions* row: deleting the Kustomization would
  leave a bound volume terminating, where no re-apply restores it.

`check_publish` is the guard, run by `promote.py check-bundle --against-prod` before any push.
Its reference is the bundle at `prod`, never a re-render of the parent commit: `prod` moves only
on promotion, so a refusal persists until the tag returns or its role directory goes, and a
later unrelated commit still fails. A failed pull fails the publish. A `prod` the registry does
not hold is the first run, as for `decide`, and the reference is then empty.
`render_diff_gates.py`'s arm 6 is the early warning on a pull request, with the base render as
its reference. The plan is the deletion policy section of
docs/target-architecture/deploy/d6-delete-old-path.md.
"""

import sys
from collections.abc import Collection, Iterable
from pathlib import Path
from typing import Protocol

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))  # scripts/
from lib.exit_codes import FAILED, OK
from lib.k8s_roles import role_dirs
from lib.repo_paths import K8S_ROLES

from artifact_retention import PROD_TAG
from deploy_tools.object_loss import bundle_documents
from deploy_tools.promote_lib.prune import READ_ERRORS
from renderer.renderer_lib.bundle import (
    FLUX_DIR,
    FLUX_TAG,
    KUSTOMIZATION,
    SHARED_KINDS,
)
from renderer.renderer_lib.ordering import split_kind

# The trailing slash keeps PersistentVolumeClaim out.
_PERSISTENT_VOLUME = "PersistentVolume/"


class BundleReader(Protocol):
    """The one registry read the publish arm makes (`forge.Registry.bundle`)."""

    def bundle(self, reference: str) -> dict[str, bytes] | None: ...


def bundle_tags(paths: Iterable[str]) -> frozenset[str]:
    """The deploy tags a bundle publishes, from its file paths relative to the bundle root."""
    prefix, index = f"{FLUX_DIR}/", f"{FLUX_DIR}/{KUSTOMIZATION}"
    names = {
        path.removeprefix(prefix).removesuffix(".yaml")
        for path in paths
        if path.startswith(prefix) and path.endswith(".yaml") and path != index
    }
    return frozenset(names - set(SHARED_KINDS))


def persistent_volume_tags(objects: Iterable[tuple[str, str]]) -> frozenset[str]:
    """The tags that render a PersistentVolume.

    Args:
        objects: Each object as `<kind>/<namespace>/<name>`, with the `cluster/` directory, or
            the tag, holding it.
    """
    return frozenset(
        split_kind(directory)[0]
        for ident, directory in objects
        if ident.startswith(_PERSISTENT_VOLUME)
    )


def refusals(
    reference: Collection[str],
    head: Collection[str],
    roles: Collection[str],
    holding_volumes: Collection[str],
) -> list[str]:
    """Why each tag may not leave the bundle, one line each; empty when none is refused.

    Args:
        reference: The tags of the reference bundle.
        head: The tags of the head render.
        roles: The role directories at the head.
        holding_volumes: The tags whose render in the reference holds a PersistentVolume.
    """
    found = []
    for tag in sorted((set(reference) | {FLUX_TAG}) - set(head)):
        if tag == FLUX_TAG:
            found.append(
                f"{FLUX_TAG} is not in the render. Its inventory holds the Flux CRDs, so it "
                "never leaves the bundle."
            )
        elif tag in holding_volumes:
            found.append(
                f"{tag} leaves the bundle and its render holds a PersistentVolume, which C3 "
                "gives no prune exemption. First drop the volume from the render in a PR of "
                "its own, or amend C3 (contracts.md, *Wider prune exemptions*)."
            )
        elif tag in roles:
            found.append(
                f"{tag} leaves the bundle while ansible/roles/k8s/{tag}/ stays. Restore its "
                "containers_list entry, or remove the role with it to retire the service "
                "(docs/flux-deploy-plane.md, *Retiring a flux-owned service*)."
            )
    return found


def role_names(roles_dir: Path) -> frozenset[str]:
    """The names of the real role directories under `roles_dir`."""
    return frozenset(path.name for path in role_dirs(roles_dir))


def check_publish(
    out: Path, registry: BundleReader, roles_dir: Path = K8S_ROLES
) -> int:
    """Judge the bundle at `out` against the bundle at `prod`, and return the exit code.

    Prints one line per refusal, or one line saying which reference passed.
    """
    try:
        files = registry.bundle(PROD_TAG)
        reference = files or {}
        volumes = persistent_volume_tags(
            (ident, directory) for ident, directory, _ in bundle_documents(reference)
        )
    except READ_ERRORS as err:
        print(
            f"check-bundle: cannot read the bundle at {PROD_TAG}, so the tag rule cannot "
            f"judge: {err}; nothing may be published",
            file=sys.stderr,
        )
        return FAILED
    held = (path.relative_to(out).as_posix() for path in out.rglob("*.yaml"))
    found = refusals(
        bundle_tags(reference), bundle_tags(held), role_names(roles_dir), volumes
    )
    for line in found:
        print(f"check-bundle: tag rule: {line}", file=sys.stderr)
    if found:
        return FAILED
    against = PROD_TAG if files is not None else f"no {PROD_TAG} yet, the first run"
    print(f"check-bundle: the tag rule passes against {against}")
    return OK
