#!/usr/bin/env python3
"""Run the stop conditions of `docs/longhorn-upgrade.md` in order, exit code naming the first failure.

Longhorn supports no downgrade, so the backup is the whole safety net and the runbook's gate
section is what proves the net is there. The section listed its checks as shell blocks the
operator ran by hand; here each is a verdict over what the cluster or the drill answered, the
runner stops at the first failure, and the exit code is the gate number (#2216, the shape
`k3s_upgrade_gates.py` set in #2162).

The gates, in the order the runbook gives them:

  1. The backup target is armed and reachable. `default` must carry a URL, and every target
     that carries one must report `available`. An armed target that is not available means the
     backups it holds cannot be listed, let alone restored.
  2. A restore has actually succeeded, recently. A green backup job is not proof — on
     2026-08-15 a B2 cap denial read as "cannot find volume.cfg in backupstore". The nightly
     restore drill writes `/var/lib/longhorn-restore-drill/last-success` only after its data
     assertions pass; the stamp must be younger than `k3s_longhorn_restore_drill_max_age_days`
     (the k3s role's defaults — monitor-bridge's check 7 uses the same number).
  3. Every volume is accounted for: attached or detached, and healthy (an idle volume reports
     `unknown`). A volume mid-attach, `degraded` or `faulted` before the hop cannot be told
     apart from upgrade damage after it.
  4. The engines run on one image. `concurrent-automatic-engine-upgrade-per-node-limit` is 0,
     so engines do not follow the manager; a previous hop's image still holding references
     means engine lag that compounds across hops. Every engine image must be `deployed`.

Every cluster read goes through `lib.kubectl` with the cluster named `prod` (#1663). The drill
stamp exists only on daniel-box, the host that runs the drill, so gate 2 fails on any other
host rather than reading an absent directory as a fresh pass. Set `LONGHORN_DRILL_STATE_DIR`
to point it elsewhere.

Exit codes:
  0      every gate passed
  1..4   the first gate that failed, by its number above
  69     the cluster could not be asked (no kubectl, no readable kubeconfig, wrong cluster,
         or a list that returned nothing parseable)

Usage:
    uv run python scripts/deploy_tools/longhorn_upgrade_gates.py
"""

import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path as _Path

# Reach `lib`: a directly-invoked script gets only its own directory on sys.path, and
# pyproject's `pythonpath` is a pytest setting.
sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

from deploy_tools import runbook_gates
from deploy_tools.runbook_gates import (
    LONGHORN_NS,
    TARGETS_ARGS,
    VOLUMES_ARGS,
    Gate,
    cluster_doc,
    items,
    name_of,
    stamp_dir_missing,
    unreachable_targets,
    unsafe_volumes,
)
from lib import yaml_fast
from lib.kubectl import DEFAULT_TOOLS, Tools
from lib.repo_paths import ROLES

CLUSTER = "prod"
RUNBOOK = "docs/longhorn-upgrade.md"

DRILL_STATE_DIR = "/var/lib/longhorn-restore-drill"
DRILL_STAMP = "last-success"
K3S_DEFAULTS = ROLES / "setup" / "k3s" / "defaults" / "main.yml"
MAX_AGE_KEY = "k3s_longhorn_restore_drill_max_age_days"

# The target the runbook's own block names. Others (`r2`) are checked when armed.
REQUIRED_TARGETS = ("default",)

# A volume is settled in exactly these two states; `attaching`, `detaching`, `creating` and
# `deleting` are all mid-flight.
SETTLED_STATES = frozenset({"attached", "detached"})

ENGINE_IMAGES_ARGS = (
    "-n",
    LONGHORN_NS,
    "get",
    "engineimages.longhorn.io",
    "-o",
    "json",
)


# ── the gates, as pure verdicts over what the cluster or the tree answered ──────────────────


def restore_drill_max_age_s(defaults_file: _Path = K3S_DEFAULTS) -> float:
    """The drill's freshness window in seconds, read from the k3s role's defaults."""
    defaults = yaml_fast.safe_load(defaults_file.read_text())
    return float(defaults[MAX_AGE_KEY]) * 86400


