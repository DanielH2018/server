"""Step 5: deploy what the tick deferred, one deploy.sh per host, riding out a stale tree.

A deploy reaches only the host the play runs against. PR #928 changed roles/containers/alloy,
a role only daniel-pi declares; `deploy.sh --tags alloy` on daniel-box matched no service,
exited 0, and land.sh printed `settled` while the Pi ran the old container (issue #929).
deploy_tags.py hosts says which host declares each tag. daniel-stage is never on the list
(issue #935; HOSTS_LAND_SH_NEVER_DEPLOYS in deploy_tags.py).
"""

from typing import NoReturn

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))  # scripts/
from deploy_tools.exit_codes import (
    DEPLOY_BROAD,
    DEPLOY_LOCK_BUSY,
    DEPLOY_OK,
    DEPLOY_PLAYBOOK_FAILED,
    DEPLOY_STALE,
    DEPLOY_TAG_MISS,
)
from deploy_tools.land_lib import ci, tick
from deploy_tools.land_lib.landing import Landing, TickState, retry_while_locked
from deploy_tools.land_lib.outcome import (
    ABANDONED_WATCH_NOTE,
    Cause,
    Verdict,
    cause_for_deploy_exit,
    say,
)


def derive_from_diff(ln: Landing) -> None:
    """Resolve the fallback to a tag list HERE rather than handing deploy.sh --changed.

    deploy.sh resolves --changed internally, so the verdict call would receive an empty
    --tags and report settled having checked nothing -- on the large-PR path, where
    verification matters most.
    """
    say(f"deriving tags from the diff since {ln.opts.since}")
    r = ln.tools.deploy_tags(ln.opts.primary, ["changed", ln.opts.since])
    if r.returncode == DEPLOY_BROAD:
        ln.die("the change is broad and maps to no service list — deploy it by hand", 1)
    if r.returncode != DEPLOY_OK:
        ln.die(f"deploy_tags.py changed failed (exit {r.returncode})", 1)
    # An empty element (a doubled or trailing comma) is dropped here rather than handed to
    # deploy.sh, which would refuse the whole list as a tag miss. Deliberate: the empty
    # element carries no service, so refusing on it is a false failure.
    ln.resolved_tags = [t for t in r.stdout.strip().split(",") if t]


def no_tag_outcome(ln: Landing) -> NoReturn:
    """No service tag: owed to a hand, nothing at all, or the tick's own state decides."""
    pr, sha = ln.opts.pr, ln.merge_sha
    if ln.plane:
        print(f"  it needs applying by hand: {ln.plane}")
        ln.finish(
            Verdict.NEEDS_MANUAL_APPLY,
            1,
            f"PR #{pr} reaches no service tag, but is not done",
        )
    if not ln.self_applied:
        ln.finish(Verdict.NOTHING_TO_DEPLOY, 0, f"PR #{pr} touched no service")
    state = ln.tick_state()
    if state == TickState.UNKNOWN:
        print(
            f"  the deployer's state directory ({ln.opts.deployer_state}) could not be read, "
            "so whether the tick applied this PR is unknown"
        )
        ln.finish(
            Verdict.NEEDS_MANUAL_APPLY,
            1,
            f"PR #{pr} — the deployer's state could not be read; confirm the tick applied it",
        )
    if state == TickState.HELD:
        print(
            f"  the deployer is holding {ln.state('hold_sha')}: its apply failed — see hold_plane and the gitops-deploy journal"
        )
        ln.ledger.cause = Cause.TICK_HELD
        ln.finish(
            Verdict.DEPLOY_FAILED,
            1,
            f"PR #{pr} — the tick's own apply failed and is held",
        )
    if state == TickState.BEHIND:
        print(
            f"  the tick did not fast-forward to origin (parked since: {ln.state('behind_since')})"
        )
        if ln.tick_watch_abandoned:
            print(ABANDONED_WATCH_NOTE)
            ln.finish(
                Verdict.DEFERRED,
                75,
                f"PR #{pr} — landed; this run stopped watching a tick still applying, "
                "so whether the deployer is deferring or holding is not yet known",
            )
        print(
            "  Usually a newer merge whose CI is still running; the next tick crosses it. Nothing is wrong with this PR."
        )
        ln.finish(
            Verdict.DEFERRED, 75, f"PR #{pr} — landed, not yet applied by the tick"
        )
    if not ln.broad_applied_covers(sha):
        print(
            "  the tick converged with origin but recorded no broad apply covering this PR "
            f"(broad_applied: {ln.state('broad_applied') or 'absent'})"
        )
        print(
            "  Something OTHER than the tick fast-forwarded the checkout, so the tick will "
            "never see this range again."
        )
        if ln.self_applied_command:
            print(f"  Apply it: {ln.self_applied_command}")
        ln.finish(
            Verdict.NEEDS_MANUAL_APPLY,
            1,
            f"PR #{pr}, {sha} — the tick converged without recording an apply of this PR",
        )
    if ln.remaining_setup:
        local = ln.tools.hostname()
        print(f"  applied on {local} only; it also reaches: {ln.remaining_setup}")
        ln.finish(
            Verdict.NEEDS_MANUAL_APPLY,
            1,
            f"PR #{pr}, {sha} — self-applied on {local} only; other hosts still need it",
        )
    ln.finish(
        Verdict.SETTLED,
        0,
        f"PR #{pr}, {sha} — no service tag; the tick applied it and converged with origin",
    )


