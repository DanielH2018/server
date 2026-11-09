"""The process boundaries a landing crosses for a tag Flux applies.

`Tools.flux` holds one `FluxReads`, so a test replaces one of its fields as it replaces any
other boundary. `promotion.split_k8s` and the gate read which tags have a k8s entry, each one
Flux applies (`k8s_tags_at`).
`promotion.wait_on_promotion` runs
`promote.py land-wait` in process (`run_land_wait`). The plan is
docs/target-architecture/deploy/d2-first-leaf.md, *How `land.sh` waits*. `pins` holds the reads
the pin gate makes before a forward-only pin bump merges (`pin_reads.py`, `pin_gate.py`): Flux
applies those pins on promotion, so their gate sits with the other Flux boundaries.

`promote_lib/wait.py` is imported from land.py's checkout, beside it, as `await_ci` is, so it is the
same release as the landing that calls it.
"""

import os
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))  # scripts/
# `promote_lib/wait.py` imports `forge` and `changed_set` bare, from scripts/deploy_tools.
_sys.path.insert(1, str(_Path(__file__).resolve().parents[1]))
from deploy_tools.deploy_lib.owners import declared_k8s_tags
from deploy_tools.land_lib.pin_reads import PinReads

import forge
from deploy_tools.promote_lib.wait import WaitResult, land_wait
from deploy_tools.promote_lib.wait_reads import live_wait_reads


def k8s_tags_at(primary: Path, ref: str) -> set[str]:
    """Every tag of a k8s `containers_list` entry at commit `ref`.

    Raises:
        subprocess.CalledProcessError: `ref` does not hold the k8s host's host_vars file.
    """
    return declared_k8s_tags(primary, ref)


def run_land_wait(
    merge: str,
    services: list[str],
    primary: Path,
    cap_s: float | None = None,
    window_grace_s: int = 0,
) -> WaitResult:
    """`promote.py land-wait`, in process. Its ancestry reads run in `primary`."""
    reads = live_wait_reads(forge.connect(os.environ), primary)
    return land_wait(reads, merge, services, cap_s=cap_s, window_grace_s=window_grace_s)


@dataclass(frozen=True)
class FluxReads:
    """The boundaries above, one field each, so a test replaces one.

    Attributes:
        land_wait: `run_land_wait`, taking the merge commit, the tags the landing names
            on its own, the primary checkout, the cap in seconds, or None for none, and the
            most the reboot window may add to the cap.
        pins: the pin gate's reads (`pin_reads.PinReads`).
        k8s_tags_at: `k8s_tags_at`.
    """

    land_wait: Callable[[str, list[str], Path, float | None, int], WaitResult] = (
        run_land_wait
    )
    pins: PinReads = field(default_factory=PinReads)
    k8s_tags_at: Callable[[Path, str], set[str]] = k8s_tags_at
