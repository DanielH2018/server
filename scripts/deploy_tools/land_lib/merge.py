"""The merge phase: arm `gh pr merge --auto` here, and wait for the merge to arrive.

--arm-merge exists so an unattended session never issues `gh pr merge` itself: it sits on
the ask list, and auto mode suspends the allow list, so a session with nobody to answer the
prompt times out as a denial (three attempts, three denials, 2026-09-03, issue #979).
Idempotent: a MERGED PR is left alone, a CLOSED one dies.

GitHub's `enablePullRequestAutoMerge` mutation (what `--auto` calls) rejects a PR that is
already CLEAN -- there is nothing to defer -- so --arm-merge used to fail on exactly the PRs
that were ready to merge (issue #1008, reproduced on PRs #998/#1001/#1002/#1004 on
2026-09-03). A CLEAN rejection falls through to a direct `gh pr merge --squash`; a PR that
merged in the gap between the idempotency check and the `--auto` attempt is a no-op, not a
failure; anything else GitHub calls not-yet-mergeable (BLOCKED, DIRTY, ...) still dies.

`--auto` exiting 0 is not proof the merge was armed either (issue #1029): on PR #1026 it
exited 0, `autoMergeRequest` stayed null, and the landing polled 35 minutes toward
merge-timeout on a PR that was CLEAN with every check green. One read-back answers all three
questions the arm can have gone wrong in -- merged in the gap, armed, or silently not armed --
and an unarmed CLEAN PR takes the same direct-merge path a CLEAN rejection does. An unarmed
PR that is NOT CLEAN dies: direct-merging it would only fail the same way. The read-back
itself failing is not a reason to fail a landing whose arm may well have worked, so it says
so and trusts the exit code.

--await-merge polls the PR's state until merged, so `gh pr create` -> `gh pr merge --auto`
-> one backgrounded land.sh is the whole procedure. Every landing on 2026-09-01 hand-wrote
that wait.
"""

from __future__ import annotations

import subprocess

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))  # scripts/
from deploy_tools.land_lib.landing import BRANCH, Landing
from deploy_tools.land_lib.outcome import Outcome, say


def arm_merge_fallback_decision(state: str, merge_state_status: str) -> str:
    """What to do after `gh pr merge --auto` rejects a PR: already-merged | merge-direct | die.

    GitHub's `enablePullRequestAutoMerge` mutation only accepts a PR that is genuinely
    blocked. A PR that is already CLEAN has nothing to defer, so `--auto` fails on exactly
    the PRs that are ready to merge right now (issue #1008; PRs #998, #1001, #1002, #1004 on
    2026-09-03). A pure function of two strings so the branch is testable without gh.
    """
    if state == "MERGED":
        return "already-merged"
    if merge_state_status == "CLEAN":
        return "merge-direct"
    return "die"


def _merge_direct(ln: Landing, subject: str) -> None:
    """Squash-merge the PR now, for a PR `--auto` will not or did not arm."""
    say(f"PR #{ln.opts.pr} is CLEAN -- nothing to defer; merging directly")
    try:
        ln.tools.gh("pr", "merge", ln.opts.pr, "--squash", "--subject", subject)
    except subprocess.CalledProcessError as exc:
        ln.die(
            f"direct gh pr merge --squash failed for PR #{ln.opts.pr}: {exc.stderr.strip()}",
            1,
        )
    say(f"merged directly: {subject}")