def stale_restore_drill(
    state_dir: str | os.PathLike, max_age_s: float, now: float | None = None
) -> list[str]:
    """Why the drill stamp does not prove a recent restore, or nothing when it does.

    The stamp is one unix epoch. Absent, unreadable or unparseable is a failure, not a skip:
    a drill that has never passed is the state most worth reporting.
    """
    missing = stamp_dir_missing(state_dir, "daniel-box, the host that runs the drill")
    if missing:
        return missing
    stamp = _Path(state_dir) / DRILL_STAMP
    try:
        epoch = float(stamp.read_text().strip())
    except FileNotFoundError:
        return [f"<{stamp} does not exist — no restore drill has ever passed here>"]
    except (OSError, ValueError) as exc:
        return [f"<cannot read an epoch from {stamp}: {exc}>"]
    age_s = (time.time() if now is None else now) - epoch
    if age_s > max_age_s:
        return [
            f"last restore drill passed {age_s / 86400:.1f} days ago "
            f"(window {max_age_s / 86400:.0f} days)"
        ]
    return []


def unaccounted_volumes(doc) -> list[str]:
    """`name (state)` for a volume mid-flight, plus `unsafe_volumes` for the robustness."""
    if doc is None:
        return ["<could not list volumes.longhorn.io>"]
    found = []
    for item in items(doc):
        state = str((item.get("status") or {}).get("state", ""))
        if state not in SETTLED_STATES:
            found.append(f"{name_of(item)} (state={state or 'none reported'})")
    return found + unsafe_volumes(doc)


def lagging_engine_images(doc) -> list[str]:
    """Engine images not `deployed`, and every referenced image when more than one is."""
    if doc is None:
        return ["<could not list engineimages.longhorn.io>"]
    found = []
    referenced = []
    for item in items(doc):
        status = item.get("status") or {}
        image = str((item.get("spec") or {}).get("image") or name_of(item))
        state = str(status.get("state", ""))
        if state != "deployed":
            found.append(f"{image} (state={state or 'none reported'})")
        refs = int(status.get("refCount") or 0)
        if refs > 0:
            referenced.append(f"{image} ({refs} references)")
    if len(referenced) > 1:
        found.extend(referenced)
    return found


# ── the runner ──────────────────────────────────────────────────────────────────────────────


def _doc(tools: Tools, args: tuple[str, ...]):
    return cluster_doc(CLUSTER, tools, args)


@dataclass(frozen=True)
class Env:
    """What the gates read besides the cluster; a test hands the runner one built on fakes.

    Attributes:
        tools: the kubectl boundary.
        state_dir: the drill's state directory.
        max_age_s: the drill's freshness window in seconds.
        now: the clock, as a unix epoch.
    """

    tools: Tools
    state_dir: str
    max_age_s: float
    now: float


def _gate_target(env: Env) -> list[str]:
    return unreachable_targets(_doc(env.tools, TARGETS_ARGS), REQUIRED_TARGETS)


def _gate_drill(env: Env) -> list[str]:
    return stale_restore_drill(env.state_dir, env.max_age_s, env.now)


def _gate_volumes(env: Env) -> list[str]:
    return unaccounted_volumes(_doc(env.tools, VOLUMES_ARGS))


def _gate_engines(env: Env) -> list[str]:
    return lagging_engine_images(_doc(env.tools, ENGINE_IMAGES_ARGS))


# Order is the runbook's, and the exit code is the position. Append; never reorder.
GATES = (
    Gate(1, "the backup target is armed and available", _gate_target),
    Gate(2, "a restore drill passed recently", _gate_drill),
    Gate(3, "every volume settled and healthy", _gate_volumes),
    Gate(4, "every engine on one deployed image", _gate_engines),
)


def run_gates(
    tools: Tools = DEFAULT_TOOLS,
    state_dir: str | None = None,
    max_age_s: float | None = None,
    now: float | None = None,
    out=sys.stdout,
) -> int:
    """Run every gate in order, print one line per gate, and return the exit code."""
    env = Env(
        tools=tools,
        state_dir=state_dir
        or os.environ.get("LONGHORN_DRILL_STATE_DIR")
        or DRILL_STATE_DIR,
        max_age_s=restore_drill_max_age_s() if max_age_s is None else max_age_s,
        now=time.time() if now is None else now,
    )
    return runbook_gates.run_gates(GATES, RUNBOOK, env, out=out)


def main(argv: list[str] | None = None) -> int:
    return runbook_gates.cli(__doc__, argv, run_gates)


if __name__ == "__main__":
    sys.exit(main())
