"""Which Longhorn robustness states count as lost redundancy. The one definition (#3668).

Two readers classify a volume's robustness, over different transports:

- monitor-bridge's `longhorn_volumes` check reads the one-hot `longhorn_volume_robustness`
  metric out of Prometheus, inside a stdlib-only pod.
- `scripts/deploy_tools/runbook_gates.py` reads `status.robustness` off each `volumes.longhorn.io`
  CR through kubectl, before a k3s upgrade or a Longhorn hop.

Both carry the same Longhorn enum: longhorn-manager exports the metric from the CR field, so
`healthy`, `degraded`, `faulted` and `unknown` mean the same thing on both sides. Until #3668
each side held its own rule, and the rules disagreed. The runbook gates allowed
`{healthy, unknown}` and refused anything else. The bridge selected `degraded|faulted`, so a
state Longhorn renamed or added would read green on the tile while the same volume stopped a
runbook.

This module is the allow-list both now call. It lives in monitor-bridge's `files/` because the
pod can import only what its ConfigMap ships; the runbook gates run from the checkout and reach
it through a `sys.path` insert of `lib.repo_paths.MONITOR_BRIDGE_FILES`. Stdlib only and
import-free, so neither side drags the other's dependencies in.
"""

from collections.abc import Iterable

# An allow-list, not a deny-list: a state this module has not heard of (a rename, a new value
# after a Longhorn bump) must read as lost redundancy, not pass. `healthy` is a volume with every
# replica; `unknown` is what an idle, detached volume reports, which is not a fault — 6 of 43
# volumes read it on 2026-08-17, including the game servers scaled to zero on purpose.
SAFE_ROBUSTNESS = frozenset({"healthy", "unknown"})

# The worst state wins when one volume reports twice (two longhorn-manager pods, or a scrape
# that caught a transition). `faulted` has no healthy replica left at all, so it outranks
# everything; an unrecognised state outranks `degraded` because nothing says it is milder.
_RANK = {"degraded": 1, "faulted": 3}
_UNRECOGNISED_RANK = 2


def unsafe_volumes(states: Iterable[tuple[str, str]]) -> dict[str, str]:
    """Volume name -> worst robustness state, for every volume outside `SAFE_ROBUSTNESS`.

    Args:
      states: `(volume name, robustness state)` pairs, in the order the caller read them. An
        empty state is kept as "", which is unsafe: a volume reporting no robustness is one
        nobody can vouch for.

    Returns:
      The unsafe volumes in first-seen order. A volume that reported a safe state on one row
      and an unsafe one on another is unsafe.
    """
    worst: dict[str, str] = {}
    for name, state in states:
        if state in SAFE_ROBUSTNESS:
            continue
        held = worst.get(name)
        if held is None or _rank(state) > _rank(held):
            worst[name] = state
    return worst


def _rank(state: str) -> int:
    return _RANK.get(state, _UNRECOGNISED_RANK)
