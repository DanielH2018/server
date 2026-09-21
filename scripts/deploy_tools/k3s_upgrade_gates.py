#!/usr/bin/env python3
"""Run the four stop conditions of `docs/k3s-upgrade.md` in order, exit code naming the first failure.

The runbook listed the gates as four shell blocks the operator ran by hand, each "a stop
condition, not a checklist item". Nothing enforced the order or the stop: a skipped block was
a paragraph nobody read (#2162). Here each gate is a function over what the cluster answered,
the runner stops at the first failure, and the exit code IS the gate number, so a caller —
or a transcript — can tell which condition refused without parsing the message.

The gates, in the order the runbook gives them:

  1. No Longhorn volume is `degraded` (or `faulted`). A degraded volume plus a node restart is
     how the last good replica goes. `detached`/`unknown` is an idle volume, not a stop.
  2. No Longhorn backup is mid-flight. A restart aborts it and the retry storm follows.
  3. The GitOps deployer holds no SHA. A non-empty `hold_sha` means a previous deploy failed
     its health gate, and the upgrade would land on top of that.
  4. Every production node is Ready.

Every cluster read goes through `lib.kubectl` with the cluster named `prod`, so the same
identity check that guards `probe.py health` refuses a staging kubectl here (#1663). The
hold marker is read from the deployer's own state directory (`lib.gitops_markers`), which
exists only on the host that runs the tick — so gate 3 fails on any other host rather than
reading an absent directory as "no hold". Set `GITOPS_STATE_DIR` to point it elsewhere.

Exit codes:
  0      every gate passed
  1..4   the first gate that failed, by its number above
  69     the cluster could not be asked (no kubectl, no readable kubeconfig, wrong cluster,
         or a list that returned nothing parseable)

Usage:
    uv run python scripts/deploy_tools/k3s_upgrade_gates.py
"""

import os
import sys
from pathlib import Path as _Path

# Reach `lib`: a directly-invoked script gets only its own directory on sys.path, and
# pyproject's `pythonpath` is a pytest setting.
sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

from deploy_tools import runbook_gates
from deploy_tools.runbook_gates import (
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
from lib.gitops_markers import MARKERS, STATE_DIR
from lib.kubectl import CLUSTER_NODES, DEFAULT_TOOLS, Tools

CLUSTER = "prod"
RUNBOOK = "docs/k3s-upgrade.md"

# Backup states a restart cannot abort. `New`, `Pending` and `InProgress` are in flight; the
# empty string is a Backup CR the controller has not picked up yet.
SETTLED_BACKUP = frozenset({"Completed", "Error", "Unknown"})

BACKUPS_ARGS = ("-n", LONGHORN_NS, "get", "backups.longhorn.io", "-o", "json")
NODES_ARGS = ("get", "nodes", "-o", "json")

# Re-exported for the test module and for anyone reading this script as the runbook's API.
__all__ = ["EX_UNAVAILABLE", "Unreadable", "unsafe_volumes"]


# ── the gates, as pure verdicts over what kubectl answered ──────────────────────────────────
#
# Each returns the list of offenders — empty means the gate passes. `None` from kubectl_json
# (a failed read) is an offender too, so a direct call can never pass on a read that failed;
# the runner maps that case to `EX_UNAVAILABLE` before it gets here. Gate 1's verdict,
# `unsafe_volumes`, is the shared one in `runbook_gates`.


def in_flight_backups(doc) -> list[str]:
    """`name (state)` for every backup whose state is not in `SETTLED_BACKUP`."""
    if doc is None:
        return ["<could not list backups.longhorn.io>"]
    found = []
    for item in _items(doc):
        state = str((item.get("status") or {}).get("state", ""))
        if state not in SETTLED_BACKUP:
            found.append(f"{_name(item)} ({state or 'no state yet'})")
    return found


def held_sha(state_dir: str | os.PathLike) -> list[str]:
    """The held SHA (with `hold_plane` when set), or the reason the marker could not be read.

    An absent or empty `hold_sha` is a cleared hold — that is how the deployer clears it. An
    absent state DIRECTORY is not: it means this is not the deploy host, and reading that as
    "no hold" is exactly the vacuous pass the gate exists to refuse.
    """
    state_dir = _Path(state_dir)
    if not state_dir.is_dir():
        return [
            f"<{state_dir} is not a directory — run this on the host that runs the tick>"
        ]
    try:
        sha = (state_dir / MARKERS["hold"]).read_text().strip()
    except FileNotFoundError:
        return []
    except OSError as exc:
        return [f"<cannot read {state_dir / MARKERS['hold']}: {exc}>"]
    if not sha:
        return []
    try:
        plane = (state_dir / MARKERS["hold_plane"]).read_text().strip()
    except OSError:
        plane = ""
    return [f"{sha} ({plane})" if plane else sha]


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


def _gate_volumes(tools: Tools, state_dir: str) -> list[str]:
    return unsafe_volumes(_doc(tools, VOLUMES_ARGS))


def _gate_backups(tools: Tools, state_dir: str) -> list[str]:
    return in_flight_backups(_doc(tools, BACKUPS_ARGS))


def _gate_hold(tools: Tools, state_dir: str) -> list[str]:
    return held_sha(state_dir)


def _gate_nodes(tools: Tools, state_dir: str) -> list[str]:
    return nodes_not_ready(_doc(tools, NODES_ARGS))


# Order is the runbook's, and the exit code is the position. Append; never reorder.
GATES = (
    Gate(1, "no degraded Longhorn volume", _gate_volumes),
    Gate(2, "no Longhorn backup in flight", _gate_backups),
    Gate(3, "the GitOps deployer holds no SHA", _gate_hold),
    Gate(4, "every production node Ready", _gate_nodes),
)


def run_gates(
    tools: Tools = DEFAULT_TOOLS,
    state_dir: str | None = None,
    out=sys.stdout,
) -> int:
    """Run every gate in order, print one line per gate, and return the exit code."""
    state_dir = state_dir or os.environ.get("GITOPS_STATE_DIR") or STATE_DIR
    return runbook_gates.run_gates(GATES, RUNBOOK, tools, state_dir, out=out)


def main(argv: list[str] | None = None) -> int:
    return runbook_gates.cli(__doc__, argv, run_gates)


if __name__ == "__main__":
    sys.exit(main())