def deploy_by_host(ln: Landing) -> int:
    """One deploy.sh per declaring host; the first non-zero exit. Retries resume at the failed host."""
    o, t = ln.opts, ln.tools
    r = t.deploy_tags(o.primary, ["hosts", ln.tags_csv])
    if r.returncode != DEPLOY_OK:
        # deploy.sh was never invoked for any host, so nothing here overlaps with the
        # catch-all in deploy_outcome, which really did run it (issue #1016).
        ln.ledger.cause = Cause.HOST_LOOKUP
        ln.die(
            "deploy_tags.py hosts failed before any deploy.sh ran; nothing was touched; "
            f"tags: {ln.tags_csv}",
            1,
            Verdict.DEPLOY_FAILED,
        )
    lines = [x for x in r.stdout.splitlines() if x.strip()]
    if not lines:
        return t.deploy(
            o.primary, ln.resolved_tags, None, observe=ln.note_in_flock_wait
        )
    local = t.hostname()
    for line in lines:
        host, _, host_tags = line.partition("\t")
        if host in ln.deployed_hosts:
            continue
        target = None if host == local else host
        if target:
            say(
                f"{host_tags}: declared on {host}, deploying there with -e target={host}"
            )
        rc = t.deploy(
            o.primary,
            [x for x in host_tags.split(",") if x],
            target,
            observe=ln.note_in_flock_wait,
        )
        if rc != DEPLOY_OK:
            return rc
        ln.deployed_hosts.add(host)
    return 0


def deploy_with_lock_retry(ln: Landing) -> int:
    """deploy_by_host, retried while the git-tree lock stays busy (exit 75)."""
    o = ln.opts
    return retry_while_locked(
        ln,
        DEPLOY_LOCK_BUSY,
        lambda: deploy_by_host(ln),
        lambda n: (
            f"deploy lock busy (attempt {n}/{o.lock_retries}); retrying in {o.lock_backoff}s"
        ),
    )


def deploy_outcome(ln: Landing, rc: int) -> None:
    """Map deploy.sh's exit to a verdict; 0 returns."""
    pr, tags, o = ln.opts.pr, ln.tags_csv, ln.opts
    if rc == DEPLOY_OK:
        return
    if rc == DEPLOY_TAG_MISS:
        # deploy.sh refused the WHOLE list and deployed nothing, including every valid
        # service beside the bad tag. This read as nothing-to-deploy until 2026-08-29,
        # which is how PR #617 left 22 digest pins undeployed behind a green verdict.
        ln.ledger.cause = Cause.TAG_MISS
        ln.finish(
            Verdict.DEPLOY_FAILED,
            1,
            f"PR #{pr} — a derived tag matched no service, so nothing deployed; tags: {tags}",
        )
    if rc == DEPLOY_LOCK_BUSY:
        ln.die(
            f"deploy lock stayed busy after {o.lock_retries} attempts — nothing deployed",
            75,
            Verdict.LOCK_BUSY,
        )
    if rc == DEPLOY_PLAYBOOK_FAILED:
        # The playbook RAN and a task failed: everything before it is live (issue #840).
        # Not a resume point; re-running it is not automatically safe.
        ln.ledger.cause = Cause.PLAYBOOK_FAILED
        ln.finish(
            Verdict.DEPLOY_FAILED,
            1,
            f"PR #{pr} — a playbook task failed AFTER applying; some changes are live; tags: {tags}",
        )
    ln.ledger.cause = cause_for_deploy_exit(rc)
    ln.finish(Verdict.DEPLOY_FAILED, 1, f"PR #{pr}, exit {rc}")


