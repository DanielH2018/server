"""Step 5: deploy what the tick deferred, one deploy.sh per host, riding out a stale tree.

A deploy reaches only the host the play runs against. A role only daniel-pi declares needs
`deploy.sh --tags <tag>` on daniel-pi: on daniel-box the tag matches no service and deploy.sh
exits 0. deploy_tags.py hosts says which host declares each tag. daniel-stage is never on the
list (HOSTS_LAND_SH_NEVER_DEPLOYS in scripts/lib/render_guard.py).
"""

from typing import NoReturn

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))  # scripts/
from lib.exit_codes import (
    DEPLOY_BROAD,
    DEPLOY_LOCK_BUSY,
    DEPLOY_LOCK_UNAVAILABLE,
    DEPLOY_LOCK_PLAN_FAILED,
    DEPLOY_NO_HOSTS,
    DEPLOY_OK,
    DEPLOY_PLAYBOOK_FAILED,
    DEPLOY_STALE,
    DEPLOY_TAG_MISS,
)
from deploy_tools.land_lib import land_platform
from deploy_tools.land_lib import ci, promotion, stale_retarget, tick
from deploy_tools.land_lib.landing import Landing, TickState, retry_while_locked
from deploy_tools.land_lib.outcome import (
    ABANDONED_WATCH_NOTE,
    Cause,
    Verdict,
    cause_for_deploy_exit,
    hold_clear_hint,
    host_not_waited_note,
    remaining_hosts_note,
    say,
)


def derive_from_diff(ln: Landing) -> None:
    """Resolve the fallback to a tag list HERE rather than handing deploy.sh --changed.

    deploy.sh resolves --changed internally, so the verdict call would receive an empty
    --tags and report settled having checked nothing -- on the large-PR path, where
    verification matters most.

    A range `changed` refuses as broad derives no tag here. Its k8s half is the promotion's,
    and land-wait's changed set names the tags this landing gates (D6.12). The deployer's
    `narrow` scoped such a range to k8s tags until then; Flux applies every one of them.
    """
    say(f"deriving tags from the diff since {ln.opts.since}")
    r = ln.tools.deploy_tags(ln.opts.primary, ["changed", ln.opts.since])
    if r.returncode == DEPLOY_BROAD:
        say(
            "`changed` refused the range as broad; the promotion's changed set names its k8s "
            "tags, and a Pi Compose role it reaches is a `deploy.sh -e target=daniel-pi` by hand"
        )
        ln.resolved_tags = []
        return
    if r.returncode != DEPLOY_OK:
        ln.die(f"deploy_tags.py changed failed (exit {r.returncode})", 1)
    # An empty element (a doubled or trailing comma) is dropped here rather than handed to
    # deploy.sh, which would refuse the whole list as a tag miss. Deliberate: the empty
    # element carries no service, so refusing on it is a false failure.
    ln.resolved_tags = [t for t in r.stdout.strip().split(",") if t]
    record_k8s_only(ln)


def record_k8s_only(ln: Landing) -> None:
    """Which of the FALLBACK derivation's tags the diff's own paths prove are k3s.

    `classify` fills `k8s_only` from the PR's file list, and a PR over `gh`'s 100-file page has
    no such list: `derive_from_diff` rebuilds the tags from `deploy_tags.py changed`, which
    prints tags and keeps no paths. With no proved platform, `wg-easy` routes to daniel-pi as
    well -- one extra ssh deploy of a Compose service the change never touched.

    The paths come from the range `changed` derived the tags from, `land_platform.diff_range`,
    read in the primary checkout because that is the tree `changed` read. Two things narrow the
    answer further:

    - INTERSECTED with `resolved_tags`, because `--since` bounds a range wider than this PR.
      A tag another session's merge in the range contributed would otherwise reach the
      `--k8s-only=` argv for a service this landing never deploys.
    Every failure leaves `k8s_only` empty and routes to every declaring host, which is the
    direction a wrong answer here must fall.
    """
    rng = land_platform.diff_range(ln.opts.since)
    r = ln.git("diff", "--name-only", rng)
    if r.returncode != 0:
        say(f"could not read the paths of {rng} — routing every tag to both platforms")
        return
    paths = [p for p in r.stdout.splitlines() if p.strip()]
    try:
        proved = ln.classifier.k8s_only_tags(paths, ln.classification.declared_set())
    except Exception as exc:
        say(f"platform not proved ({type(exc).__name__}) — routing to both platforms")
        return
    ln.k8s_only = sorted(set(proved) & set(ln.resolved_tags))
    if ln.k8s_only:
        say(
            f"the diff proves {','.join(ln.k8s_only)} is a k3s change; "
            "routing it to the cluster alone"
        )


