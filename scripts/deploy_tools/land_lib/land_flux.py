"""The three process boundaries a landing crosses for a tag Flux applies.

`Tools.flux` holds one `FluxReads`, so a test replaces one of its fields as it replaces any
other boundary. `promotion.split_owned` and `merge.py`'s handback refusal read which tags an
entry's `owner` takes from Ansible (`owners_at`). The refusal reads whether a tag's
Kustomization still exists (`kustomization_exists`). `promotion.wait_on_promotion` runs
`promote.py land-wait` in process (`run_land_wait`). The plan is
docs/target-architecture/deploy/d2-first-leaf.md, *How `land.sh` waits*.

`promote_wait` is imported from land.py's checkout, beside it, as `await_ci` is, so it is the
same release as the landing that calls it.
"""

import os
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))  # scripts/
# `promote_wait` imports `forge` and `changed_set` bare, from scripts/deploy_tools.
_sys.path.insert(1, str(_Path(__file__).resolve().parents[1]))
from deploy_tools.deploy_lib.owners import flux_owned
from lib import kubectl

import forge
from promote_wait import WaitResult, land_wait, live_wait_reads

KUSTOMIZATIONS = "kustomizations.kustomize.toolkit.fluxcd.io"


def owners_at(primary: Path, ref: str) -> dict[str, str]:
    """Each tag no Ansible path may deploy at commit `ref`, mapped to its `owner`.

    `deploy_lib.owners.flux_owned` is the one reader, so a landing and `deploy.sh` agree on
    which tags Flux applies.

    Raises:
        ValueError: an entry's `owner` is outside `lib.k8s_roles.OWNER_VALUES`.
        subprocess.CalledProcessError: `ref` does not hold the k8s host's host_vars file.
    """
    return flux_owned(primary, ref)


class KustomizationUnreadable(RuntimeError):
    """The read-only kubectl could not say whether a Kustomization exists."""


def kustomization_exists(name: str) -> bool:
    """Whether the Kustomization `name` exists in flux-system, read with the read-only kubectl.

    Raises:
        KustomizationUnreadable: kubectl failed for any reason but `NotFound`.
    """
    try:
        proc = kubectl.kubectl(
            "prod",
            "get",
            KUSTOMIZATIONS,
            "-n",
            "flux-system",
            name,
            "-o",
            "name",
            timeout=60,
        )
    except (
        OSError,
        subprocess.SubprocessError,
        kubectl.MissingKubectl,
        kubectl.WrongCluster,
    ) as exc:
        raise KustomizationUnreadable(str(exc)) from exc
    if proc.returncode == 0:
        return True
    if "NotFound" in proc.stderr:
        return False
    raise KustomizationUnreadable(
        f"kubectl exited {proc.returncode}: {proc.stderr.strip()}"
    )


def run_land_wait(merge: str, services: list[str], primary: Path) -> WaitResult:
    """`promote.py land-wait`, in process. Its ancestry reads run in `primary`."""
    reads = live_wait_reads(forge.connect(os.environ), primary)
    return land_wait(reads, merge, services)


@dataclass(frozen=True)
class FluxReads:
    """The boundaries above, one field each, so a test replaces one.

    Attributes:
        owners_at: `owners_at`.
        kustomization_exists: `kustomization_exists`.
        land_wait: `run_land_wait`, taking the merge commit, the published tags and the
            primary checkout.
    """

    owners_at: Callable[[Path, str], dict[str, str]] = owners_at
    kustomization_exists: Callable[[str], bool] = kustomization_exists
    land_wait: Callable[[str, list[str], Path], WaitResult] = run_land_wait
