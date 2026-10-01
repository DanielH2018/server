#!/usr/bin/env python3
"""Follow a merged PR through to a verified deploy, in one invocation.

Invoke it as ``./scripts/deploy_tools/land.sh``, which execs this file; every doc, skill
and hook names the shim. The implementation is the ``land_lib`` package beside it, one
module per phase; this file is the docstring ``--help`` prints and ``main``.

WHY ONE INVOCATION RATHER THAN A CHAIN. A session cannot write this sequence inline: shell
control flow and command substitution defeat the worktree containment check, which refuses with
"too complex to verify that it stays inside the worktree". A single script invocation is
accepted, loops and all. Run it backgrounded and the session is re-invoked when it exits, instead
of hand-polling CI for five to fifteen minutes -- 835 polls across 213 wait episodes before this
existed.

``--detach --await-verdict`` IS THE ONE-COMMAND FORM, and it is what a session should reach for.
It names its own logfile, forks the landing into it, then blocks until that landing prints its
``VERDICT:`` line and exits with the landing's code. It covers three steps a caller would
otherwise write by hand: the ``git rev-parse origin/master`` for ``--since`` (resolved here when
the flag is absent, before the merge is armed, so it is still the PRE-merge tip), the redirect,
and ``timeout 1200 tail -f -n +1 <log> | grep -m1 '^VERDICT:'``. The landing runs as a detached
grandchild outside the caller's process tree, so a harness that kills the waiting call leaves it
running to its verdict (issue #3158). The mechanics are ``land_lib/detach.py``.

WITHOUT ``--detach``, REDIRECT STDOUT AND STDERR TO A FILE YOURSELF. A backgrounded Bash call
hands this script a non-blocking pipe, and Ansible refuses to start on one ("Ansible requires
blocking IO on stdin/stdout/stderr"). ``main`` clears O_NONBLOCK on its own fds, and deploy.sh
does the same, but ``> "$CLAUDE_JOB_DIR/tmp/land<n>.log" 2>&1`` is still the whole fix.
``main`` also line-buffers stdout, so that log fills phase by phase instead of arriving at
exit -- see ``_prepare_stdio``.

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
    land.sh --pr 574 --arm-merge --await-merge --detach --await-verdict
                                             # the one-command form: own logfile, own wait
    land.sh --pr 574 --detach --await-verdict --log-dir .fanout   # put the log somewhere else

Exit codes:
  0   deployed and settled, or there was nothing to deploy
  1   CI red, blocked by a change needing a hand, deploy failed, the health gate failed, or
      the PR was closed unmerged, conflicts with master, or its own CI is red
  64  bad arguments
  75  gave up waiting -- the merge budget or CI budget elapsed, the deploy lock stayed busy,
      the tick was skipped for lock contention every time, master merged faster than one
      tick-and-deploy cycle, or the tick has not yet crossed origin

Verdicts printed on stdout: settled | unhealthy | deploy-failed | nothing-to-deploy |
blocked | needs-manual-apply | deferred | merge-conflict | pr-ci-red | merge-timeout |
ci-red | ci-timeout | lock-busy | tip-outran-retries.

`blocked` is not a failure of this PR -- something else in the incoming range needs an
operator, and nothing was deployed. `needs-manual-apply` means this PR reaches something
neither a deploy tag nor the tick covers, or a self-applied setup role that reaches a host
beyond the one the tick just ran on, so it is landed but not live everywhere.
`deferred` means the tick applies this PR itself and has not crossed origin yet; the next
tick does it -- UNLESS the verdict says this run stopped watching a tick that was still
applying, in which case the deployer's markers were read mid-apply and a hold cannot be
ruled out. Re-run land.sh then; no later tick crosses a hold.
`merge-conflict` and `pr-ci-red` are the merge wait ending early on the two states an armed
auto-merge never recovers from. `pr-ci-red` is the PR's CI before the merge; `ci-red` is
master's after it.
"""

import contextlib
import dataclasses
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # scripts/
from lib.exit_codes import LAND_BAD_ARGS
from lib.git import git
from deploy_tools.land_lib import detach, pipeline
from deploy_tools.land_lib.landing import Landing
from deploy_tools.land_lib.ledger import annotation_line
from deploy_tools.land_lib.options import Options, parse_args
from deploy_tools.land_lib.tools import Classifier, Tools


