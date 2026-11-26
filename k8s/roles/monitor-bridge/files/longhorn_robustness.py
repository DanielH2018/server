"""Which Longhorn robustness states count as lost redundancy. The one definition.

Two readers classify a volume's robustness, over different transports:

- The `LonghornVolumeDegraded` rule in observability's `storage.yaml` selects the one-hot
  `longhorn_volume_robustness` metric with `state!~"healthy|unknown"`, a regex that
  `tests/services/alert_rules/test_storage_rule_literals.py` holds to `SAFE_ROBUSTNESS`.
- `scripts/deploy_tools/runbook_gates.py` reads `status.robustness` off each `volumes.longhorn.io`
  CR through kubectl, before a k3s upgrade or a Longhorn hop.

Both carry the same Longhorn enum: longhorn-manager exports the metric from the CR field, so
`healthy`, `degraded`, `faulted` and `unknown` mean the same thing on both sides. If each side
held its own rule, the rules could disagree: a selector of `degraded|faulted` reads a state
Longhorn renamed or added as green on the tile, while the same volume stops a runbook gate that
allows only `{healthy, unknown}`.

This module is the allow-list both sides hold. It stays in monitor-bridge's `files/`, where the
runbook gates run from the checkout and reach it through a `sys.path` insert of
`lib.repo_paths.MONITOR_BRIDGE_FILES`. It does not ship in the pod, so no module on
`monitor_bridge_modules` may import it. Stdlib only and import-free, so neither side drags the
other's dependencies in.
"""

from collections.abc import Iterable

# Allow-list: an unrecognised state must read as lost redundancy. `unknown` is an idle, detached
# volume, not a fault.
SAFE_ROBUSTNESS = frozenset({"healthy", "unknown"})

# Worst state wins; an unrecognised state outranks `degraded` because nothing says it is milder.
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
