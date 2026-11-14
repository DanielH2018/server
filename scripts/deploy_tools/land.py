#!/usr/bin/env python3
"""Follow a merged PR through to a verified deploy, in one invocation.

Invoke it as ``./scripts/deploy_tools/land.sh``, which execs this file; every doc, skill
and hook names the shim. The implementation is the ``land_lib`` package beside it, one
module per phase; this file holds ``main`` and the docstring ``--help`` prints from.
``--help`` prints the summary line above and everything from ``Usage::`` on. The sections
between are rationale for whoever edits this file, so they print nowhere (#4448).

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
call leaves it running to its verdict (issue #3158). The mechanics are ``land_lib/detach.py``.

THE WAIT IS cc-wait's, NOT THIS SCRIPT'S. ``cc-wait`` is the one wait loop every repo shares (the
dotfiles ``cc-wait`` package), and ``land_probe.py`` is this repo's ``land`` source for it. A
wait that runs out of budget exits 75 and prints the command that resumes it: re-run THAT, never
``land.sh``, which would start a second landing.

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
                                             # run makes and removes (`land_lib/fresh.py`)
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
      published a host package that contains the merge. From a `--detach` parent, 75 means
      only that the group's live-landing limit was full, so no landing started

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
`refused` is every other exit-1 stop: the landing policy, a PR closed unmerged or with
changes requested, a read or a fetch that failed, or a checkout whose landing code master
has changed since, before `--arm-merge` merges anything
(`classify.refuse_stale_code_before_merge`); re-run that one with `--fresh`. After the merge, such
a checkout hands the grading to a fresh checkout of origin/master itself, whose landing prints
the verdict (`classify.refuse_stale_code`). The `land:` line on stderr, repeated in the
verdict's detail, says which. `handoff-timeout` is land.sh giving up on a lander unit that is
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

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # scripts/
from lib.exit_codes import LAND_BAD_ARGS, LAND_GAVE_UP
from lib.git import git
from deploy_tools.land_lib import detach, fresh, handoff, live_limit, pin_gate, pipeline
from deploy_tools.land_lib.landing import Landing
from deploy_tools.land_lib.ledger import annotation_line, result_line
from deploy_tools.land_lib.options import HANDOFF_ENV, Options, parse_args
from deploy_tools.land_lib.outcome import Outcome, write_verdict_file
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
    # DECIDED: a usage error is not a landing, so it annotates nothing. argparse raises
    # SystemExit(2) before a Landing or a Ledger exists, so the only line this could write is
    # `pr=unknown verdict=aborted` -- a row meaning "you typed the command wrong" in the same
    # stream the Landings board counts. The bash original annotated it because its EXIT trap
    # was installed before the arg loop, and this port reproduced that until issue #1304
    # measured 592 such rows in Loki's 744h window. The wrapper below still annotates
    # nothing: it only renumbers the code, and `--help` (exit 0) passes through untouched.
    #
    # WHY IT IS WRAPPED AGAIN. argparse's 2 collided with two other meanings a caller sees
    # through this same pipeline -- `CI_DISARMED` and `PUBLISH_PUSHED_NO_PR`
    # -- so `land.sh` alone answered a bad command line with a different number from every
    # other entry point here. `LAND_BAD_ARGS` is the shared 64.
    try:
        opts = parse_args(argv, help_text(__doc__ or ""))
    except SystemExit as exc:
        raise SystemExit(LAND_BAD_ARGS if exc.code == 2 else exc.code) from None
    if opts.regate:
        return _regate(opts, tools)
    unit = os.environ.get(HANDOFF_ENV)
    if unit:
        return _hand_off(opts, unit, tools)
    if opts.detach:
        opts = _resolve_since(opts)
    if opts.fresh:
        child = fresh.child_argv(sys.argv[1:] if argv is None else argv, opts.since)
        landing = functools.partial(_land_fresh, opts.primary, tools, child)
    else:
        landing = functools.partial(_land, opts, tools, classifier)
    reads = live_limit.github_reads(tools.gh_json)
    if opts.detach:
        return _detached(opts, landing, live_limit.admission(opts.pr, reads))
    with detach.holding_slot(live_limit.pr_kind(opts.pr, reads)):
        return landing()


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

    A `--detach` here takes no slot and is never refused: the unit runs `land.sh` as the
    operator, on the operator's token, and its foreground landing holds the slot in the
    operator's registry. A slot or a quota read here would count the caller's user instead.
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
        # The unit records the landing in the operator's file, which this user cannot
        # read, so the reported line is recorded here too.
        with contextlib.suppress(OSError):
            tools.record_verdict(opts.pr, rc, line)
        return rc

    return _detached(opts, land, None) if opts.detach else land()


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
    opts: Options, landing: Callable[[], int], admission: live_limit.Admission | None
) -> int:
    """Fork `landing` into its own logfile and name the `cc-wait` command that waits on it."""
    log = detach.log_path(opts.pr, Path(opts.log_dir) if opts.log_dir else None)
    try:
        pid = detach.fork(log, landing, admission, f"land{opts.pr}")
    except detach.TooManyLandings as full:
        log.unlink(missing_ok=True)
        print(f"land: {full}", file=sys.stderr)
        return LAND_GAVE_UP
    detach.announce(pid, log, opts.pr)
    return 0


def _land_fresh(primary: Path, tools: Tools, argv: list[str]) -> int:
    """`--fresh`: the fresh checkout's landing's exit code, or 1 when none started."""
    try:
        return tools.fresh_landing(primary, argv)
    except fresh.CheckoutFailed as failed:
        print(f"land: {failed}; nothing was landed", file=sys.stderr)
        return 1


def _land(opts: Options, tools: Tools, classifier: Classifier | None) -> int:
    """Run one landing to its verdict; its exit code.

    The path every invocation took before `--detach` existed, unchanged: a caller that passes
    neither new flag reaches this from `main` directly.

    A merged PR whose grading moves to a fresh checkout (`fresh.GradeFresh`) gets its verdict,
    record and annotation from that checkout's landing, so this one writes none of them. When
    that landing never starts (`fresh.CheckoutFailed`), this one ends `refused` and records it.
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
            refused = Outcome(
                1,
                f"PR #{opts.pr} — {moved}, but {failed}; this landing graded nothing; "
                f"re-run it as `{rerun}`",
            )
            ln.ledger.verdict = refused.verdict or ""
            rc = refused.rc
            print(result_line(ln.ledger, refused))
            refused.emit()
            line = refused.file_line()
    except Exception as exc:
        # Still one VERDICT line, as every exit-1 end prints (#4451), and the same verdict a
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


def _record(opts: Options, tools: Tools, ln: Landing, rc: int, line: str) -> None:
    """Write the landing's verdict file, its verdict record and its annotation."""
    if opts.verdict_file:
        with contextlib.suppress(OSError):
            write_verdict_file(opts.verdict_file, line)
    # `probe.py landing-verdicts` reads this, so a caller never greps a task's output.
    with contextlib.suppress(OSError):
        tools.record_verdict(opts.pr, rc, line)
    # Fire-and-forget: a landing that succeeded must never report failure because
    # logging it did not. An unexpected exception still annotates, as `aborted`.
    with contextlib.suppress(Exception):
        tools.logger(annotation_line(ln.ledger, rc, tools.clock() - ln.ledger.t_start))


if __name__ == "__main__":
    sys.exit(main())
