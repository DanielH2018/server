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
  69     the cluster could not be asked (no kubectl, no readable kubeconfig, wrong cluster)

Usage:
    uv run python scripts/deploy_tools/k3s_upgrade_gates.py
"""

import os
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path as _Path

# Reach `lib`: a directly-invoked script gets only its own directory on sys.path, and
# pyproject's `pythonpath` is a pytest setting.
sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

from lib.gitops_markers import MARKERS, STATE_DIR
from lib.kubectl import (
    CLUSTER_NODES,
    DEFAULT_TOOLS,
    MissingKubectl,
    Tools,
    WrongCluster,
    kubectl_json,
)

CLUSTER = "prod"
EX_UNAVAILABLE = 69

# Longhorn's volume robustness values that mean a replica is already gone or going. `unknown`
# is what an idle, detached volume reports and is deliberately not here.
UNSAFE_ROBUSTNESS = frozenset({"degraded", "faulted"})

# Longhorn backup states a restart would abort. `Completed`, `Error` and `Unknown` are
# settled; the empty string is a Backup CR the controller has not picked up yet.
IN_FLIGHT_BACKUP = frozenset({"", "New", "Pending", "InProgress"})

VOLUMES_ARGS = ("-n", "longhorn-system", "get", "volumes.longhorn.io", "-o", "json")
BACKUPS_ARGS = ("-n", "longhorn-system", "get", "backups.longhorn.io", "-o", "json")
NODES_ARGS = ("get", "nodes", "-o", "json")


def _items(doc) -> list[dict]:
    return list((doc or {}).get("items") or [])


def _name(item: dict) -> str:
    return str((item.get("metadata") or {}).get("name", "?"))


# ── the gates, as pure verdicts over what kubectl answered ──────────────────────────────────
#
# Each returns the list of offenders — empty means the gate passes. `None` from kubectl_json
# (a failed read) is an offender too: a gate that cannot see its subject must not pass.


def unsafe_volumes(doc) -> list[str]:
    """`name (robustness)` for every volume whose robustness is in `UNSAFE_ROBUSTNESS`."""
    if doc is None:
        return ["<could not list volumes.longhorn.io>"]
    found = []
    for item in _items(doc):
        robustness = str((item.get("status") or {}).get("robustness", ""))
        if robustness in UNSAFE_ROBUSTNESS:
            found.append(f"{_name(item)} ({robustness})")
    return found


def in_flight_backups(doc) -> list[str]:
    """`name (state)` for every backup whose state is in `IN_FLIGHT_BACKUP`."""
    if doc is None:
        return ["<could not list backups.longhorn.io>"]
    found = []
    for item in _items(doc):
        state = str((item.get("status") or {}).get("state", ""))
        if state in IN_FLIGHT_BACKUP:
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


@dataclass(frozen=True)
class Gate:
    number: int
    title: str
    check: Callable[[Tools, str], list[str]]


def _gate_volumes(tools: Tools, state_dir: str) -> list[str]:
    return unsafe_volumes(kubectl_json(CLUSTER, *VOLUMES_ARGS, tools=tools))


def _gate_backups(tools: Tools, state_dir: str) -> list[str]:
    return in_flight_backups(kubectl_json(CLUSTER, *BACKUPS_ARGS, tools=tools))


def _gate_hold(tools: Tools, state_dir: str) -> list[str]:
    return held_sha(state_dir)


def _gate_nodes(tools: Tools, state_dir: str) -> list[str]:
    return nodes_not_ready(kubectl_json(CLUSTER, *NODES_ARGS, tools=tools))


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
    for gate in GATES:
        try:
            offenders = gate.check(tools, state_dir)
        except (WrongCluster, MissingKubectl) as exc:
            print(
                f"gate {gate.number} ({gate.title}): cannot ask the cluster — {exc}",
                file=out,
            )
            return EX_UNAVAILABLE
        if offenders:
            print(f"gate {gate.number} FAILED — {gate.title}:", file=out)
            for offender in offenders:
                print(f"  {offender}", file=out)
            print(
                f"stop: gate {gate.number} is a stop condition (docs/k3s-upgrade.md)",
                file=out,
            )
            return gate.number
        print(f"gate {gate.number} ok — {gate.title}", file=out)
    print("all 4 gates passed", file=out)
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv:
        print(__doc__, file=sys.stderr)
        return 64
    return run_gates()


if __name__ == "__main__":
    sys.exit(main())
