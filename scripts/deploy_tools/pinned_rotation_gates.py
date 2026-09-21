#!/usr/bin/env python3
"""Run the stop conditions of the pinned-secret procedure in `docs/secret-rotation.md`, exit code naming the first failure.

A pinned secret anchors existing data: `authelia_storage` encrypts the TOTP secrets and
WebAuthn credentials in Authelia's SQLite database, and `change-key` re-encrypts that database
IN PLACE, so the instant it succeeds the old key opens nothing and the pre-rotation snapshot is
the only way back. The runbook's discipline is a staged cutover whose safety is the order of
its steps; here the conditions that must hold before `change-key` are verdicts over what the
registry, the cluster and this shell answered, the runner stops at the first failure, and the
exit code is the gate number (#2216, the shape `k3s_upgrade_gates.py` set in #2162).

This script checks the preconditions and nothing more. It never generates, prints or handles
a key, and it is safe to run from anywhere — the `change-key` commands are not, which is what
gate 4 is for.

The gates, in the order the runbook gives them:

  1. The secret is registered `pinned` and is not a `source: record` key. The weekly cron
     rotates `auto`-tier secrets unattended with a bare `sops set`, and that is exactly the
     swap that loses the data — a reclassified row would hand the cron this key.
  2. A snapshot of the anchored data was taken for THIS rotation: a `readyToUse` Longhorn
     snapshot of the `homelab/authelia-config` volume, not from a recurring job, younger than
     `SNAPSHOT_MAX_AGE_S`. The nightly R2 backup leaves a day-old snapshot behind; enrolments
     since then are what a rollback onto it would lose.
  3. Authelia is available. `change-key` runs inside the pod, and the gap between it and the
     redeploy is a real TOTP outage that has to stay short — a pod that is not running yet
     makes the gap open-ended.
  4. This shell is on daniel-box and is not a Claude Code session. The commands need the
     server's kubeconfig, and a key typed into a Claude session is transcribed, which is the
     exposure the runbook exists to prevent.

Every cluster read goes through `lib.kubectl` with the cluster named `prod` (#1663).

Exit codes:
  0      every gate passed
  1..4   the first gate that failed, by its number above
  69     the cluster could not be asked (no kubectl, no readable kubeconfig, wrong cluster,
         or a list that returned nothing parseable)

Usage:
    uv run python scripts/deploy_tools/pinned_rotation_gates.py
"""

import os
import sys
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path as _Path

# Reach `lib`: a directly-invoked script gets only its own directory on sys.path, and
# pyproject's `pythonpath` is a pytest setting.
sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

from deploy_tools import runbook_gates
from deploy_tools.runbook_gates import (
    LONGHORN_NS,
    VOLUMES_ARGS,
    Gate,
    cluster_doc,
    items,
    name_of,
)
from lib import yaml_fast
from lib.kubectl import DEFAULT_TOOLS, K3S_KUBECONFIG, Tools
from lib.repo_paths import ANSIBLE

CLUSTER = "prod"
RUNBOOK = "docs/secret-rotation.md"

SECRET = "authelia_storage"
REGISTRY = ANSIBLE / "secret_rotation.yml"
PVC = ("homelab", "authelia-config")
WORKLOAD = ("homelab", "authelia")
# Two hours: long enough to take the snapshot and read the runbook, short enough that the
# nightly recurring snapshot never satisfies it by accident.
SNAPSHOT_MAX_AGE_S = 2 * 3600
# Set in every shell the Claude Code Bash tool runs.
CLAUDE_SESSION_VAR = "CLAUDECODE"
# The server node's kubeconfig. Only daniel-box has one, and the runbook's `sudo k3s kubectl
# exec` reads it.
SERVER_KUBECONFIG = K3S_KUBECONFIG

SNAPSHOTS_ARGS = ("-n", LONGHORN_NS, "get", "snapshots.longhorn.io", "-o", "json")
DEPLOYMENT_ARGS = ("-n", WORKLOAD[0], "get", "deployment", WORKLOAD[1], "-o", "json")


# ── the gates, as pure verdicts over what the registry, the cluster and the shell answered ──


def not_pinned(registry: Mapping, secret: str = SECRET) -> list[str]:
    """Why the registry row would let a bare `sops set` reach this secret, else nothing."""
    entry = ((registry or {}).get("entries") or {}).get(secret)
    if not isinstance(entry, Mapping):
        return [f"{secret} is not in the registry — run secret_rotation.py sync first"]
    found = []
    tier = str(entry.get("tier", ""))
    if tier != "pinned":
        found.append(f"{secret} is tier {tier or 'unset'}, not pinned")
    if entry.get("source") == "record":
        found.append(
            f"{secret} is a `source: record` key — the app holds the credential"
        )
    return found


def volume_of(volumes_doc, pvc: tuple[str, str] = PVC) -> str | None:
    """The Longhorn volume name bound to `namespace/pvc`, or None."""
    for item in items(volumes_doc):
        k8s = (item.get("status") or {}).get("kubernetesStatus") or {}
        if (k8s.get("namespace"), k8s.get("pvcName")) == pvc:
            return name_of(item)
    return None