def no_tag_outcome(ln: Landing, scope: str = "no service tag") -> NoReturn:
    """No service tag: owed to a hand, nothing at all, or the tick's own state decides.

    `scope` is why this landing has no tags to deploy, printed on the verdict line. The
    default is the ordinary case — the PR's own files derived none. `derive_from_diff` passes
    its own wording, because a refused broad derivation is not the same fact and an operator
    reading `reaches no service tag` about a 330-file PR goes looking for a different bug.
    """
    pr, sha = ln.opts.pr, ln.merge_sha
    if ln.plane:
        print(f"  it needs applying by hand: {ln.plane}")
        # Both remediations, because this arm ends the landing and the one at the foot of
        # this function never runs.
        if ln.classification.remaining_setup:
            print(remaining_hosts_note(ln.classification.remaining_setup))
        # The tick's own half too: a PR carrying BOTH ends here without ever reading the
        # deployer's state, so every tick state would go unreported. Reported,
        # not re-verdicted — `Landing.tick_half_open_lines` carries why.
        for line in ln.tick_half_open_lines():
            print(line)
        if ln.host_targets:  # this arm ends the landing before the host wait
            print(host_not_waited_note(ln.host_targets))
        ln.finish(
            Verdict.NEEDS_MANUAL_APPLY,
            1,
            f"PR #{pr} reaches {scope}, but is not done",
        )
    if not ln.classification.self_applied:
        if ln.classification.remaining_setup:
            # No plane, no tag, and nothing the tick applies -- and still a host owes a hand.
            # A PR whose only loud path is a repo file a setup role ships out of the checkout
            # (`scripts/deploy_tools/staging_gate_remote.sh` -> `/usr/local/bin/staging-gate-run`)
            # puts no role in the change set, so `self_applied` is False and this arm would
            # end the landing at `nothing-to-deploy` with the remediation computed and never
            # printed.
            print(remaining_hosts_note(ln.classification.remaining_setup))
            ln.finish(
                Verdict.NEEDS_MANUAL_APPLY,
                1,
                f"PR #{pr} reaches {scope}, but a setup role ships a changed repo file to "
                "hosts no tick applies",
            )
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
        hold = ln.state("hold_sha")
        print(
            f"  the deployer is holding {hold}: its apply failed — see the held planes in owed.jsonl and the gitops-deploy journal"
        )
        print(f"  {hold_clear_hint(hold)}")
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
    if not ln.tick_applied(sha):
        print(
            "  the tick crossed this PR but recorded no broad apply covering it "
            f"({ln.receipt_summary(sha)})"
        )
        # The receipt's `applied` is written by `handle_broad` only after the apply RETURNS, so a run
        # still applying reads exactly like a tick that converged without applying — and a
        # tick that already ff-merged this PR answers CONVERGED, never BEHIND, so the arm
        # above cannot catch it.
        if ln.tick_watch_abandoned:
            print(ABANDONED_WATCH_NOTE)
            ln.finish(
                Verdict.DEFERRED,
                75,
                f"PR #{pr}, {sha} — this run stopped watching a tick still applying, so "
                "whether it recorded an apply of this PR is not yet known",
            )
        for line in ln.tick_half_remediation():
            print(line)
        ln.finish(
            Verdict.NEEDS_MANUAL_APPLY,
            1,
            f"PR #{pr}, {sha} — the tick converged without recording an apply of this PR",
        )
    if ln.classification.remaining_setup:
        local = ln.tools.hostname()
        print(
            f"  applied on {local} only; it also reaches: {ln.classification.remaining_setup}"
        )
        ln.finish(
            Verdict.NEEDS_MANUAL_APPLY,
            1,
            f"PR #{pr}, {sha} — self-applied on {local} only; other hosts still need it",
        )
    ln.finish(
        Verdict.SETTLED,
        0,
        f"PR #{pr}, {sha} — {scope}; the tick applied it and converged with origin",
    )


