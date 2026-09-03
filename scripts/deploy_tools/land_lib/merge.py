"""The merge phase: arm `gh pr merge --auto` here, and wait for the merge to arrive.

--arm-merge exists so an unattended session never issues `gh pr merge` itself: it sits on
the ask list, and auto mode suspends the allow list, so a session with nobody to answer the
prompt times out as a denial (three attempts, three denials, 2026-09-03, issue #979).
Idempotent: a MERGED PR is left alone, a CLOSED one dies.

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
from deploy_tools.land_lib.outcome import say


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
    except subprocess.CalledProcessError as exc:
        ln.die(f"gh pr merge --auto failed for PR #{pr}: {exc.stderr.strip()}", 1)
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
