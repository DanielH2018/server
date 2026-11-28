#!/usr/bin/env python3
"""Follow a merged PR through to a verified deploy, in one invocation.

Invoke it as ``./scripts/deploy_tools/land.sh``, which execs this file; every doc, skill
and hook names the shim. The implementation is the ``land_lib`` package beside it, one
module per phase; this file holds ``main`` and the docstring ``--help`` prints from.
``--help`` prints the summary line above and everything from ``Usage::`` on. The sections
between are rationale for whoever edits this file, so they print nowhere.

WHY ONE INVOCATION RATHER THAN A CHAIN. A session cannot write this sequence inline: shell
control flow and command substitution defeat the worktree containment check, which refuses with
"too complex to verify that it stays inside the worktree". A single script invocation is
accepted, loops and all. Run it backgrounded and the session is re-invoked when it exits, instead
of hand-polling CI for five to fifteen minutes -- 835 polls across 213 wait episodes before this
existed.

``--detach && cc-wait land <pr>`` IS THE ONE-COMMAND FORM, and it is what a session should reach
for. ``--detach`` names its own logfile, forks the landing into it and returns; ``cc-wait land
<pr>`` then waits until that landing records its exit code, prints its ``VERDICT:`` line and
exits with the landing's code. ``--detach`` covers two steps a caller would otherwise write by
hand: the ``git rev-parse origin/master`` for ``--since`` (resolved here when the flag is absent,
before the merge is armed, so it is still the PRE-merge tip) and the redirect. The landing runs
as a detached grandchild outside the caller's process tree, so a harness that kills the waiting
call leaves it running to its verdict. The mechanics are ``land_lib/detach.py``.

THE WAIT IS cc-wait's, NOT THIS SCRIPT'S. ``cc-wait`` is the one wait loop every repo shares (the
dotfiles ``cc-wait`` package), and ``land_probe.py`` is this repo's ``land`` source for it. A
wait that runs out of budget exits 75 and prints the command that resumes it: re-run THAT, never
``land.sh``, which a live landing of the PR refuses (``land_lib/pr_lock.py``).

WITHOUT ``--detach``, REDIRECT STDOUT AND STDERR TO A FILE YOURSELF. A backgrounded Bash call
hands this script a non-blocking pipe, and a Python write to a full one raises
``BlockingIOError`` and loses the line. ``main`` clears O_NONBLOCK on its own fds, but another
holder of the pipe can set it again, so ``> "$CLAUDE_JOB_DIR/tmp/land<n>.log" 2>&1`` is still
the whole fix.
``main`` also line-buffers stdout, so that log fills phase by phase instead of arriving at
exit -- see ``_prepare_stdio``.

WITH ``LAND_HANDOFF_UNIT`` SET, THIS SCRIPT LANDS NOTHING ITSELF. The ``claude`` agent user's
profile sets it: that user cannot deploy, so land.sh starts ``<unit>@<pr>.service``,
which lands the PR as the operator under the landing policy, and reports that landing's verdict
and exit code as its own. ``--detach && cc-wait land <pr>`` works unchanged; ``--tags``,
``--since``, ``--subject`` and ``--fresh`` are refused. The mechanics are ``land_lib/handoff.py``.

WHAT THIS SCRIPT DOES NOT DO. It holds no check of its own: no health logic, no deploy
staleness logic. promote and Flux apply every tag, `land_lib/health_gate.gate` owns the health
verdict, await_ci.wait owns the CI wait. A check
appearing in here is a bug, not a feature -- it would be a second implementation that drifts from the first. Which checkout each helper comes from, and why
it is not one answer, is the docstring of ``land_lib/tools.py``.

Usage::

    land.sh --pr 574 --since <pre-merge-sha>
    land.sh --pr 574 --since <sha> --await-merge   # wait for a PR merged some other way
    land.sh --pr 574 --arm-merge --await-merge --since <sha>   # arm the merge INSIDE this script
    land.sh --pr 574 --tags sonarr,radarr    # skip derivation, scope by hand
    land.sh --pr 574 --arm-merge --await-merge --detach && cc-wait land 574
                                             # the one-command form: own logfile, shared wait
    land.sh --pr 574 --detach --log-dir .fanout && cc-wait land 574 --log-dir .fanout
                                             # put the log somewhere else
    land.sh --pr 574 --arm-merge --await-merge --fresh
                                             # land from a checkout of origin/master this
                                             # run makes and removes (`land_lib/fresh.py`);
                                             # a landing whose code is older than master's
                                             # does this itself, so the flag is optional
    land.sh --regate 574                     # re-run a merged pin bump's gates and
                                             # re-post homelab/upgrade-gates; never merges

A READY PR WHOSE HEAD'S ONLY CI SKIPPED AS A DRAFT is converted to draft and back once per head
(`gh pr ready --undo`, then `gh pr ready`) after a 180s grace, because GitHub starts no run on
the ready flip. The wait then continues inside the merge budget; a second draft-skip on the
same head ends `merge-timeout`.

A PR THAT BUMPS A FORWARD-ONLY PIN MERGES ONLY THROUGH THE PIN GATE. When the head moves
`k3s_longhorn_version` or `k3s_metallb_version`, the direct merge first requires daniel-box,
DEPLOY_HOLD reading `none`, no bump of that pin in (last-good, master tip], and a green
`runbook_gates.py longhorn-upgrade` or `metallb-upgrade`. It then posts `homelab/upgrade-gates`
on the head. `--regate <pr>` re-runs the gates for a merged bump and re-posts the status; it
exits 0 when it posted and 1 when it refused. `land_lib/pin_gate.py` has the rule.

Exit codes:
  0   deployed and settled, or there was nothing to deploy
  1   CI red or unproven, a tag matching no k8s entry, a
      held or failed promotion, the health gate failed, or the PR was closed
      unmerged, conflicts with master, or its own CI is red
  64  bad arguments
  75  gave up waiting -- the merge budget or CI budget elapsed, no promotion covered the
      merge in time, the health gate could not read its store, or the builder has not
      published a host package that contains the merge, or another landing of the PR is live
      (`cause=landing-live`), so no landing started

Verdicts printed on stdout: settled | unhealthy | deploy-failed | nothing-to-deploy |
needs-manual-apply | deferred | merge-conflict | pr-ci-red | merge-timeout |
ci-red | ci-timeout | ci-unproven | refused | handoff-timeout | unbuilt.

`needs-manual-apply` means this PR rotates a secret, whose consumers neither a deploy tag nor a
host package covers, so it is landed but not live. `nothing-to-deploy` also covers a PR that
reaches only a setup role, which no landing applies.
`deferred` means this landing gave up waiting and a re-run completes it: no promotion covered
the merge in time, the builder has not published the host package, or the health gate could
not read its store or found its tree older than origin/master (exit 75), so the deploy went
ungraded. Re-run land.sh to grade it.
ONE LANDING OF A PR RUNS AT A TIME. A second `land.sh --pr <n>` started while one is live, on
either host and as any user, ends `refused` with exit 75 before it forks or merges anything.
Its `LAND-RESULT:` line carries `cause=landing-live`, and its verdict names the live
landing's host, pid and log and the `cc-wait land <n>` command that waits on it.
`land_lib/pr_lock.py` has the rule.
`refused` is every other exit-1 stop: the landing policy, a PR closed unmerged or with
changes requested, a read or a fetch that failed, or a checkout whose landing code master
has changed since, under a lander unit or when git could not compare. Outside a unit, a
landing that finds such a checkout re-runs itself from a fresh checkout of origin/master:
the whole landing before `--arm-merge` merges anything
(`classify.refuse_stale_code_before_merge`), the grading after the merge
(`classify.refuse_stale_code`). One `land:` line on stderr says so, and the fresh checkout's
landing prints the `LAND-RESULT:` and `VERDICT:` lines. It re-runs once: when master's landing
code moves again before the fresh landing's own check, that landing ends `refused` and a
re-run of the same command lands. The `land:` line on stderr, repeated in the verdict's
detail, says which. `handoff-timeout` is land.sh giving up on a lander unit that is
still running (`LAND_HANDOFF_UNIT` below); a re-run waits on it again. `unbuilt` replaces a
host's `deferred` when the builder reports a failing reason and has published no package of
that host at or after the merge, so no updater fire can apply it: fix the builder (`probe.py
builder`), then re-run. Bad arguments (64) print no verdict.
A PR whose tag Flux applies waits on `promote.py land-wait`, whose verdicts are settled |
deploy-failed | gave-up | suspended. A tag suspended at the merge commit applies nothing
until a PR resumes it, so the wait and the gate leave it out. `suspended` means every such
tag of the PR is suspended; the landing prints `nothing-to-deploy` and exits 0, unless a host
package or a plane owed a hand decides the verdict. The suspend PR itself ends this way,
before a promotion applies it: check that `probe.py landing` shows `last-good` at or past M.
`settled` also needs R's `flux/soak` status to read `success` once the PR changes a tag; a
`failure` there ends the landing `deploy-failed` with cause `soak-failed`, and the detail
prints the read-only kubectl command that names the services the soak failed.
`ci-unproven` means GitHub never ran CI for the merge commit and no later master commit can
stand in for it, so a re-run alone never converges; the detail names the tip and the way out.
`merge-conflict` and `pr-ci-red` are the merge wait ending early on the two states an armed
auto-merge never recovers from. `pr-ci-red` is the PR's CI before the merge; `ci-red` is
master's after it.

A PR WHOSE BASE IS BEHIND MASTER IS RE-TESTED BEFORE THE MERGE. When the PR's head reads green
and master has changed any path since its merge base, `--arm-merge --await-merge` updates the
PR branch from master once and merges only after CI is green on the updated head. The update
gives the wait one more merge budget. A refused update stops the landing with rc 1 and asks for
a rebase. `land_lib/merge.py` has the rule.
"""