def deploy_by_host(ln: Landing, at: str = "") -> int:
    """One deploy.sh per declaring host; the first non-zero exit. Retries resume at the failed host.

    `at` is handed to `deploy.sh --at`: the commit each run renders. Empty renders the primary
    checkout, which is what every retry after a stale tree does.

    The hosts come from the same tree the deploy renders. Under `at`, `containers_list` is read
    at that commit (`tools.landing_hosts_at`): a PR adding a Pi role and its entry together
    declares the role in no checkout until the tick fast-forwards, and the primary read would
    route it to no host, so the landing would run locally without
    `-e target=daniel-pi` and fail closed at the health gate. A ref that
    cannot be read falls back to `deploy_tags.py hosts` against the primary, whose own
    failure arm below stays the one that ends the landing. A tag under no host in either read
    falls through to one local deploy, which is right for a new cluster role.

    `k8s_only` routes to a `platform: k8s` entry only, through both reads. `wg-easy` is declared
    on daniel-box as k8s and on daniel-pi as Docker, so without it a change to either deploys both
    -- one extra ssh deploy of a service the change never touched. A tag the k8s caller graph
    reached names a k3s role, and so does one whose every changed path sits under
    `ansible/roles/k8s/`; `classify` records the union of the two.
    """
    o, t = ln.opts, ln.tools
    by_host = (
        t.landing_hosts_at(ln.resolved_tags, at, o.primary, ln.k8s_only) if at else None
    )
    if by_host is not None:
        lines = [
            f"{host}\t{','.join(host_tags)}" for host, host_tags in by_host.items()
        ]
    else:
        argv = ["hosts", ln.tags_csv]
        if ln.k8s_only:
            argv.append(f"--k8s-only={','.join(ln.k8s_only)}")
        r = t.deploy_tags(o.primary, argv)
        if r.returncode != DEPLOY_OK:
            # deploy.sh was never invoked for any host, so nothing here overlaps with the
            # catch-all in deploy_outcome, which really did run it.
            ln.ledger.cause = Cause.HOST_LOOKUP
            ln.die(
                "deploy_tags.py hosts failed before any deploy.sh ran; nothing was touched; "
                f"tags: {ln.tags_csv}",
                1,
                Verdict.DEPLOY_FAILED,
            )
        lines = [x for x in r.stdout.splitlines() if x.strip()]
    local = t.hostname()
    if not lines:
        if _skip_tick_applied(ln, local, ln.resolved_tags):
            return 0
        return t.deploy(
            o.primary, ln.resolved_tags, None, observe=ln.note_in_flock_wait, at=at
        )
    for line in lines:
        host, _, host_tags = line.partition("\t")
        if host in ln.deployed_hosts:
            continue
        tags = [x for x in host_tags.split(",") if x]
        target = None if host == local else host
        if target:
            say(
                f"{host_tags}: declared on {host}, deploying there with -e target={host}"
            )
        # Only the local host's tags: the tick's broad apply names no `-e target=`, so its
        # marker says nothing about what the Pi runs.
        elif _skip_tick_applied(ln, host, tags):
            ln.deployed_hosts.add(host)
            continue
        rc = t.deploy(o.primary, tags, target, observe=ln.note_in_flock_wait, at=at)
        if rc != DEPLOY_OK:
            return rc
        ln.deployed_hosts.add(host)
    return 0


def _skip_tick_applied(ln: Landing, host: str, tags: list[str]) -> bool:
    """Say so and return True when the tick's own apply already deployed `tags` on `host`.

    Step 5 exists for what the tick DEFERRED. A deploy-plane PR whose tick applied the whole
    play at the merge commit deferred nothing, and the deploy this step would run is the
    same render again -- paid for with a second wait on the tree lock. Step 7 still gates
    the tags and still reads the deployer's markers; only the `deploy.sh` run is skipped.
    """
    if not ln.tick_already_deployed(ln.merge_sha, tags):
        return False
    say(
        f"{','.join(tags)}: the tick already applied these on {host} "
        f"({ln.receipt_summary(ln.merge_sha)}); not deploying them again"
    )
    return True


