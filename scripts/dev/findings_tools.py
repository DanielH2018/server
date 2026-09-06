"""Every process boundary `findings.py` crosses, as one injectable object.

A test replaces one field and never a module attribute, so no function in `findings.py` is
pinned to the module a test imported it from. The defaults are the real implementations.

FOUR BOUNDARIES, AND THE CLASSIFIER LOADER IS STILL NOT ONE. `gh` and `gh_json` are the
GitHub Issues register; `run_verify` is the shell a stored `## Verify-by` command runs in;
`worktree_facts` is the git read that decides whether a claim is still live. Loading the
read-only classifier out of `.claude/hooks/` stays a defaulted parameter on
`classify_verify_command` instead, because that loader and the decision it feeds are one unit
and belong beside each other rather than at this seam.

`worktree_facts` arrived fourth because its absence was measurable: `monkeypatch_allowlist.txt`
carried 8 `monkeypatch.setattr` calls across two test modules, every one of them standing in
for this missing field. A patch pins the module a test imported it from, so `cmd_claims` and
`cmd_next` could only be driven from a module attribute; injecting the read here retires all 8.

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

from dev.prune_worktrees import _worktree_facts
from lib.gh import gh, gh_json
from lib.repo_paths import REPO

# (worktrees, dirty, merged, ok) — `prune_worktrees._worktree_facts`'s own return, named here
# so the field below reads as one thing rather than a four-element tuple spelled out.
WorktreeFacts = tuple[list[Any], Callable[[str], bool], Callable[[Any], bool], bool]


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
    """The gh reads, the gh writes, the verify-by shell and the worktree read.

    Each is replaceable on its own, which is what lets a test drive `claims`, `reap`, `claim`
    or `next` against invented worktree state without patching a module attribute.
    """

    gh_json: Callable[..., Any] = gh_json
    gh: Callable[..., subprocess.CompletedProcess[str]] = gh
    run_verify: Callable[[str, float], subprocess.CompletedProcess[str]] = run_verify
    worktree_facts: Callable[[], WorktreeFacts] = _worktree_facts
