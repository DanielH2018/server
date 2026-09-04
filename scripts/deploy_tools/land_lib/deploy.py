"""Step 5: deploy what the tick deferred, one deploy.sh per host, riding out a stale tree.

A deploy reaches only the host the play runs against. PR #928 changed roles/containers/alloy,
a role only daniel-pi declares; `deploy.sh --tags alloy` on daniel-box matched no service,
exited 0, and land.sh printed `settled` while the Pi ran the old container (issue #929).
deploy_tags.py hosts says which host declares each tag. daniel-stage is never on the list
(issue #935; HOSTS_LAND_SH_NEVER_DEPLOYS in deploy_tags.py).
"""

from __future__ import annotations

from typing import NoReturn

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))  # scripts/
from deploy_tools.land_lib import ci, tick
from deploy_tools.land_lib.landing import BRANCH, Landing
from deploy_tools.land_lib.outcome import say


def derive_from_diff(ln: Landing) -> None:
    """Resolve the fallback to a tag list HERE rather than handing deploy.sh --changed.

    deploy.sh resolves --changed internally, so the verdict call would receive an empty
    --tags and report settled having checked nothing -- on the large-PR path, where
    verification matters most.
    """
    say(f"deriving tags from the diff since {ln.opts.since}")
    r = ln.tools.deploy_tags(ln.opts.primary, ["changed", ln.opts.since])
    if r.returncode == 3:
        ln.die("the change is broad and maps to no service list — deploy it by hand", 1)
    if r.returncode != 0:
        ln.die(f"deploy_tags.py changed failed (exit {r.returncode})", 1)
    ln.tags = r.stdout.strip()


def no_tag_outcome(ln: Landing) -> NoReturn:
    """No service tag: owed to a hand, nothing at all, or the tick's own state decides."""
    pr, sha = ln.opts.pr, ln.merge_sha
    if ln.plane:
        print(f"  it needs applying by hand: {ln.plane}")
        ln.finish(
            "needs-manual-apply", 1, f"PR #{pr} reaches no service tag, but is not done"
        )
    if not ln.self_applied:
        ln.finish("nothing-to-deploy", 0, f"PR #{pr} touched no service")
    state = ln.tick_state()
    if state == "held":
        print(
            f"  the deployer is holding {ln.state('hold_sha')}: its apply failed — see hold_plane and the gitops-deploy journal"
        )
        ln.ledger.cause = "tick-held"
        ln.finish(
            "deploy-failed", 1, f"PR #{pr} — the tick's own apply failed and is held"
        )
    if state == "behind":
        print(
            f"  the tick did not fast-forward to origin (parked since: {ln.state('behind_since')})"
        )
        print(
            "  Usually a newer merge whose CI is still running; the next tick crosses it. Nothing is wrong with this PR."
        )
        ln.finish("deferred", 75, f"PR #{pr} — landed, not yet applied by the tick")
    if ln.remaining_setup:
        local = ln.tools.hostname()
        print(f"  applied on {local} only; it also reaches: {ln.remaining_setup}")
        ln.finish(
            "needs-manual-apply",
            1,
            f"PR #{pr}, {sha} — self-applied on {local} only; other hosts still need it",
        )
    ln.finish(
        "settled",
        0,
        f"PR #{pr}, {sha} — no service tag; the tick applied it and converged with origin",
    )


def deploy_by_host(ln: Landing) -> int:
    """One deploy.sh per declaring host; the first non-zero exit. Retries resume at the failed host."""
    o, t = ln.opts, ln.tools
    r = t.deploy_tags(o.primary, ["hosts", ln.tags])
    if r.returncode != 0:
        # deploy.sh was never invoked for any host, so nothing here overlaps with the
        # catch-all in deploy_outcome, which really did run it (issue #1016).
        ln.ledger.cause = "host-lookup"
        ln.die(
            "deploy_tags.py hosts failed before any deploy.sh ran; nothing was touched; "
            f"tags: {ln.tags}",
            1,
            "deploy-failed",
        )
    lines = [x for x in r.stdout.splitlines() if x.strip()]
    if not lines:
        return t.deploy(o.primary, ln.tags, None)
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
        rc = t.deploy(o.primary, host_tags, target)
        if rc != 0:
            return rc
        ln.deployed_hosts.add(host)
    return 0


