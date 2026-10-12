"""Which claimed issues a still-running fan-out batch is working, read from the run manifests.

`findings.py` judges a claim by the worktree it names. A batch in this repo is claimed under
the orchestrator's worktree, which carries no commits and stays clean, so once the
orchestrator's session dies `classify` calls that worktree REMOVABLE and every claim it holds
reads stale. The batches outlive it, because each runs as its own systemd user unit, and `next`
would offer their issues to a second session until the batch opened its PR (#4601). This module
answers the question the worktree cannot: is a batch that names this issue still running?

A batch's unit runs on the host the manifest names, which may be a peer. A host that cannot be
read answers "cannot tell", and the caller holds the claim: releasing it on a failed read hands
live work to a second session, the same fail-safe direction `claim_is_live` takes for an
unreadable worktree.
"""

import subprocess
from collections.abc import Callable, Collection
from pathlib import Path

# Reach the sibling package: a directly-invoked script gets only its own directory on
# sys.path, and pyproject's `pythonpath` is a pytest setting.
import sys as _sys

_sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fanout_lib.manifest import MANIFEST_DIR, Batch, all_runs
from fanout_lib.target import SERVER
from fanout_lib.transport import run_command

# Measured 2026-10-12 from daniel-box: `systemctl --user is-active` answers in 0.28-0.52s over
# ssh to daniel-server and in 0.01-0.02s locally. The ssh leg's own ConnectTimeout is 8s, so
# this bounds a host that accepts the connection and then hangs.
IS_ACTIVE_TIMEOUT_S = 10.0
# `systemctl is-active` states for a unit whose agent is still running or still stopping.
# Everything else it prints for a finished batch is `inactive` or `failed`; any other word is
# read as "cannot tell".
_RUNNING = frozenset({"active", "activating", "reloading", "deactivating"})
_FINISHED = frozenset({"inactive", "failed"})


def _host_states(
    host: str, units: list[str], run: Callable[..., subprocess.CompletedProcess]
) -> list[str] | str:
    """Each unit's `is-active` state, in order, or the reason the host could not be read.

    One call per host covers every unit on it, because `ufw limit ssh` rejects a sixth
    connection inside 30 seconds. `is-active` prints one line per unit and exits non-zero when
    any of them is not active, so the exit code says nothing and the line count decides. An
    empty stdout is a failed read, never "every unit is inactive": the local leg without a user
    bus prints nothing to stdout and exits 1.
    """
    try:
        done = run(
            host, f"systemctl --user is-active {' '.join(units)}", IS_ACTIVE_TIMEOUT_S
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return f"{type(exc).__name__}: {exc}"
    if done.returncode == 255:
        return f"ssh failed: {done.stderr.strip() or 'exit 255'}"
    states = done.stdout.split()
    if len(states) != len(units):
        return (
            f"expected {len(units)} unit state(s), got {len(states)}"
            f"{': ' + done.stderr.strip() if done.stderr.strip() else ''}"
        )
    return states


def running_batch_holds(
    repo: str | None,
    issues: Collection[int],
    root: Path = MANIFEST_DIR,
    run: Callable[..., subprocess.CompletedProcess] = run_command,
) -> dict[int, str]:
    """Each of ``issues`` a standing batch in ``repo`` names, mapped to why its claim is held.

    A batch is consulted only when it has no `removed_at`, works ``repo``, and names one of
    ``issues``, so a call with no stale claims reads files and runs nothing. An issue whose
    batch unit has finished is left out, and its claim stays stale.

    Args:
        repo: the register the claims are in, as `OWNER/NAME`, or None for this repo's.
        issues: the issue numbers whose claims read stale by their worktree.
        root: the manifest directory.
        run: `fanout_lib.transport.run_command`-shaped, `(host, command, timeout)`. A test
            seam, so no test reads a real unit or opens an ssh connection.

    Returns:
        `{issue: reason}`, the reason in the words a `claims` row prints: the running batch,
        or "cannot tell" with what failed.
    """
    repo = repo or SERVER
    wanted = set(issues)
    by_host: dict[str, list[Batch]] = {}
    for manifest in all_runs(root):
        for b in manifest.batches:
            if not b.removed_at and b.repo == repo and wanted & set(b.issues):
                by_host.setdefault(b.host, []).append(b)
    holds: dict[int, str] = {}
    for host, batches in by_host.items():
        states = _host_states(host, [b.unit for b in batches], run)
        for i, b in enumerate(batches):
            if isinstance(states, str):
                reason = (
                    f"cannot tell whether fan-out batch {b.batch} on {host} is running "
                    f"({states}); held"
                )
            elif states[i] in _RUNNING:
                reason = (
                    f"fan-out batch {b.batch} is still running ({b.unit} on {host})"
                )
            elif states[i] in _FINISHED:
                continue
            else:
                reason = (
                    f"cannot tell whether fan-out batch {b.batch} on {host} is running "
                    f"(unit state `{states[i]}`); held"
                )
            for n in wanted & set(b.issues):
                holds.setdefault(n, reason)
    return holds