def arm_merge(ln: Landing) -> None:
    """Run `gh pr merge --squash --auto` for this PR, unless it is already merged."""
    pr = ln.opts.pr
    print(f"== arm  arming PR #{pr}'s merge")
    view = ln.view("state,title")
    if view.get("state") == "MERGED":
        say("already merged; --arm-merge is a no-op")
        return
    if view.get("state") == "CLOSED":
        ln.die(f"PR #{pr} was closed without merging — nothing to arm", 1)
    subject = ln.opts.subject or view.get("title", "")
    try:
        ln.tools.gh("pr", "merge", pr, "--squash", "--auto", "--subject", subject)
    except subprocess.CalledProcessError:
        retry = ln.view("state,mergeStateStatus")
        decision = arm_merge_fallback_decision(
            retry.get("state", ""), retry.get("mergeStateStatus", "")
        )
        if decision == "already-merged":
            # A race with the idempotency check above: the PR merged between that read and
            # this --auto attempt. Keep the MERGED short-circuit's semantics: say, not die.
            say(f"gh pr merge --auto failed because PR #{pr} merged in the meantime")
            return
        if decision == "merge-direct":
            _merge_direct(ln, subject)
            return
        ln.die(
            f"gh pr merge --auto failed for PR #{pr} "
            f"(mergeStateStatus={retry.get('mergeStateStatus', '')})",
            1,
        )
    # --auto exiting 0 is not proof the merge was armed (issue #1029). One read-back
    # answers every way it can have gone wrong; a read-back that itself fails must not turn
    # a possibly-successful arm into a failed landing.
    try:
        armed = ln.view("state,mergeStateStatus,autoMergeRequest")
    except Outcome:
        say(f"could not confirm PR #{pr}'s arm; trusting gh pr merge --auto's exit 0")
        return
    state = armed.get("state", "")
    mss = armed.get("mergeStateStatus", "")
    if state == "MERGED":
        say(f"PR #{pr} merged in the meantime")
        return
    if state == "OPEN" and armed.get("autoMergeRequest") is None:
        if arm_merge_fallback_decision(state, mss) == "merge-direct":
            say(
                f"gh pr merge --auto exited 0 but PR #{pr} is not armed "
                "(autoMergeRequest is null)"
            )
            _merge_direct(ln, subject)
            return
        ln.die(
            f"gh pr merge --auto exited 0 but PR #{pr} is not armed "
            f"(mergeStateStatus={mss})",
            1,
        )
    say(f"auto-merge armed: {subject}")


def await_merge(ln: Landing) -> None:
    """Poll until merged. Bail early only on the two states an auto-merge never leaves.

    Only CONFLICTING may bail, and only on two consecutive polls: GitHub computes
    mergeability asynchronously and serves UNKNOWN until it settles (PR #657 read UNKNOWN on
    a live open PR), and master moving under the PR flips the field for one poll. A red PR
    CI is the other way an armed auto-merge never fires; GitHub says only `BLOCKED`, the
    same word it uses while checks run, so await_ci owns that verdict, one-shot. Only its
    exit 1 bails: `pending` IS the grace period, derived rather than guessed.
    """
    o, t = ln.opts, ln.tools
    print(f"== 0/6  waiting for PR #{o.pr} to merge (auto-merge or the merge queue)")
    waited = 0
    conflicting = 0
    while True:
        view = ln.view("state,mergeable,headRefOid")
        state = view.get("state", "")
        if state == "MERGED":
            break
        if state == "CLOSED":
            ln.die(f"PR #{o.pr} was closed without merging — nothing to land", 1)
        conflicting = conflicting + 1 if view.get("mergeable") == "CONFLICTING" else 0
        if conflicting >= 2:
            ln.die(
                f"PR #{o.pr} conflicts with {BRANCH} — rebase it, re-arm "
                "`gh pr merge --squash --auto`, then re-run this",
                1,
                "merge-conflict",
            )
        head = view.get("headRefOid") or ""
        if head:
            rc, line = t.await_ci(head, 0)
            if rc == 1:
                ln.die(
                    f"PR #{o.pr} cannot merge — its own CI is red ({line}); fix it, push, "
                    "and re-run this",
                    1,
                    "pr-ci-red",
                )
        if waited >= o.merge_timeout:
            ln.die(
                f"PR #{o.pr} still {state} after {o.merge_timeout}s — not being merged; "
                "look at its checks or the queue",
                75,
                "merge-timeout",
            )
        t.sleep(o.merge_poll)
        waited += o.merge_poll
    say(f"merged after {waited}s")
