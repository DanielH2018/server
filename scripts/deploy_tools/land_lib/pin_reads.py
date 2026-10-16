"""The process boundaries the pin gate crosses (`pin_gate.py`), one `PinReads` field each.

`Tools.flux.pins` holds one `PinReads`, so a test replaces one field as it replaces any other
boundary. `last_good` reads the tag with `promote.py`'s own reader, `prod_commit`, so a landing
and a promotion agree on which commit `last-good` names.
"""

import os
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))  # scripts/
# `promote.py` imports `forge` and its siblings bare, from scripts/deploy_tools.
_sys.path.insert(1, str(_Path(__file__).resolve().parents[1]))
from validate import cluster_pins
from validate.cluster_pins import Pin

import forge
from artifact_retention import LAST_GOOD_TAG
from deploy_tools.promote import prod_commit
from deploy_tools.promote_lib.prune import READ_ERRORS

GATES_TIMEOUT_S = 300

_RUNBOOK_GATES = _Path(__file__).resolve().parents[1] / "runbook_gates.py"
_CHECKOUT = _Path(__file__).resolve().parents[3]


class Unreadable(RuntimeError):
    """A read the gate needs failed, so it cannot say the merge is safe."""


def _live_hold() -> str | None:
    try:
        return forge.connect(os.environ).github.variable(forge.HOLD_VARIABLE)
    except READ_ERRORS as exc:
        raise Unreadable(f"{forge.HOLD_VARIABLE}: {exc}") from exc


def _live_last_good() -> str | None:
    try:
        manifest = forge.connect(os.environ).registry.manifest(LAST_GOOD_TAG)
        return prod_commit(manifest)
    except READ_ERRORS as exc:
        raise Unreadable(f"{LAST_GOOD_TAG}: {exc}") from exc


def run_gate_set(name: str) -> tuple[int, str]:
    """`runbook_gates.py <name>` from this checkout; its exit code and combined output.

    A subprocess rather than an import, because each gate module puts role `files/`
    directories on `sys.path` that a landing has no business sharing.
    """
    try:
        proc = subprocess.run(
            [_sys.executable, str(_RUNBOOK_GATES), name],
            cwd=_CHECKOUT,
            capture_output=True,
            text=True,
            timeout=GATES_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 69, f"runbook_gates.py {name} did not run: {exc}"
    return proc.returncode, (proc.stdout + proc.stderr).strip()


@dataclass(frozen=True)
class PinReads:
    """The gate's boundaries, one field each, so a test replaces one.

    Attributes:
        pins_at: each forward-only pin at a SHA in the checkout, as `cluster_pins.pins_at`
            reads it. Raises `Unjudged` when a pin cannot be read.
        hold: DEPLOY_HOLD's value, or None when the variable does not exist. Raises
            `Unreadable` when the read fails.
        last_good: the commit `last-good` names, or None when the tag does not exist.
            Raises `Unreadable` when the read fails.
        run_gates: runs a gate set by name; its exit code and output.
    """

    pins_at: Callable[[Path, str], Mapping[str, Pin | None]] = lambda checkout, sha: (
        cluster_pins.pins_at(sha, checkout)
    )
    hold: Callable[[], str | None] = _live_hold
    last_good: Callable[[], str | None] = _live_last_good
    run_gates: Callable[[str], tuple[int, str]] = run_gate_set