def _prepare_stdio() -> None:
    """Clear O_NONBLOCK on 0/1/2, and line-buffer stdout so a redirected log fills as we go.

    Two separate hazards on the same fds. A backgrounded Bash call hands us non-blocking
    pipes, which Ansible refuses to start on. And a landing is always run with stdout
    redirected to a file, where Python block-buffers it -- so the log would stay EMPTY for
    the whole ten-to-fifteen-minute run and appear all at once at exit, leaving a session
    tailing it unable to tell which phase was in flight. stderr is line-buffered either way,
    so without this a `die` line would surface above the `== arm` lines printed before it.

    The suppress is load-bearing rather than defensive: under pytest's capsys, `sys.stdout`
    is not a TextIOWrapper and has no `reconfigure`.
    """
    for fd in (0, 1, 2):
        with contextlib.suppress(OSError):
            os.set_blocking(fd, True)
    with contextlib.suppress(AttributeError, OSError, ValueError):
        sys.stdout.reconfigure(line_buffering=True)


def main(
    argv: list[str] | None = None,
    tools: Tools | None = None,
    classifier: Classifier | None = None,
) -> int:
    """Parse, run, print the outcome, and annotate every run that became a landing."""
    tools = tools or Tools()
    # DECIDED: a usage error is not a landing, so it annotates nothing. argparse raises
    # SystemExit(2) before a Landing or a Ledger exists, so the only line this could write is
    # `pr=unknown verdict=aborted` -- a row meaning "you typed the command wrong" in the same
    # stream the Landings board counts. The bash original annotated it because its EXIT trap
    # was installed before the arg loop, and this port reproduced that until issue #1304
    # measured 592 such rows in Loki's 744h window. The wrapper below still annotates
    # nothing: it only renumbers the code, and `--help` (exit 0) passes through untouched.
    #
    # WHY IT IS WRAPPED AGAIN. argparse's 2 collided with three other meanings a caller sees
    # through this same pipeline -- `CI_DISARMED`, `DEPLOY_TAG_MISS` and `PUBLISH_PUSHED_NO_PR`
    # -- so `land.sh` alone answered a bad command line with a different number from every
    # other entry point here. `LAND_BAD_ARGS` is the shared 64.
    try:
        opts = parse_args(argv, __doc__ or "")
    except SystemExit as exc:
        raise SystemExit(LAND_BAD_ARGS if exc.code == 2 else exc.code) from None
    if opts.detach:
        return _detached(_resolve_since(opts), tools, classifier)
    return _land(opts, tools, classifier)


def _resolve_since(opts: Options) -> Options:
    """`opts` with `--since` filled in from `origin/master` when the caller left it empty.

    BEFORE THE MERGE IS ARMED, which is the whole reason it is here and not in the pipeline.
    `--since` is the PRE-merge tip: it bounds the range the truncated-list fallback derives
    tags from, and reading it after `--arm-merge` has merged would capture the tip that
    INCLUDES this PR and derive nothing. The ordering is the script's: the caller does not run
    `git rev-parse origin/master` first.

    ONLY ON THE `--detach` PATH, deliberately. An invocation without the flag reaches `_land`
    having had nothing resolved for it, so the proven path is unchanged. An unreadable
    `origin/master` -- a primary checkout that is not there -- leaves `--since` empty and the
    pipeline names the missing checkout, which is the error the caller needs rather than a
    `rev-parse` traceback.
    """
    if opts.since:
        return opts
    try:
        head = git("rev-parse", "origin/master", cwd=opts.primary, check=False)
    except OSError:
        return opts
    if head.returncode != 0:
        return opts
    return dataclasses.replace(opts, since=head.stdout.strip())


def _detached(opts: Options, tools: Tools, classifier: Classifier | None) -> int:
    """Fork the landing into its own logfile; wait for its verdict under `--await-verdict`."""
    log = detach.log_path(opts.pr, Path(opts.log_dir) if opts.log_dir else None)
    pid = detach.fork(log, lambda: _land(opts, tools, classifier))
    detach.announce(pid, log, opts.await_verdict)
    if not opts.await_verdict:
        return 0
    return detach.await_verdict(pid, log)


def _land(opts: Options, tools: Tools, classifier: Classifier | None) -> int:
    """Run one landing to its verdict; its exit code.

    The path every invocation took before `--detach` existed, unchanged: a caller that passes
    neither new flag reaches this from `main` directly.
    """
    _prepare_stdio()
    ln = Landing(opts, tools, classifier)
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