def deploy_with_lock_retry(ln: Landing, at: str = "") -> int:
    """deploy_by_host, retried while the git-tree lock stays busy (exit 75)."""
    o = ln.opts
    return retry_while_locked(
        ln,
        DEPLOY_LOCK_BUSY,
        lambda: deploy_by_host(ln, at),
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
        # service beside the bad tag. That must not read as nothing-to-deploy.
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
    if rc == DEPLOY_LOCK_UNAVAILABLE:
        # Not contention, so `retry_while_locked` above did not retry it and retrying here
        # would not help either: flock failed on the lock FILE. Nothing was deployed, which
        # is what this message has to say.
        ln.ledger.cause = Cause.DEPLOY_EXIT_LOCK_UNAVAILABLE
        ln.finish(
            Verdict.DEPLOY_FAILED,
            1,
            f"PR #{pr} — the tree lock could not be taken at all, so nothing deployed; "
            f"tags: {tags}",
        )
    if rc == DEPLOY_NO_HOSTS:
        # The playbook matched no host and ansible exited 0; the wrapper read the empty PLAY
        # RECAP. Nothing was deployed, so the health gate would grade the OLD pods.
        ln.ledger.cause = Cause.DEPLOY_EXIT_NO_HOSTS
        ln.finish(
            Verdict.DEPLOY_FAILED,
            1,
            f"PR #{pr} — the playbook matched no host, so nothing deployed; tags: {tags}",
        )
    if rc == DEPLOY_LOCK_PLAN_FAILED:
        # `deploy_locks.py plan` did not print the service locks, so the wrapper took none
        # and started no playbook. Its own arm, ahead of the generic `exit {rc}` line below,
        # because "nothing deployed" is the fact the operator acts on.
        ln.ledger.cause = Cause.DEPLOY_EXIT_LOCK_PLAN_FAILED
        ln.finish(
            Verdict.DEPLOY_FAILED,
            1,
            f"PR #{pr} — deploy_locks.py plan gave the wrapper no lock list, so nothing "
            f"deployed; tags: {tags}",
        )
    if rc == DEPLOY_PLAYBOOK_FAILED:
        # The playbook RAN and a task failed: everything before it is live.
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
    """Step 5, end to end: derive, split, deploy, wait on promotion, stamp the ledger.

    The split comes before any deploy.sh (`promotion.split_owned`): a tag Flux applies leaves
    `resolved_tags`, and deploy.sh runs only when an Ansible-owned tag remains, which is a Pi
    Compose tag. A path-derived tag Flux applies is then dropped (`promotion.drop_path_derived`),
    because every landing whose PR may move the bundle waits on the promotion, and land-wait's
    changed set names the tags the gate reads (D6.12). A PR that moves no bundle file, or
    whose changed set names no tag, ends in `no_tag_outcome` when deploy.sh had nothing either.
    A plane it also carries, such as the rotation note for `secrets.yml`, is reported by the
    health step after the wait.
    """
    t = ln.tools
    if ln.needs_diff:
        derive_from_diff(ln)
    promotion.split_owned(ln)
    promotion.drop_path_derived(ln)
    waits = (
        bool(ln.owned_tags)
        or ln.needs_diff
        or promotion.moves_the_bundle(ln.classification.pr_paths)
    )
    if not ln.resolved_tags and not waits:
        if ln.host_targets and not ln.plane:
            return  # nothing for deploy.sh: the tick took the empty change set, then the host wait
        no_tag_outcome(ln)
    ln.ledger.tags_label = ",".join(ln.resolved_tags + list(ln.owned_tags))
    if ln.resolved_tags:
        _deploy_resolved(ln)
    elif not ln.tick_is_the_apply:
        # Nothing for deploy.sh, so the gate renders the merge commit from a snapshot, as
        # after `deploy.sh --at`, and the tick is kicked to converge the primary checkout.
        ln.deployed_at = ln.merge_sha
        tick.kick_tick(ln)
    # Stamped before the promotion wait: the re-gate reads a later deploy of these tags as one
    # that started after deploy.sh returned, and the wait can last 67 minutes, or longer while
    # an earlier commit's verification is in flight (`wait.py:IN_FLIGHT_WAIT_S`).
    ln.deploy_ended_at = t.wall_clock()
    if waits:
        promotion.wait_on_promotion(ln)
        ln.ledger.tags_label = ",".join(ln.resolved_tags + list(ln.owned_tags))
    # Step 5's own time on the board, the promotion wait included.
    ln.ledger.t_deploy = t.clock()
    if not ln.resolved_tags and not ln.owned_tags:
        if ln.host_targets and not ln.plane:
            return
        no_tag_outcome(ln)


def _deploy_resolved(ln: Landing) -> None:
    """deploy.sh for `resolved_tags`, riding out a stale tree; returns only when it deployed."""
    o, t = ln.opts, ln.tools
    # The merge commit, rendered from a snapshot of itself rather than from the primary
    # checkout. Step 4 does not wait for the tick to fast-forward that checkout, so nothing
    # before this point has moved it -- `--at` is what makes that safe.
    #
    # Unless the tick applies part of this PR itself, in which case step 4 DID await it and the
    # primary is the tree that tick fast-forwarded. Same predicate as `pipeline._step_tick`,
    # whose DECIDED note carries the reasoning; an empty `deployed_at` also keeps this landing
    # off the kick below, since the awaited tick is the one request it needs.
    if not ln.tick_is_the_apply:
        ln.deployed_at = ln.merge_sha
    rc = deploy_with_lock_retry(ln, at=ln.deployed_at)
    if rc == DEPLOY_STALE and ln.deployed_at:
        rc = stale_retarget.retarget(ln, rc, deploy_with_lock_retry)
    if rc == DEPLOY_STALE and ln.deployed_at:
        # DECIDED: exit 4 under `--at` means a commit MERGED AFTER the deployed one reaches
        # the same tags, and that newer landing owns the service. `stale_retarget.retarget`
        # has tried the newest green descendant, WAITING up to `ci_timeout` for a newer one
        # when that went stale too (#5517): each contains this PR (nothing reverted), its
        # master CI is green (a CI-gated tree), and the per-service lock still lets one
        # deploy of a service run at a time. What still holds of the old reasoning: the
        # primary's HEAD was never CI-gated, so it is no target. This arm is the path from
        # before `--at` (the tick fast-forwards the primary, then deploy from there), reached
        # when no green descendant exists, the wait ran out, or every retarget went stale;
        # the retry loop below is that path. More `stale_retries` cannot converge (#5486).
        say(
            f"a later commit reaches {ln.tags_csv}; falling back to the tick and the primary "
            "checkout"
        )
        ln.deployed_at = ""
        # The hosts already deployed at the merge commit are forgotten with it, so the retry
        # redeploys every host from the primary. One landing renders one tree: keeping them
        # would leave host A on the merge commit and host B on the primary, with a single gate
        # that can only render one of the two and would call the other settled unseen.
        ln.deployed_hosts.clear()
    elif ln.deployed_at:
        # DECIDED: the tick is kicked HERE, after deploy.sh has returned, not in step 4.
        # `gitops-deploy.service` wraps its whole unit run in the tree lock, so a tick kicked
        # before the deploy is one deploy.sh then queues behind inside its own `flock -w 3840`
        # -- the same wait, moved out of `tick=` and into `lock=`. Kicked after, it converges
        # the primary while this landing gates, and the gate's own snapshot needs no lock.
        # Not on the fallback arm: the retry loop runs a tick WITH its wait, and two requests
        # for the same work is one more than the path had before `--at`.
        tick.kick_tick(ln)
    # 4 = the tree is behind origin/master: someone merged during the CI wait. The tick
    # fast-forwards to the newest GREEN commit in the incoming range, not only to a green tip,
    # so what this landing needs green is its OWN merge commit -- wait on that, every attempt,
    # after the blockers check and backed off by `lock_backoff` the way the lock-contention
    # retry above already is. The wait normally returns at once, because step 3 already waited
    # on the same SHA; it is kept because a retry can reach here without step 3 having run.
    #
    # Waiting on the CURRENT TIP would chase a tip that moves again while the landing waits,
    # on merge commits whose own CI is already green; the ancestor walk avoids that.
    for attempt in range(1, o.stale_retries + 1):
        if rc != DEPLOY_STALE:
            break
        # The backoff DOUBLES each attempt (60s, 120s, 240s). A fixed 60s spends three
        # attempts inside ~4 minutes, which loses the same race against a merge rate of
        # roughly one every 2 minutes. Three attempts that cannot
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
        # t_deploy instead.
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
        ticked = t.clock()
        tick.run_tick(ln)
        # Into t_tick ALONE, so `tick=` on the board reads nonzero for a landing that
        # awaited a tick. The seconds come out of `deploy=`: step 4 stamps t_tick = t_ci for the
        # fast path, and the shift above moves both by the same amount, so without this the
        # wait here is invisible as a tick.
        ln.ledger.t_tick = (ln.ledger.t_tick or 0.0) + (t.clock() - ticked)
        rc = deploy_by_host(ln)
    if rc == DEPLOY_STALE:
        # Every retry lost the tip race. `deploy-failed` is the wrong word for it: exit 4 means
        # NOTHING was deployed and re-running is safe, while `deploy-failed` reads as "the
        # deploy broke" and sends a session looking at its own change. Exit 75, the resume-point
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
