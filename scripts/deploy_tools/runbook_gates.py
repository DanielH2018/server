"""The runner and the shared verdicts behind every `scripts/deploy_tools/*_gates.py`.

A runbook whose safety is step order lists its stop conditions as gates, and each runbook's
gates run as one script (`k3s_upgrade_gates.py` first, #2162; the other four, #2216). Every
one of those scripts has the same shape, and this module is that shape written once:

  * A gate is a pure verdict function over what the cluster or the tree answered — it returns
    the offenders, and an empty list is a pass. Nothing in a verdict runs a process.
  * The runner calls the gates in the runbook's order, prints one line per gate, stops at the
    first failure, and returns the gate's NUMBER as the exit code. `EX_UNAVAILABLE` (69) is
    "could not look": no kubectl, no readable kubeconfig, the wrong cluster, or a list that
    returned nothing parseable. That is not a graded verdict, so it is not a gate number.
  * Every cluster read goes through `lib.kubectl` naming the cluster, so the identity check
    that guards `probe.py health` refuses a staging kubectl here too (#1663).
  * A state directory that does not exist means "wrong host", never "clean": the deployer's
    hold marker and the drills' stamps live on the host that runs them, and reading an absent
    directory as a pass is the vacuous pass the gate exists to refuse (`stamp_dir_missing`).

The Longhorn volume verdict lives here because two runbooks share it — a degraded volume stops
a k3s upgrade and a Longhorn hop alike. A verdict one runbook owns stays in that runbook's
script.
"""

import os
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path as _Path

# Reach `lib`: a directly-invoked script gets only its own directory on sys.path, and
# pyproject's `pythonpath` is a pytest setting.
sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

from lib.kubectl import MissingKubectl, Tools, WrongCluster, kubectl_json

EX_UNAVAILABLE = 69
EX_USAGE = 64

# Allow-lists, not deny-lists: a Longhorn state this file has not heard of (a rename, a new
# value after a Longhorn bump) must stop the procedure, not pass it. `healthy` is a volume with
# every replica; `unknown` is what an idle, detached volume reports. `degraded` and `faulted`
# are the ones the runbooks name.
SAFE_ROBUSTNESS = frozenset({"healthy", "unknown"})

LONGHORN_NS = "longhorn-system"
VOLUMES_ARGS = ("-n", LONGHORN_NS, "get", "volumes.longhorn.io", "-o", "json")
TARGETS_ARGS = ("-n", LONGHORN_NS, "get", "backuptargets.longhorn.io", "-o", "json")


def items(doc) -> list[dict]:
    """The `items` of a kubectl list document, tolerating None and a missing key."""
    return list((doc or {}).get("items") or [])


def name_of(item: dict) -> str:
    return str((item.get("metadata") or {}).get("name", "?"))


def unsafe_volumes(doc) -> list[str]:
    """`name (robustness)` for every volume whose robustness is not in `SAFE_ROBUSTNESS`."""
    if doc is None:
        return ["<could not list volumes.longhorn.io>"]
    found = []
    for item in items(doc):
        robustness = str((item.get("status") or {}).get("robustness", ""))
        if robustness not in SAFE_ROBUSTNESS:
            found.append(f"{name_of(item)} ({robustness or 'no robustness reported'})")
    return found


def unreachable_targets(doc, required: Sequence[str]) -> list[str]:
    """Backup targets in `required` that are absent or unarmed, and armed ones not `available`.

    An armed target that is not available means the backups it holds cannot be listed, let
    alone restored. A disarmed target outside `required` is not an offender: disarming is the
    containment lever for a B2 cap spiral, and a runbook that does not need that target must
    not refuse on it.
    """
    if doc is None:
        return ["<could not list backuptargets.longhorn.io>"]
    seen: dict[str, dict] = {name_of(item): item for item in items(doc)}
    found = [f"{name} (no such backup target)" for name in required if name not in seen]
    for name, item in sorted(seen.items()):
        url = str((item.get("spec") or {}).get("backupTargetURL") or "")
        available = (item.get("status") or {}).get("available")
        if not url:
            if name in required:
                found.append(f"{name} (disarmed: backupTargetURL is empty)")
            continue
        if available is not True:
            found.append(f"{name} (armed, available={available})")
    return found


def stamp_dir_missing(state_dir: str | os.PathLike, host_hint: str) -> list[str]:
    """The one offender an absent state directory produces, or an empty list when it exists."""
    state_dir = _Path(state_dir)
    if state_dir.is_dir():
        return []
    return [f"<{state_dir} is not a directory — run this on {host_hint}>"]


# ── the runner ──────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Gate:
    number: int
    title: str
    check: Callable[..., list[str]]


class Unreadable(RuntimeError):
    """A cluster list returned nothing parseable — the gate could not look, so it is not graded."""


def cluster_doc(cluster: str, tools: Tools, args: Sequence[str]):
    """`kubectl_json` that raises `Unreadable` instead of returning None."""
    doc = kubectl_json(cluster, *args, tools=tools)
    if doc is None:
        raise Unreadable(f"`kubectl {' '.join(args)}` returned no document")
    return doc


def run_gates(gates: Sequence[Gate], runbook: str, *context, out=sys.stdout) -> int:
    """Run `gates` in order with `context` as each check's arguments; return the exit code.

    Prints one line per gate. The first failing gate's offenders are printed under it, and its
    number is the return value; `EX_UNAVAILABLE` when a gate could not ask the cluster.
    """
    for gate in gates:
        try:
            offenders = gate.check(*context)
        except (WrongCluster, MissingKubectl, Unreadable) as exc:
            print(
                f"gate {gate.number} ({gate.title}): cannot ask the cluster — {exc}",
                file=out,
            )
            return EX_UNAVAILABLE
        if offenders:
            print(f"gate {gate.number} FAILED — {gate.title}:", file=out)
            for offender in offenders:
                print(f"  {offender}", file=out)
            print(f"stop: gate {gate.number} is a stop condition ({runbook})", file=out)
            return gate.number
        print(f"gate {gate.number} ok — {gate.title}", file=out)
    print(f"all {len(gates)} gates passed", file=out)
    return 0


def cli(
    doc: str, argv: list[str] | None, run: Callable[..., int], *, takes: int = 0
) -> int:
    """The entry point every gate script shares: the wrong argument count prints `doc`.

    `takes` is how many positional arguments the runbook's own invocation carries — 0 for a
    script whose gates read only the host and the cluster, 1 for the etcd restore gates,
    where the snapshot name is the subject gate 3 grades. An argument starting with `-` is a
    flag, and no gate script has one, so it is usage rather than a positional.
    """
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != takes or any(arg.startswith("-") for arg in argv):
        print(doc, file=sys.stderr)
        return EX_USAGE
    return run(*argv)