import contextlib
import io
from collections.abc import Callable
import dataclasses
import functools
import os
import shlex
import sys
from pathlib import Path

from lib.exit_codes import LAND_BAD_ARGS, LAND_GAVE_UP
from lib.git import git
from deploy_tools.land_lib import (
    detach,
    fresh,
    handoff,
    pin_gate,
    pipeline,
    pr_lock,
    turn,
)
from deploy_tools.land_lib.landing import Landing
from deploy_tools.land_lib.ledger import Ledger, annotation_line, result_line
from deploy_tools.land_lib.options import HANDOFF_ENV, Options, parse_args
from deploy_tools.land_lib.outcome import Cause, Outcome, Verdict, write_verdict_file
from deploy_tools.land_lib.tools import Classifier, Tools


def _prepare_stdio() -> None:
    """Clear O_NONBLOCK on 0/1/2, and line-buffer stdout so a redirected log fills as we go.

    Two separate hazards on the same fds. A backgrounded Bash call hands us non-blocking
    pipes, where a write that finds the pipe full raises `BlockingIOError`, in this process
    and in every Python child that inherits the fds. And a landing is always run with stdout
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
    if isinstance(sys.stdout, io.TextIOWrapper):
        with contextlib.suppress(OSError, ValueError):
            sys.stdout.reconfigure(line_buffering=True)


def help_text(doc: str) -> str:
    """The part of `doc` that `--help` prints: its summary line, then the `Usage::` line onward.

    A docstring without a line starting `Usage::` comes back whole.
    """
    start = doc.find("\nUsage::")
    if start < 0:
        return doc
    return f"{doc.splitlines()[0]}\n{doc[start:]}"


def main(
    argv: list[str] | None = None,
    tools: Tools | None = None,
    classifier: Classifier | None = None,
) -> int:
    """Parse, run, print the outcome, and annotate every run that became a landing."""
    tools = tools or Tools()
    # DECIDED: a usage error is not a landing, so it annotates nothing. `parse_args` exits
    # `LAND_BAD_ARGS` before a Landing or a Ledger exists, so the only line this could write is
    # `pr=unknown verdict=aborted` -- a row meaning "you typed the command wrong" in the same
    # stream the Landings board counts.
    opts = parse_args(argv, help_text(__doc__ or ""))
    if opts.regate:
        return _regate(opts, tools)
    unit = os.environ.get(HANDOFF_ENV)
    if unit:
        return _hand_off(opts, unit, tools)
    if opts.detach:
        opts = _resolve_since(opts)
    child = fresh.child_argv(sys.argv[1:] if argv is None else argv, opts.since)
    if opts.fresh:
        landing = functools.partial(_land_fresh, opts.primary, tools, child)
    else:
        landing = functools.partial(_land, opts, tools, classifier, child)
    lock = pr_lock.take(opts.pr, tools.hostname())
    if isinstance(lock, pr_lock.Holder):
        return _refuse_second(opts, tools, lock)
    try:
        if opts.detach:
            return _detached(opts, tools, landing, lock)
        return detach.until_stopped(landing)
    finally:
        if lock:
            lock.release()


def _refuse_second(opts: Options, tools: Tools, holder: pr_lock.Holder) -> int:
    """End a landing of a PR that `holder` is landing, before it forks or merges; exit 75.

    It prints the `LAND-RESULT:` and `VERDICT:` lines a landing ends with and fills
    `--verdict-file`. It writes no annotation and no verdict record: the PR's landing is the
    live one, which writes both.
    """
    ledger = Ledger(pr=opts.pr, t_start=tools.clock())
    ledger.cause = Cause.LANDING_LIVE
    outcome = Outcome(
        LAND_GAVE_UP, holder.refusal(opts.pr, tools.hostname()), Verdict.REFUSED
    )
    print(result_line(ledger, outcome))
    outcome.emit()
    if opts.verdict_file:
        with contextlib.suppress(OSError):
            write_verdict_file(opts.verdict_file, outcome.file_line())
    return outcome.rc


def _regate(opts: Options, tools: Tools) -> int:
    """`--regate <pr>`: the pin gate's gates and status post for a merged PR; its exit code.

    It runs before the `LAND_HANDOFF_UNIT` hand-off on purpose. The lander unit exists to
    merge and deploy, and a regate does neither, so it runs as the caller, whose login the
    status description records. It writes no landing annotation for the same reason.
    """
    try:
        pin_gate.regate(Landing(opts, tools))
    except Outcome as outcome:
        outcome.emit()
        return outcome.rc
    return 0


def _hand_off(opts: Options, unit: str, tools: Tools) -> int:
    """Land through `<unit>@<pr>.service` (`land_lib/handoff.py`), detached when asked.

    The flags the unit cannot honour are refused here, before any fork, so the caller reads
    the refusal and its exit code at once rather than from a log.
    """
    refused = handoff.refused_flags(opts)
    if refused:
        print(
            f"land: {unit}@.service lands with --arm-merge --await-merge and decides the "
            f"deploy itself, so it cannot take {', '.join(refused)}; drop them and re-run",
            file=sys.stderr,
        )
        return LAND_BAD_ARGS

    def land() -> int:
        rc, line = handoff.land_through_unit(
            opts.pr,
            unit,
            tools.systemctl,
            follow=tools.follow_journal,
        )
        # The unit records the landing in the operator's file, which this user cannot read, so
        # record the line here too.
        with contextlib.suppress(OSError):
            tools.record_verdict(opts.pr, rc, line)
        return rc

    return _detached(opts, tools, land) if opts.detach else land()


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


def _detached(
    opts: Options,
    tools: Tools,
    landing: Callable[[], int],
    lock: pr_lock.Held | None = None,
) -> int:
    """Fork `landing` into its own logfile and name the `cc-wait` command that waits on it.

    The landing inherits `lock`, the PR lock this process took, and holds it to its end.
    """
    log = detach.log_path(opts.pr, Path(opts.log_dir) if opts.log_dir else None)
    if not lock:
        pid = tools.fork(log, landing, f"land{opts.pr}", keep=())
    else:
        pid = tools.fork(log, lock.for_fork(landing), f"land{opts.pr}", keep=lock.keep)
        lock.pass_on(pid, log)
    detach.announce(pid, log, opts.pr)
    return 0


def _land_fresh(primary: Path, tools: Tools, argv: list[str]) -> int:
    """`--fresh`: the fresh checkout's landing's exit code, or 1 when none started."""
    try:
        return tools.fresh_landing(primary, argv)
    except fresh.CheckoutFailed as failed:
        print(f"land: {failed}; nothing was landed", file=sys.stderr)
        return 1


def _land(
    opts: Options, tools: Tools, classifier: Classifier | None, child: list[str]
) -> int:
    """Run one landing to its verdict; its exit code.

    A caller that passes neither `--detach` nor `--fresh` reaches this from `main` directly.

    A merged PR whose grading moves to a fresh checkout (`fresh.GradeFresh`) gets its verdict,
    record and annotation from that checkout's landing, so this one writes none of them. When
    that landing never starts (`fresh.CheckoutFailed`), this one ends `refused` and records it.
    A landing that hands its turn over (`turn.HandOver`) runs `child`, its own arguments,
    from a fresh checkout the same way, and that landing inherits the turn. So does a landing
    that finds its code older than master's before the merge (`fresh.LandFresh`), with no
    turn to pass.
    """
    _prepare_stdio()
    ln = Landing(opts, tools, classifier)
    rc = 1
    graded_here = True
    # PENDING first, so a reader never takes an earlier landing's line for this one's.
    line = "STOPPED: rc=1 the landing raised before it reached a verdict"
    if opts.verdict_file:
        write_verdict_file(opts.verdict_file, "PENDING")
    try:
        outcome = pipeline.run(ln)
        rc = outcome.rc
        print(result_line(ln.ledger, outcome))
        outcome.emit()
        line = outcome.file_line()
    except fresh.GradeFresh as moved:
        print(f"land: {moved}", file=sys.stderr)
        try:
            rc = tools.fresh_landing(opts.primary, moved.argv)
            graded_here = False
        except fresh.CheckoutFailed as failed:
            rerun = f"land.sh {shlex.join(moved.argv)} {fresh.FRESH_FLAG}"
            rc, line = _refuse_unstarted(
                ln,
                f"{moved}, but {failed}; this landing graded nothing; re-run it as `{rerun}`",
            )
    except (fresh.LandFresh, turn.HandOver) as moved:
        # A `LandFresh` holds no turn: its check runs before the landing asks for one.
        fd = moved.fd if isinstance(moved, turn.HandOver) else None
        print(f"land: {moved}", file=sys.stderr)
        try:
            rc = tools.fresh_landing(opts.primary, child, fd)
            graded_here = False
        except fresh.CheckoutFailed as failed:
            rc, line = _refuse_unstarted(
                ln,
                f"{moved}, but {failed}; nothing is merged; re-run the same land.sh command",
            )
        finally:
            if fd is not None:
                os.close(fd)
    except Exception as exc:
        # Still one VERDICT line, as every exit-1 end prints, and the same verdict a
        # crash reaches through the handoff unit's STOPPED line. The traceback follows, and
        # the annotation below stays `aborted`, so the board keeps a crash apart from a
        # refusal.
        reason = " ".join(f"{type(exc).__name__}: {exc}".split())
        crashed = Outcome(1, f"PR #{opts.pr} — the landing raised {reason}")
        print(result_line(ln.ledger, crashed))
        crashed.emit()
        line = crashed.file_line()
        raise
    finally:
        if graded_here:
            _record(opts, tools, ln, rc, line)
    return rc


def _refuse_unstarted(ln: Landing, why: str) -> tuple[int, str]:
    """End `ln` refused, because the landing its fresh checkout was to run never started.

    Prints the VERDICT line and the outcome's detail, and sets the ledger's verdict, so the
    annotation `_record` writes reads `refused`.

    Returns:
      The exit code and the verdict-file line.
    """
    refused = Outcome(1, f"PR #{ln.opts.pr} — {why}")
    ln.ledger.verdict = refused.verdict or ""
    print(result_line(ln.ledger, refused))
    refused.emit()
    return refused.rc, refused.file_line()


def _record(opts: Options, tools: Tools, ln: Landing, rc: int, line: str) -> None:
    """Write the landing's verdict file, its verdict record and its annotation."""
    if opts.verdict_file:
        with contextlib.suppress(OSError):
            write_verdict_file(opts.verdict_file, line)
    # `probe.py landing-verdicts` reads this, so a caller never greps a task's output.
    with contextlib.suppress(OSError):
        tools.record_verdict(opts.pr, rc, line)
    # Fire-and-forget: a landing that succeeded must never report failure because logging it failed.
    with contextlib.suppress(Exception):
        tools.logger(annotation_line(ln.ledger, rc, tools.clock() - ln.ledger.t_start))


if __name__ == "__main__":
    sys.exit(main())
