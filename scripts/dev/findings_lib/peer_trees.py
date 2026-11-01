"""The worktrees the other host's checkout registers, read over ssh (#6840).

`claim_is_live` judges a claim against this host's `git worktree list`. Both hosts' operator
checkouts make unprefixed branches, so `other_clone_owner` cannot tell a daniel-server claim
from a daniel-box one, and daniel-box's weekly `reap` released claims daniel-server sessions
still worked. A claim with no local worktree is therefore also looked up in the peer's list.

A peer that cannot be read answers None, and the caller holds the claim: the same fail-safe
direction `running_batch_holds` takes for a batch host it cannot read.
"""

import shlex
import subprocess
from collections.abc import Callable

# Reach the sibling package directories: a directly-invoked script gets only its own
# directory on sys.path, and pyproject's `pythonpath` is a pytest setting.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

from dev.fanout_lib.transport import HOSTS, run_command, this_host
from lib.worktrees import Worktree, parse_worktree_list

# Measured 2026-11-01 from daniel-server: the list from daniel-box's checkout (216 worktrees)
# came back in 1.15s over a fresh ssh connection, whose own ConnectTimeout is 8s.
LIST_TIMEOUT_S = 15.0


def peer_host(local_host: str) -> str | None:
    """The other host in `transport.HOSTS`, or None when this host is not one of them."""
    if local_host not in HOSTS:
        return None
    return next(h for h in HOSTS if h != local_host)


def peer_worktree_list(
    checkout: str,
    run: Callable[..., subprocess.CompletedProcess] = run_command,
    local_host: str | None = None,
) -> list[Worktree] | None:
    """The worktrees the peer host's ``checkout`` registers, or None when it cannot be read.

    Args:
        checkout: the primary checkout's path, which is the same on both hosts for one user.
        run: `fanout_lib.transport.run_command`-shaped, `(host, command, timeout)`. A test
            seam, so no test opens an ssh connection.
        local_host: this host's name; defaults to `transport.this_host()`.

    Returns:
        The peer's worktrees, `[]` when this host has no peer, or None on a failed read. An
        empty listing is a failed read, because a healthy one always names the checkout.
    """
    peer = peer_host(local_host or this_host())
    if peer is None:
        return []
    command = f"git -C {shlex.quote(checkout)} worktree list --porcelain"
    try:
        done = run(peer, command, LIST_TIMEOUT_S)
    except OSError, subprocess.SubprocessError:
        return None
    if done.returncode != 0:
        return None
    return parse_worktree_list(done.stdout) or None