def deploy_phase(ln: Landing) -> None:
    """Step 5, end to end: derive if needed, deploy, ride out a stale tree, stamp the ledger."""
    o, t = ln.opts, ln.tools
    if ln.needs_diff:
        derive_from_diff(ln)
    if not ln.resolved_tags:
        no_tag_outcome(ln)
    ln.ledger.tags_label = ln.tags_csv
    rc = deploy_with_lock_retry(ln)
    # 4 = the tree is behind origin/master: someone merged during the CI wait. The tick
    # fast-forwards to the newest GREEN commit in the incoming range, not only to a green tip,
    # so what this landing needs green is its OWN merge commit -- wait on that, every attempt,
    # after the blockers check and backed off by `lock_backoff` the way the lock-contention
    # retry above already is. The wait normally returns at once, because step 3 already waited
    # on the same SHA; it is kept because a retry can reach here without step 3 having run.
    #
    # It waited on the CURRENT TIP until the ancestor walk landed, and that is what the
    # `tip-outran-retries` verdict measured: six landings in 14 days spent 400-614s chasing a
    # tip that moved again while they waited, on merge commits whose own CI was already green.
    # Issue #1084 is the older half: PR #1051's landing retried three times in ~25s with no
    # backoff and, because the wait used to be gated behind `tip_sha != merge_sha`, no CI wait
    # either, while master CI on the merge commit was still 2m48s from green.
    for attempt in range(1, o.stale_retries + 1):
        if rc != DEPLOY_STALE:
            break
        # The backoff DOUBLES each attempt (60s, 120s, 240s). A fixed 60s spends three
        # attempts inside ~4 minutes, and PR #1460's landing on 2026-09-09 met a merge rate of
        # roughly one every 2 minutes: the checkout went from 7 to 9 commits behind DURING the
        # landing and every retry lost the same race (#1466). Three attempts that cannot
        # converge are worse than two that can, so each wait is longer than the gap that beat
        # the last one.
        backoff = o.lock_backoff * 2 ** (attempt - 1)
        say(
            f"tree went stale mid-landing (exit 4); retrying in {backoff}s "
            f"({attempt}/{o.stale_retries})"
        )
        t.sleep(backoff)
        ln.fetch_branch()
        if ci.blockers(ln) == DEPLOY_BROAD:
            ln.finish(
                Verdict.BLOCKED,
                1,
                f"PR #{o.pr} — a change needing a hand landed during the wait; see above",
            )
        say(
            f"waiting for master CI on the merge commit {ln.merge_sha} "
            "(the tick fast-forwards to the newest green commit it can reach)"
        )
        started = t.clock()
        ci.wait_master_ci(ln, ln.merge_sha, f"the merge commit {ln.merge_sha}")
        # CI time, not deploy time: shift both later stamps so the board books it under
        # wait_ci with no new field to learn. Includes the backoff sleep above (mirrors
        # `deploy_with_lock_retry`'s own `+ o.lock_backoff`), or that time falls into
        # t_deploy instead -- the exact mis-attribution this comment exists to prevent.
        waited = t.clock() - started + backoff
        ln.ledger.t_ci = (ln.ledger.t_ci or 0.0) + waited
        ln.ledger.t_tick = (ln.ledger.t_tick or 0.0) + waited
        # DECIDED: a failing retick here ENDS the landing (deploy-failed, cause=tick-failed)
        # rather than carrying on to deploy_by_host the way bash's stale-retry loop did --
        # bash discarded the tick's own exit code and kept going regardless. Deliberate per
        # tick.py's module docstring and #1013: this shares tick.py's one retry
        # implementation with step 4 rather than land.sh's un-retried, un-accounted copy, and
        # that implementation's failure mode is to raise. Listed as #1085 item 8 so it is not
        # re-derived as a parity bug.
        tick.run_tick(ln)
        rc = deploy_by_host(ln)
    if rc == DEPLOY_STALE:
        # Every retry lost the tip race. `deploy-failed` is the wrong word for it: exit 4 means
        # NOTHING was deployed and re-running is safe, while `deploy-failed` reads as "the
        # deploy broke" and sends a session looking at its own change. PR #1460 ended that way
        # twice on 2026-09-09 with four other sessions merging (#1466). Exit 75, the resume-point
        # code, for the same reason `lock-busy` uses it.
        ln.ledger.cause = Cause.DEPLOY_EXIT_STALE
        ln.die(
            f"master merged faster than one tick-and-deploy cycle; every one of "
            f"{o.stale_retries} retries found the tree behind again — nothing was deployed, "
            f"re-run the same land.sh command",
            75,
            Verdict.TIP_OUTRAN_RETRIES,
        )
    deploy_outcome(ln, rc)
    ln.ledger.t_deploy = t.clock()
