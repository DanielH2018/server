"""Run the stop conditions of a MetalLB pin bump in order, exit code naming the first failure.

After H1, merging `k3s_metallb_version` is the upgrade, so `land.sh` runs these gates before
it merges such a bump (docs/target-architecture/host/h1-cluster-config-to-flux.md, *Longhorn
and MetalLB bumps gate at the merge*). MetalLB owns the LoadBalancer VIPs, and its speakers
announce them from every node, so the bump must land on a cluster that is whole and on a
promotion that is not already holding a failed change. Both gates are `k3s_upgrade.py`'s own
verdicts, reused rather than restated:

  1. `DEPLOY_HOLD` reads `none`. A hold means a previous promotion failed its health gate,
     and the upgrade would land on top of that.
  2. Every production node is Ready. A speaker on a NotReady node cannot announce, so a VIP
     that fails over during the roll has nowhere to go.

Gate 1 reads the `DEPLOY_HOLD` repository variable, as `k3s_upgrade.py`'s gate 3 does.

Exit codes:
  0      every gate passed
  1..2   the first gate that failed, by its number above
  69     the cluster could not be asked, or `DEPLOY_HOLD` could not be read

Usage:
    uv run python scripts/deploy_tools/runbook_gates.py metallb-upgrade
"""

import sys
from collections.abc import Callable
from pathlib import Path as _Path

# Reach `scripts/`, for `lib` and `deploy_tools`: the entrypoint puts only its own directory on
# sys.path, and pyproject's `pythonpath` is a pytest setting.
sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

from deploy_tools.runbook_gates_lib import gate_runner
from deploy_tools.runbook_gates_lib.gate_runner import Gate, cluster_doc
from deploy_tools.runbook_gates_lib.k3s_upgrade import (
    CLUSTER,
    LIVE_HOLD,
    NODES_ARGS,
    deploy_hold,
    nodes_not_ready,
)
from lib.kubectl import DEFAULT_TOOLS, Tools

# No MetalLB runbook exists; the plan section that defines these gates is the one to read.
RUNBOOK = "docs/target-architecture/host/h1-cluster-config-to-flux.md"


def _gate_hold(tools: Tools, read_hold: Callable[[], str | None]) -> list[str]:
    return deploy_hold(read_hold)


def _gate_nodes(tools: Tools, read_hold: Callable[[], str | None]) -> list[str]:
    return nodes_not_ready(cluster_doc(CLUSTER, tools, NODES_ARGS))


# Order is the gate number. Append; never reorder.
GATES = (
    Gate(1, "DEPLOY_HOLD reads none", _gate_hold),
    Gate(2, "every production node Ready", _gate_nodes),
)


def run_gates(
    tools: Tools = DEFAULT_TOOLS,
    read_hold: Callable[[], str | None] = LIVE_HOLD,
    out=sys.stdout,
) -> int:
    """Run every gate in order, print one line per gate, and return the exit code."""
    return gate_runner.run_gates(GATES, RUNBOOK, tools, read_hold, out=out)


def main(argv: list[str] | None = None) -> int:
    return gate_runner.cli(__doc__, argv, run_gates)
