"""Run the four stop conditions of `docs/k3s-upgrade.md` in order, exit code naming the first failure.

The runbook listed the gates as four shell blocks the operator ran by hand, each "a stop
condition, not a checklist item". Nothing enforced the order or the stop: a skipped block was
a paragraph nobody read. Here each gate is a function over what the cluster answered,
the runner stops at the first failure, and the exit code IS the gate number, so a caller —
or a transcript — can tell which condition refused without parsing the message.

The gates, in the order the runbook gives them:

  1. No Longhorn volume is `degraded` (or `faulted`). A degraded volume plus a node restart is
     how the last good replica goes. `detached`/`unknown` is an idle volume, not a stop.
  2. No Longhorn backup is mid-flight. A restart aborts it and the retry storm follows.
  3. `DEPLOY_HOLD` reads `none`. A hold means a previous promotion failed its health gate,
     and the upgrade would land on top of that.
  4. Every production node is Ready.

Every cluster read goes through `lib.kubectl` with the cluster named `prod`, so the same
identity check that guards `probe.py health` refuses a staging kubectl here. Gate 3 reads the
`DEPLOY_HOLD` repository variable through the forge, as the landing pin gate does. A missing
or empty variable fails the gate rather than reading as no hold.

Exit codes:
  0      every gate passed
  1..4   the first gate that failed, by its number above
  69     the cluster could not be asked (no kubectl, no readable kubeconfig, wrong cluster,
         or a list that returned nothing parseable), or `DEPLOY_HOLD` could not be read

Usage:
    uv run python scripts/deploy_tools/runbook_gates.py k3s-upgrade
"""

import sys
from collections.abc import Callable
from pathlib import Path as _Path

# Reach `scripts/`, for `lib` and `deploy_tools`: the entrypoint puts only its own directory on
# sys.path, and pyproject's `pythonpath` is a pytest setting.
sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

from deploy_tools.runbook_gates_lib import gate_runner
from deploy_tools.runbook_gates_lib.gate_runner import (
    EX_UNAVAILABLE,
    LONGHORN_NS,
    VOLUMES_ARGS,
    Gate,
    Unreadable,
    cluster_doc,
    items as _items,
    name_of as _name,
    unsafe_volumes,
)
from deploy_tools.land_lib.pin_gate import hold_problem
from deploy_tools.land_lib.pin_reads import PinReads
from deploy_tools.land_lib.pin_reads import Unreadable as HoldUnreadable
from lib.kubectl import CLUSTER_NODES, DEFAULT_TOOLS, Tools
from lib.repo_paths import HOST_LIB_FILES

# The Backup CR reader the backup-health cron ships (#3735); it imports host_lib.
sys.path.insert(0, str(HOST_LIB_FILES))
sys.path.insert(0, str(_Path(__file__).resolve().parents[2] / "lib"))
from longhorn_lib import longhorn_backups

CLUSTER = "prod"
RUNBOOK = "docs/k3s-upgrade.md"

# Backup states a restart cannot abort. `New`, `Pending` and `InProgress` are in flight; the
# empty string is a Backup CR the controller has not picked up yet.
SETTLED_BACKUP = frozenset({longhorn_backups.COMPLETED, "Error", "Unknown"})

LIVE_HOLD = PinReads().hold
BACKUPS_ARGS = ("-n", LONGHORN_NS, *longhorn_backups.LIST_ARGS)
NODES_ARGS = ("get", "nodes", "-o", "json")

# Re-exported for the test module and for anyone reading this script as the runbook's API.
__all__ = ["EX_UNAVAILABLE", "Unreadable", "unsafe_volumes"]


# ── the gates, as pure verdicts over what kubectl answered ──────────────────────────────────
#
# Each returns the list of offenders — empty means the gate passes. `None` from kubectl_json
# (a failed read) is an offender too, so a direct call can never pass on a read that failed;
# the runner maps that case to `EX_UNAVAILABLE` before it gets here. Gate 1's verdict,
# `unsafe_volumes`, is the shared one in `gate_runner`.


def in_flight_backups(doc) -> list[str]:
    """`name (state)` for every backup whose state is not in `SETTLED_BACKUP`."""
    if doc is None:
        return [f"<could not list {longhorn_backups.RESOURCE}>"]
    found = []
    for backup in longhorn_backups.from_items(_items(doc)):
        if backup.state not in SETTLED_BACKUP:
            found.append(f"{backup.name} ({backup.state or 'no state yet'})")
    return found


def deploy_hold(read: Callable[[], str | None]) -> list[str]:
    """`DEPLOY_HOLD`'s problem as one offender, or nothing when it reads `none`.

    `hold_problem` owns what each value means, so this gate and the landing pin gate agree.

    Raises:
        Unreadable: the variable could not be read. The runner then exits `EX_UNAVAILABLE`
            rather than this gate's number, which would claim a hold is set (#4468).
    """
    try:
        value = read()
    except HoldUnreadable as exc:
        raise Unreadable(str(exc), cluster=False) from exc
    problem = hold_problem(value)
    return [problem] if problem else []


def nodes_not_ready(doc, expected=CLUSTER_NODES[CLUSTER]) -> list[str]:
    """`name (reason)` for every expected node that is missing or whose Ready condition is not True."""
    if doc is None:
        return ["<could not list nodes>"]
    seen: dict[str, str] = {}
    for item in _items(doc):
        conditions = (item.get("status") or {}).get("conditions") or []
        ready = next((c for c in conditions if c.get("type") == "Ready"), None)
        seen[_name(item)] = str((ready or {}).get("status", "Unknown"))
    found = []
    for name in sorted(expected):
        status = seen.get(name)
        if status is None:
            found.append(f"{name} (not in the cluster)")
        elif status != "True":
            found.append(f"{name} (Ready={status})")
    return found


# ── the runner ──────────────────────────────────────────────────────────────────────────────


def _doc(tools: Tools, args: tuple[str, ...]):
    return cluster_doc(CLUSTER, tools, args)


def _gate_volumes(tools: Tools, read_hold: Callable[[], str | None]) -> list[str]:
    return unsafe_volumes(_doc(tools, VOLUMES_ARGS))


def _gate_backups(tools: Tools, read_hold: Callable[[], str | None]) -> list[str]:
    return in_flight_backups(_doc(tools, BACKUPS_ARGS))


def _gate_hold(tools: Tools, read_hold: Callable[[], str | None]) -> list[str]:
    return deploy_hold(read_hold)


def _gate_nodes(tools: Tools, read_hold: Callable[[], str | None]) -> list[str]:
    return nodes_not_ready(_doc(tools, NODES_ARGS))


# Order is the runbook's, and the exit code is the position. Append; never reorder.
GATES = (
    Gate(1, "no degraded Longhorn volume", _gate_volumes),
    Gate(2, "no Longhorn backup in flight", _gate_backups),
    Gate(3, "DEPLOY_HOLD reads none", _gate_hold),
    Gate(4, "every production node Ready", _gate_nodes),
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
