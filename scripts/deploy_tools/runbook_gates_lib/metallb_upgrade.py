"""Run the stop conditions of a MetalLB pin bump in order, exit code naming the first failure.

After H1, merging `k3s_metallb_version` is the upgrade, so `land.sh` runs these gates before
it merges such a bump (docs/target-architecture/host/h1-cluster-config-to-flux.md, *Longhorn
and MetalLB bumps gate at the merge*). MetalLB owns the LoadBalancer VIPs, and its speakers
announce them from every node, so the bump must land on a cluster that is whole and on a
deployer that is not already holding a failed change. Both gates are `k3s_upgrade.py`'s own
verdicts, reused rather than restated:

  1. The GitOps deployer holds no SHA. A non-empty `hold_sha` means a previous deploy failed
     its health gate, and the upgrade would land on top of that.
  2. Every production node is Ready. A speaker on a NotReady node cannot announce, so a VIP
     that fails over during the roll has nowhere to go.

The hold marker lives in the deployer's state directory on the host that runs the tick, so
gate 1 fails on any other host rather than reading an absent directory as "no hold". Set
`GITOPS_STATE_DIR` to point it elsewhere.

Exit codes:
  0      every gate passed
  1..2   the first gate that failed, by its number above
  69     the cluster could not be asked, or a deployer marker exists and could not be read

Usage:
    uv run python scripts/deploy_tools/runbook_gates.py metallb-upgrade
"""

import os
import sys
from pathlib import Path as _Path

# Reach `scripts/`, for `lib` and `deploy_tools`: the entrypoint puts only its own directory on
# sys.path, and pyproject's `pythonpath` is a pytest setting.
sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

from deploy_tools.runbook_gates_lib import gate_runner
from deploy_tools.runbook_gates_lib.gate_runner import Gate, cluster_doc
from deploy_tools.runbook_gates_lib.k3s_upgrade import (
    CLUSTER,
    NODES_ARGS,
    held_sha,
    nodes_not_ready,
)
from lib.kubectl import DEFAULT_TOOLS, Tools
from lib.repo_paths import GITOPS_DEPLOY_FILES

# The deployer's own marker module, read from its role's files/ rather than a copy (#3275).
sys.path.insert(0, str(GITOPS_DEPLOY_FILES))
from gitops_markers import STATE_DIR

# No MetalLB runbook exists; the plan section that defines these gates is the one to read.
RUNBOOK = "docs/target-architecture/host/h1-cluster-config-to-flux.md"


def _gate_hold(tools: Tools, state_dir: str) -> list[str]:
    return held_sha(state_dir)


def _gate_nodes(tools: Tools, state_dir: str) -> list[str]:
    return nodes_not_ready(cluster_doc(CLUSTER, tools, NODES_ARGS))


# Order is the gate number. Append; never reorder.
GATES = (
    Gate(1, "the GitOps deployer holds no SHA", _gate_hold),
    Gate(2, "every production node Ready", _gate_nodes),
)


def run_gates(
    tools: Tools = DEFAULT_TOOLS,
    state_dir: str | None = None,
    out=sys.stdout,
) -> int:
    """Run every gate in order, print one line per gate, and return the exit code."""
    state_dir = state_dir or os.environ.get("GITOPS_STATE_DIR") or STATE_DIR
    return gate_runner.run_gates(GATES, RUNBOOK, tools, state_dir, out=out)


def main(argv: list[str] | None = None) -> int:
    return gate_runner.cli(__doc__, argv, run_gates)