def _creation_epoch(snapshot: dict) -> float | None:
    raw = str((snapshot.get("status") or {}).get("creationTime") or "")
    try:
        return datetime.fromisoformat(raw).timestamp()
    except ValueError:
        return None


def no_fresh_snapshot(
    volumes_doc,
    snapshots_doc,
    pvc: tuple[str, str] = PVC,
    now: float | None = None,
    max_age_s: float = SNAPSHOT_MAX_AGE_S,
) -> list[str]:
    """Why no snapshot proves the anchored data is backed up for this rotation, else nothing."""
    if volumes_doc is None:
        return ["<could not list volumes.longhorn.io>"]
    if snapshots_doc is None:
        return ["<could not list snapshots.longhorn.io>"]
    volume = volume_of(volumes_doc, pvc)
    if volume is None:
        return [f"no Longhorn volume is bound to {pvc[0]}/{pvc[1]}"]
    now = time.time() if now is None else now
    newest: float | None = None
    for item in items(snapshots_doc):
        status = item.get("status") or {}
        if (item.get("spec") or {}).get("volume") != volume:
            continue
        if status.get("readyToUse") is not True:
            continue
        if "RecurringJob" in (status.get("labels") or {}):
            continue
        epoch = _creation_epoch(item)
        if epoch is not None and (newest is None or epoch > newest):
            newest = epoch
    if newest is None:
        return [
            f"no ready hand-taken snapshot of {volume} ({pvc[0]}/{pvc[1]}) — "
            "take one in the Longhorn UI first"
        ]
    age_s = now - newest
    if age_s > max_age_s:
        return [
            f"the newest hand-taken snapshot of {pvc[1]} is {age_s / 3600:.1f} h old "
            f"(window {max_age_s / 3600:.0f} h) — take a fresh one"
        ]
    return []


def authelia_unavailable(deployment_doc) -> list[str]:
    """Why the Deployment has no available replica, else nothing."""
    if deployment_doc is None:
        return [f"<could not get deployment {WORKLOAD[0]}/{WORKLOAD[1]}>"]
    available = int((deployment_doc.get("status") or {}).get("availableReplicas") or 0)
    if available < 1:
        return [f"{WORKLOAD[1]} has {available} available replicas"]
    return []


def wrong_shell(environ: Mapping[str, str], server_kubeconfig: _Path) -> list[str]:
    """Why the commands must not run from this shell: not daniel-box, or a Claude session."""
    found = []
    if not server_kubeconfig.exists():
        found.append(
            f"{server_kubeconfig} is not here — run the commands on daniel-box, the server node"
        )
    if environ.get(CLAUDE_SESSION_VAR):
        found.append(
            f"{CLAUDE_SESSION_VAR} is set: this is a Claude Code session, and a key typed "
            "here is transcribed — run the commands from a plain shell"
        )
    return found


# ── the runner ──────────────────────────────────────────────────────────────────────────────


def _doc(tools: Tools, args: tuple[str, ...]):
    return cluster_doc(CLUSTER, tools, args)


@dataclass(frozen=True)
class Env:
    """What the gates read besides the cluster; a test hands the runner one built on fakes.

    Attributes:
        tools: the kubectl boundary.
        registry: the rotation registry file.
        environ: the shell's environment.
        server_kubeconfig: the path only the server node has.
        now: the clock, as a unix epoch.
    """

    tools: Tools
    registry: _Path
    environ: Mapping[str, str]
    server_kubeconfig: _Path
    now: float


def _gate_registry(env: Env) -> list[str]:
    return not_pinned(yaml_fast.safe_load(env.registry.read_text()))


def _gate_snapshot(env: Env) -> list[str]:
    return no_fresh_snapshot(
        _doc(env.tools, VOLUMES_ARGS), _doc(env.tools, SNAPSHOTS_ARGS), now=env.now
    )


def _gate_authelia(env: Env) -> list[str]:
    return authelia_unavailable(_doc(env.tools, DEPLOYMENT_ARGS))


def _gate_shell(env: Env) -> list[str]:
    return wrong_shell(env.environ, env.server_kubeconfig)


# Order is the runbook's, and the exit code is the position. Append; never reorder.
GATES = (
    Gate(1, f"{SECRET} is registered pinned", _gate_registry),
    Gate(2, "a fresh snapshot of the anchored data exists", _gate_snapshot),
    Gate(3, "authelia is available", _gate_authelia),
    Gate(4, "a plain shell on daniel-box", _gate_shell),
)


def run_gates(
    tools: Tools = DEFAULT_TOOLS,
    registry: _Path = REGISTRY,
    environ: Mapping[str, str] | None = None,
    server_kubeconfig: _Path = SERVER_KUBECONFIG,
    now: float | None = None,
    out=sys.stdout,
) -> int:
    """Run every gate in order, print one line per gate, and return the exit code."""
    env = Env(
        tools=tools,
        registry=registry,
        environ=os.environ if environ is None else environ,
        server_kubeconfig=server_kubeconfig,
        now=time.time() if now is None else now,
    )
    return runbook_gates.run_gates(GATES, RUNBOOK, env, out=out)


def main(argv: list[str] | None = None) -> int:
    return runbook_gates.cli(__doc__, argv, run_gates)


if __name__ == "__main__":
    sys.exit(main())
