"""Which Unix user's clone a worktree branch belongs to, read from the branch name alone.

The operator (`ubuntu`) and the `claude` agent user each work from their own clone of this
repo (`docs/claude-agent-user.md`), and neither lists the other's worktrees. A branch name
still says whose it is. The agent user's login profile sets `CLAUDE_WORKTREE_PREFIX`, and
EnterWorktree then names its branches `worktree-<prefix>+<slug>`; the operator's environment
leaves it unset, and its branches carry no `+` prefix.

`findings.py` reads this before judging a claim (#4103), and `fanout.py place` reads the
same variable to name a batch branch, so the two cannot disagree on whose branch is whose.
"""

import os
import re
from collections.abc import Mapping

WORKTREE_PREFIX_ENV = "CLAUDE_WORKTREE_PREFIX"

_PREFIXED = re.compile(r"^worktree-([^+/]+)\+")


def own_prefix(env: Mapping[str, str] = os.environ) -> str:
    """The running user's worktree prefix, or `""` for the operator."""
    return env.get(WORKTREE_PREFIX_ENV, "")


def branch_prefix(branch: str) -> str:
    """The prefix `branch` was created under, or `""` for an operator branch."""
    match = _PREFIXED.match(branch)
    return match.group(1) if match else ""


def other_clone_owner(branch: str, env: Mapping[str, str] = os.environ) -> str | None:
    """Whose clone `branch` lives in when it is not the running user's, else None.

    Returns the owning prefix (`claude`), or `operator` for an unprefixed branch read by a
    prefixed user.
    """
    owner = branch_prefix(branch)
    if owner == own_prefix(env):
        return None
    return owner or "operator"
