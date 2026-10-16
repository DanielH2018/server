"""`probe.py landing`'s promotion lines: both holds, `prod` and `last-good`, then suspensions.

Until slice D6 deletes the GitOps deployer, two holds coexist. The deployer's `hold_sha`
holds the Ansible-owned tags, and the `promote` workflow's repository variable `DEPLOY_HOLD`
holds the Flux-owned ones. `hold_lines` prints a line for each on every run, even when it
reads `none`, so neither can be forgotten.

The first three reads are the ones docs/target-architecture/deploy/d2-first-leaf.md (*What
`probe.py landing` reads, and what it costs*) prices: one `api.github.com` call, two `ghcr.io`
requests and one cluster read. daniel-box has no flux CLI, and an anonymous `api.github.com`
poll would spend the host's quota that the deployer's CI gate reads.

The fourth is a second cluster read, D5.1b's: every Kustomization and OCIRepository in
`flux-system` whose `spec.suspend` is true, the root `homelab` included, with the field
manager of that field. A suspend from git reads as kustomize-controller's, and an operator's
hand suspend as `homelab-operator`'s (docs/target-architecture/deploy/d5-bulk-move.md, *The
stop lever*).

Run: imported only, by `landing_blockers.py`.
"""

import json
import re
from collections.abc import Callable
from collections.abc import Mapping
from dataclasses import dataclass

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))
_sys.path.insert(0, str(_Path(__file__).resolve().parents[2] / "deploy_tools"))

import forge
from lib import kubectl as _kubectl
from lib.gh import gh

# Each promotion read's timeout. On 2026-10-14, three runs from daniel-box took at most 0.37s
# for `gh variable get DEPLOY_HOLD`, 0.37s for the GHCR token request plus one manifest GET,
# and 0.09s for the kubectl read of the OCIRepository. On 2026-10-16 the suspension read of
# six Flux objects with their managed fields took 0.08s, three times. 5s is over 13 times the
# slowest, so a timeout means the source is down rather than slow. A timeout downgrades that
# one line.
PROMOTE_READ_TIMEOUT_S = 5

# `flux push artifact --revision master@sha1:<sha>` writes the commit under this annotation,
# and source-controller copies it into the OCIRepository's `status.artifact.metadata`.
REVISION_ANNOTATION = "org.opencontainers.image.revision"
# The Flux kinds an operator suspends: each Kustomization and the root's source.
SUSPENDABLE_KINDS = "kustomizations.kustomize.toolkit.fluxcd.io,ocirepositories.source.toolkit.fluxcd.io"


@dataclass(frozen=True)
class PromoteReads:
    """The three promotion reads, one callable each, each raising on failure.

    Attributes:
        deploy_hold: DEPLOY_HOLD's value, or None when the variable does not exist.
        prod: `{"commit", "digest"}` that the OCIRepository `homelab` last fetched for `prod`.
        last_good: `{"commit", "digest"}` of the tag `last-good`, or None while it is absent.
        suspended: `{"kind", "name", "managers"}` for each suspended Flux object
            (`suspended_objects`). None makes no such read.
    """

    deploy_hold: Callable[[], str | None]
    prod: Callable[[], dict]
    last_good: Callable[[], dict | None]
    suspended: Callable[[], list[dict]] | None = None


_ARTIFACT_REVISION = re.compile(r"master@sha1:([0-9a-f]{40})")
_ARTIFACT_DIGEST = re.compile(r"[^@]*@(sha256:[0-9a-f]{64})")


def _deploy_hold() -> str | None:
    proc = gh(
        "variable",
        "get",
        forge.HOLD_VARIABLE,
        "-R",
        forge.REPOSITORY,
        check=False,
        timeout=PROMOTE_READ_TIMEOUT_S,
    )
    if proc.returncode == 0:
        return proc.stdout.strip()
    if f"variable {forge.HOLD_VARIABLE} was not found" in proc.stderr:
        return None
    raise RuntimeError(
        f"gh variable get exited {proc.returncode}: {proc.stderr.strip()}"
    )


def artifact_of(ocirepository: dict) -> dict:
    """`{"commit", "digest"}` from an OCIRepository's `status.artifact`.

    The digest is the one `revision` carries (`prod@sha256:<hex>`), and the commit is the
    `org.opencontainers.image.revision` that source-controller copies from the manifest.

    Raises:
        ValueError: either is missing or not in that form.
    """
    artifact = (ocirepository.get("status") or {}).get("artifact") or {}
    digest = _ARTIFACT_DIGEST.fullmatch(str(artifact.get("revision", "")))
    commit = _ARTIFACT_REVISION.fullmatch(
        str((artifact.get("metadata") or {}).get(REVISION_ANNOTATION, ""))
    )
    if digest is None or commit is None:
        raise ValueError(f"the OCIRepository's status.artifact is {artifact!r}")
    return {"commit": commit.group(1), "digest": digest.group(1)}


