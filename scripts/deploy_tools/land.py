#!/usr/bin/env python3
"""Follow a merged PR through to a verified deploy, in one invocation.

Invoke it as ``./scripts/deploy_tools/land.sh``, which execs this file; every doc, skill
and hook names the shim. The implementation is the ``land_lib`` package beside it, one
module per phase; this file is the docstring ``--help`` prints and ``main``.

WHY ONE INVOCATION RATHER THAN A CHAIN. A session cannot write this sequence inline:
shell control flow and command substitution defeat the worktree containment check, which
refuses with "too complex to verify that it stays inside the worktree". A single script
invocation is accepted, loops and all (verified 2026-08-29). Run it backgrounded and the
session is re-invoked when it exits, instead of hand-polling CI for five to fifteen
minutes -- 835 polls across 213 wait episodes before this existed.

ALWAYS REDIRECT STDOUT AND STDERR TO A FILE. A backgrounded Bash call hands this script a
non-blocking pipe, and Ansible refuses to start on one ("Ansible requires blocking IO on
stdin/stdout/stderr"). ``main`` clears O_NONBLOCK on its own fds, and deploy.sh does the
same, but ``> "$CLAUDE_JOB_DIR/tmp/land<n>.log" 2>&1`` is still the whole fix. ``main``
also line-buffers stdout, so that log fills phase by phase instead of arriving at exit --
see ``_prepare_stdio``.

WHAT THIS SCRIPT DOES NOT DO. It holds no check of its own: no health logic, no tag
validation, no staleness logic. deploy.sh owns the lock and the refusals, gitops_tick.sh
owns the tick, deploy_detach_notify.gate owns the health verdict, await_ci.wait owns the CI
wait. A check appearing in here is a bug, not a feature -- it would be a second
implementation that drifts from the first. Which checkout each helper comes from, and why
it is not one answer, is the docstring of ``land_lib/tools.py``.

Usage::

    land.sh --pr 574 --since <pre-merge-sha>
    land.sh --pr 574 --since <sha> --await-merge   # arm `gh pr merge --auto` first, then this
    land.sh --pr 574 --arm-merge --await-merge --since <sha>   # arm the merge INSIDE this script
    land.sh --pr 574 --tags sonarr,radarr    # skip derivation, scope by hand

Exit codes:
  0   deployed and settled, or there was nothing to deploy
  1   CI red, blocked by a change needing a hand, deploy failed, the health gate failed, or
      the PR was closed unmerged, conflicts with master, or its own CI is red
  2   bad arguments
  75  gave up waiting -- the merge budget or CI budget elapsed, the deploy lock stayed busy,
      the tick was skipped for lock contention every time, or the tick has not yet crossed
      origin

Verdicts printed on stdout: settled | unhealthy | deploy-failed | nothing-to-deploy |
blocked | needs-manual-apply | deferred | merge-conflict | pr-ci-red | merge-timeout |
ci-red | ci-timeout | lock-busy.

`blocked` is not a failure of this PR -- something else in the incoming range needs an
operator, and nothing was deployed. `needs-manual-apply` means this PR reaches something
neither a deploy tag nor the tick covers, or a self-applied setup role that reaches a host
beyond the one the tick just ran on (issue #1009), so it is landed but not live everywhere.
`deferred` means the tick applies this PR itself and has not crossed origin yet; the next
tick does it.
`merge-conflict` and `pr-ci-red` are the merge wait ending early on the two states an armed
auto-merge never recovers from. `pr-ci-red` is the PR's CI before the merge; `ci-red` is
master's after it.
"""

from __future__ import annotations

import contextlib
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # scripts/
from deploy_tools.land_lib import pipeline
from deploy_tools.land_lib.landing import Landing
from deploy_tools.land_lib.ledger import annotation_line
from deploy_tools.land_lib.options import parse_args
from deploy_tools.land_lib.tools import Tools


def _prepare_stdio() -> None:
    """Clear O_NONBLOCK on 0/1/2, and line-buffer stdout so a redirected log fills as we go.

    Two separate hazards on the same fds. A backgrounded Bash call hands us non-blocking
    pipes, which Ansible refuses to start on. And a landing is always run with stdout
    redirected to a file, where Python block-buffers it -- so the log stayed EMPTY for the
    whole ten-to-fifteen-minute run and appeared all at once at exit, leaving a session
    tailing it unable to tell which phase was in flight. stderr is line-buffered either way,
    which is why a `die` line used to surface above the `== arm` lines printed before it.

    The suppress is load-bearing rather than defensive: under pytest's capsys, `sys.stdout`
    is not a TextIOWrapper and has no `reconfigure`.
    """
    for fd in (0, 1, 2):
        with contextlib.suppress(OSError):
            os.set_blocking(fd, True)
    with contextlib.suppress(AttributeError, OSError, ValueError):
        sys.stdout.reconfigure(line_buffering=True)


def main(argv: list[str] | None = None, tools: Tools | None = None) -> int:
    """Parse, run, print the outcome, and annotate -- whatever happened."""
    opts = parse_args(argv, __doc__ or "")
    tools = tools or Tools()
    _prepare_stdio()
    ln = Landing(opts, tools)
    rc = 1
    try:
        outcome = pipeline.run(ln)
        rc = outcome.rc
        outcome.emit()
    finally:
        # Fire-and-forget: a landing that succeeded must never report failure because
        # logging it did not. An unexpected exception still annotates, as `aborted`.
        with contextlib.suppress(Exception):
            tools.logger(
                annotation_line(ln.ledger, rc, tools.clock() - ln.ledger.t_start)
            )
    return rc


if __name__ == "__main__":
    sys.exit(main())
