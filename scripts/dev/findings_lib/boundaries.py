"""Every process boundary `findings.py` crosses, as one injectable object.

A test replaces one field and never a module attribute, so no function in `findings.py` is
pinned to the module a test imported it from. The defaults are the real implementations.

THREE BOUNDARIES, AND NO SHELL. `gh` and `gh_json` are the GitHub Issues register;
`worktree_facts` is the git read that decides whether a claim is still live. There is
deliberately nothing here that executes a command: a verify-by is prose describing how to
check a finding, and `findings.py verify` prints it rather than running it (#1313). The
`run_verify` field, and the read-only classifier that gated the text it ran, were removed
together with the execution they existed to make safe.

`worktree_facts` arrived fourth because its absence was measurable: `monkeypatch_allowlist.txt`
carried 8 `monkeypatch.setattr` calls across two test modules, every one of them standing in
for this missing field. A patch pins the module a test imported it from, so `cmd_claims` and
`cmd_next` could only be driven from a module attribute; injecting the read here retires all 8.
"""

import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

# Reach the sibling package directories: a directly-invoked script gets only its own
# directory on sys.path, and pyproject's `pythonpath` is a pytest setting.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

from dev.prune_worktrees import _worktree_facts
from lib.gh import gh, gh_json

# (worktrees, dirty, merged, ok) — `prune_worktrees._worktree_facts`'s own return, named here
# so the field below reads as one thing rather than a four-element tuple spelled out.
WorktreeFacts = tuple[list[Any], Callable[[str], bool], Callable[[Any], bool], bool]


@dataclass(frozen=True)
class FindingsTools:
    """The gh reads, the gh writes and the worktree read.

    Each is replaceable on its own, which is what lets a test drive `claims`, `reap`, `claim`
    or `next` against invented worktree state without patching a module attribute.
    """

    gh_json: Callable[..., Any] = gh_json
    gh: Callable[..., subprocess.CompletedProcess[str]] = gh
    worktree_facts: Callable[[], WorktreeFacts] = _worktree_facts