def _prod() -> dict:
    proc = _kubectl.kubectl(
        "prod",
        "get",
        "ocirepositories.source.toolkit.fluxcd.io",
        "-n",
        "flux-system",
        "homelab",
        "-o",
        "json",
        timeout=PROMOTE_READ_TIMEOUT_S,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"kubectl exited {proc.returncode}: {proc.stderr.strip()}")
    return artifact_of(json.loads(proc.stdout))


def _transport(method: str, url: str, headers: Mapping[str, str]) -> forge.Response:
    return forge.urllib_transport(method, url, headers, timeout=PROMOTE_READ_TIMEOUT_S)


def _last_good() -> dict | None:
    registry = forge.Registry(_transport, None)
    found = registry.tagged("last-good")
    if found is None:
        return None
    manifest, digest = found
    revision = (manifest.get("annotations") or {}).get(REVISION_ANNOTATION, "")
    commit = _ARTIFACT_REVISION.fullmatch(str(revision))
    return {"commit": commit.group(1) if commit else "", "digest": digest}


def suspend_managers(obj: dict) -> list[str]:
    """The sorted field managers that own `spec.suspend` in `obj`'s `managedFields`."""
    managers = set()
    for entry in (obj.get("metadata") or {}).get("managedFields") or []:
        spec = (entry.get("fieldsV1") or {}).get("f:spec")
        if isinstance(spec, dict) and "f:suspend" in spec:
            managers.add(str(entry.get("manager", "")))
    return sorted(managers)


def suspended_objects(items: list[dict]) -> list[dict]:
    """`{"kind", "name", "managers"}` for each of `items` whose `spec.suspend` is true."""
    return [
        {
            "kind": item.get("kind", ""),
            "name": (item.get("metadata") or {}).get("name", ""),
            "managers": suspend_managers(item),
        }
        for item in items
        if (item.get("spec") or {}).get("suspend") is True
    ]


def _suspended() -> list[dict]:
    proc = _kubectl.kubectl(
        "prod",
        "get",
        SUSPENDABLE_KINDS,
        "-n",
        "flux-system",
        "-o",
        "json",
        "--show-managed-fields",
        timeout=PROMOTE_READ_TIMEOUT_S,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"kubectl exited {proc.returncode}: {proc.stderr.strip()}")
    return suspended_objects(json.loads(proc.stdout).get("items") or [])


LIVE_PROMOTE_READS = PromoteReads(_deploy_hold, _prod, _last_good, _suspended)


def hold_lines(snap: dict) -> list[str]:
    """The two holds, `prod` and `last-good`, then the suspended Flux objects.

    The two holds coexist while the deployer and `promote` do. Each hold line names its source and the plane it holds, and prints even when it reads
    `none`. A `failed=` hold prints its names. `prod` is reported equal to `last-good` only
    when both digests were read and match. The suspension line names each object's field
    managers for `spec.suspend`, so a hand suspend (`homelab-operator`) reads differently from
    one rendered from git.
    """
    promote = snap.get("promote") or {}
    hold = snap.get("hold")
    deploy_hold = promote.get("deploy_hold")
    prod, last_good = promote.get("prod"), promote.get("last_good")

    def artifact(value: dict | None) -> str:
        if value is None:
            return "unknown"
        if not value["digest"]:
            return "none"
        return f"{value['commit'][:8] or '?'} {value['digest'][:19]}"

    same = bool(prod and last_good and last_good["digest"] == prod["digest"])
    return [
        "hold_sha (GitOps deployer, Ansible-owned tags): "
        + ("unknown" if hold is None else hold["sha"][:8] or "none"),
        "DEPLOY_HOLD (promote workflow, Flux-owned tags): "
        + ("unknown" if deploy_hold is None else deploy_hold or "none"),
        f"prod: {artifact(prod)}   last-good: "
        + ("the same digest as prod" if same else artifact(last_good)),
        "suspended (Flux): " + suspension(promote.get("suspended")),
    ]


def suspension(objects: list[dict] | None) -> str:
    """The suspended Flux objects as one line's value: `unknown`, `none`, or each named."""
    if objects is None:
        return "unknown"
    if not objects:
        return "none"
    return "; ".join(
        f"{o['kind']}/{o['name']} by {','.join(o['managers']) or 'no manager'}"
        for o in objects
    )