def deploy_with_lock_retry(ln: Landing) -> int:
    """deploy_by_host, retried while the git-tree lock stays busy (exit 75)."""
    o, t = ln.opts, ln.tools
    rc = 0
    for attempt in range(1, o.lock_retries + 1):
        # Sampled BEFORE the attempt, for the reason note_lock_contention documents. It
        # precedes the WHOLE per-host loop, so contention arising at a later host can name
        # the holder seen before the first one; `holder or tools.lock_holder()` covers the
        # case where that sample was empty.
        # DECIDED: see tick.py's run_tick for why this samples every attempt rather than
        # only a losing one (#1085 item 4) -- same #1031 race, same tradeoff, same tests
        # (test_the_lock_holder_is_sampled_before_the_deploy_attempt in
        # tests/test_land_deploy.py).
        holder = t.lock_holder()
        started = t.clock()
        rc = deploy_by_host(ln)
        if rc != 75:
            break
        ln.note_lock_contention(int(t.clock() - started) + o.lock_backoff, holder)
        say(
            f"deploy lock busy (attempt {attempt}/{o.lock_retries}); retrying in {o.lock_backoff}s"
        )
        t.sleep(o.lock_backoff)
    return rc


def deploy_outcome(ln: Landing, rc: int) -> None:
    """Map deploy.sh's exit to a verdict; 0 returns."""
    pr, tags, o = ln.opts.pr, ln.tags, ln.opts
    if rc == 0:
        return
    if rc == 2:
        # deploy.sh refused the WHOLE list and deployed nothing, including every valid
        # service beside the bad tag. This read as nothing-to-deploy until 2026-08-29,
        # which is how PR #617 left 22 digest pins undeployed behind a green verdict.
        ln.ledger.cause = "tag-miss"
        ln.finish(
            "deploy-failed",
            1,
            f"PR #{pr} — a derived tag matched no service, so nothing deployed; tags: {tags}",
        )
    if rc == 75:
        ln.die(
            f"deploy lock stayed busy after {o.lock_retries} attempts — nothing deployed",
            75,
            "lock-busy",
        )
    if rc == 20:
        # The playbook RAN and a task failed: everything before it is live (issue #840).
        # Not a resume point; re-running it is not automatically safe.
        ln.ledger.cause = "playbook-failed"
        ln.finish(
            "deploy-failed",
            1,
            f"PR #{pr} — a playbook task failed AFTER applying; some changes are live; tags: {tags}",
        )
    ln.ledger.cause = f"deploy-exit-{rc}"
    ln.finish("deploy-failed", 1, f"PR #{pr}, exit {rc}")


def deploy_phase(ln: Landing) -> None:
    """Step 5, end to end: derive if needed, deploy, ride out a stale tree, stamp the ledger."""
    o, t = ln.opts, ln.tools
    print("== 5/6  deploying what the tick deferred")
    if ln.needs_diff:
        derive_from_diff(ln)
    if not ln.tags:
        no_tag_outcome(ln)
    ln.ledger.tags_label = ln.tags
    rc = deploy_with_lock_retry(ln)
    # 4 = the tree is behind origin/master: someone merged during the CI wait. The tick
    # crosses a tip only once master CI is green ON IT, so wait on the CURRENT tip -- every
    # attempt, not only when it moved -- after the blockers check, backed off by
    # `lock_backoff` the way the lock-contention retry above already is. Issue #1084: PR
    # #1051's landing retried this exit three times in ~25s with no backoff and, because the
    # wait used to be gated behind `tip_sha != merge_sha`, no CI wait either, while master CI
    # on the merge commit was still 2m48s from green (verdict=deploy-failed
    # cause=deploy-exit-4 tags=configarr). A landing that can never cross must not wait 15
    # minutes before saying so. Bounded: a third merge during the tip wait moves the tip
    # again.
    for attempt in range(1, o.stale_retries + 1):
        if rc != 4:
            break
        say(
            f"tree went stale mid-landing (exit 4); retrying in {o.lock_backoff}s "
            f"({attempt}/{o.stale_retries})"
        )
        t.sleep(o.lock_backoff)
        ln.fetch_branch()
        if ci.blockers(ln) == 3:
            ln.finish(
                "blocked",
                1,
                f"PR #{o.pr} — a change needing a hand landed during the wait; see above",
            )
        tip = ln.git("rev-parse", f"origin/{BRANCH}")
        if tip.returncode != 0:
            ln.die(f"could not read origin/{BRANCH}", 1)
        tip_sha = tip.stdout.strip()
        say(
            f"waiting for master CI on the tip {tip_sha} (the tick defers until it is green)"
        )
        started = t.clock()
        ci.wait_master_ci(ln, tip_sha, f"the tip {tip_sha}")
        # CI time, not deploy time: shift both later stamps so the board books it under
        # wait_ci with no new field to learn. Includes the backoff sleep above (mirrors
        # `deploy_with_lock_retry`'s own `+ o.lock_backoff`), or that time falls into
        # t_deploy instead -- the exact mis-attribution this comment exists to prevent.
        waited = t.clock() - started + o.lock_backoff
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
    deploy_outcome(ln, rc)
    ln.ledger.t_deploy = t.clock()
