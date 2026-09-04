"""Every process boundary `findings.py` crosses, as one injectable object.

A test replaces one field and never a module attribute, so no function in `findings.py` is
pinned to the module a test imported it from. The defaults are the real implementations.

THREE BOUNDARIES, NOT FOUR. `gh` and `gh_json` are the GitHub Issues register; `run_verify`
is the shell a stored `## Verify-by` command runs in. The fourth thing that reaches outside
the process — loading the read-only classifier out of `.claude/hooks/` — stays a defaulted
parameter on `classify_verify_command`, because that loader and the decision it feeds are one
unit and belong beside each other rather than at this seam.

`run_verify` runs the command with `shell=True` and no argv splitting BY DESIGN: an issue
body stores a command line, not an argv. What makes that safe is the gate in front of it —
`classify_verify_command` refuses anything the repo's own read-only classifier does not
clear, and this function is never called without that verdict.
"""

import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

# Reach the sibling package directories: a directly-invoked script gets only its own
# directory on sys.path, and pyproject's `pythonpath` is a pytest setting.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

from lib.gh import gh, gh_json
from lib.repo_paths import REPO


def run_verify(command: str, timeout: float) -> subprocess.CompletedProcess[str]:
    """Run a verify-by command from the repo root, capturing both streams.

    Raises `subprocess.TimeoutExpired` or `OSError` rather than reporting them: the caller
    turns each into the `error` verdict with its own wording.
    """
    return subprocess.run(
        command,
        shell=True,
        cwd=str(REPO),
        capture_output=True,
        text=True,
        timeout=timeout,
    )


@dataclass(frozen=True)
class FindingsTools:
    """The gh reads, the gh writes and the verify-by shell, each replaceable on its own."""

    gh_json: Callable[..., Any] = gh_json
    gh: Callable[..., subprocess.CompletedProcess[str]] = gh
    run_verify: Callable[[str, float], subprocess.CompletedProcess[str]] = run_verify
