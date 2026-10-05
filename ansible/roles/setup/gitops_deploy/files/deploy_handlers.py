#!/usr/bin/env python3
"""One function per terminal branch of a tick, each returning the process exit code.

`gitops_deploy.main()` picks exactly one of these after `deploy_phases.plan_tick`, in a
load-bearing order — broad before k8s, because a broad change and a promoted
image bump can arrive in the same range and the broad plane has to win. Everything a handler
needs is a parameter: the tick's `tools`, `state` and `config`, plus the `TickTarget` and
`TickPlan` the phases produced.

Almost every path returns 0. A failed deploy pages through Discord and the hold marker rather
than through the exit code; the `0 if posted else 1` branches are reached only when even the
failure alert could not be delivered, and 1 there is what leaves systemd's OnFailure unit as
the backstop.

Reach `deploy_io` and `deploy_alerts` qualified, never by from-import.
"""

import time

import deploy_alert_text
import deploy_alerts
import deploy_broad_k8s
import deploy_defer
import deploy_io
import deploy_locks
import deploy_narrow
from deploy_changes import setup_tags_for, setup_role_tag
from deploy_config import CHICAGO, Config, log
from deploy_git import (
    dirty_alert_slot,
    dirty_summary,
    should_alert_dirty,
)
from deploy_k8s import declares_snapshot_claims, rollback_volume_revert_note
from deploy_state import DeployerState
from deploy_tick_types import TickPlan, TickTarget
from deploy_toolbox import DeployTools

# The two local-time slots a dirty working tree pages in, at ~08:00 and ~20:00 CT. They are
# constants here rather than settings on `Config`: no config.env key sets them, and
# `handle_dirty` is the only reader. `deploy_git.dirty_alert_slot` turns them into the marker
# `state.read("dirty_alerted")` dedupes on.
DIRTY_ALERT_MORNING_HOUR = 8
DIRTY_ALERT_EVENING_HOUR = 20


def handle_dirty(
    tools: DeployTools, state: DeployerState, config: Config, target: TickTarget
) -> int:
    """A dirty working tree: log the paths every tick, page at most twice a day."""
    # Say so in the journal on EVERY tick, before the throttle. The Discord page is throttled to
    # twice a day, so between slots `journalctl -t gitops-deploy` was the only place left to look
    # and it said `-- No entries --` — indistinguishable from "ticked, nothing to do". On
    # 2026-08-30 one untracked file parked the primary checkout 7 commits behind for ~40 minutes,
    # and reading the empty journal is most of what that cost: every other signal (last_run fresh,
    # hold_sha empty, CI green, the unit exiting 0) was healthy, because a dirty skip IS healthy.
    #
    # `git status --porcelain` counts untracked files, so the tree can be dirty with nothing
    # modified — which is why the line names the paths rather than just the state. Unthrottled at
    # 48 lines/day only while parked, which is exactly when they are wanted.
    log(
        "working tree dirty — skipping (git status --porcelain counts untracked files): "
        + dirty_summary(target.status)
    )
    # Healthy skip (operator mid-edit). Throttle the page to twice a day at ~08:00 and ~20:00 CT
    # instead of every 30-min tick (see DIRTY_ALERT_FILE).
    now_ct = tools.now(CHICAGO)
    if should_alert_dirty(
        now_ct,
        state.read("dirty_alerted"),
        DIRTY_ALERT_MORNING_HOUR,
        DIRTY_ALERT_EVENING_HOUR,
    ):
        # Mark as alerted only on confirmed delivery, else retry next tick (see discord()).
        if deploy_alerts.discord(
            tools, config, deploy_alert_text.dirty_tree_alert(config.hostname)
        ):
            state.write(
                "dirty_alerted",
                dirty_alert_slot(
                    now_ct,
                    DIRTY_ALERT_MORNING_HOUR,
                    DIRTY_ALERT_EVENING_HOUR,
                ),
            )
    return 0


def handle_ci_failed(
    tools: DeployTools, state: DeployerState, config: Config, target: TickTarget
) -> int:
    """Master is red and no ancestor of it is green: stay on `local`, page once per SHA."""
    alert_red_tip(tools, state, config, target)
    log(f"origin {target.origin[:8]}: CI failed — not deploying")
    return 0


def alert_red_tip(
    tools: DeployTools, state: DeployerState, config: Config, target: TickTarget
) -> None:
    """Page once per SHA for a red master tip, whether or not this tick deployed past it.

    Two call sites, one marker: `handle_ci_failed` on a tick that stayed on `local`, `main()`
    on a tick that fast-forwarded to a green ancestor of the same red tip. Keyed on the TIP in
    both (`target.tip` is empty only on a hand-built target, where origin IS the tip).
    """
    red = target.tip or target.origin
    body = deploy_alert_text.ci_failed_alert(config.hostname, target.local, red)
    deploy_alerts.alert_once(tools, state, config, "ci", red, body)


def log_pi_changes(cs) -> None:
    """Name the Pi Docker work this tick merged and cannot apply, in the journal.

    Called from each handler right after its `git merge --ff-only`, because the claim the line
    makes is that the change is merged. The Pi has `has_gitops: false`, so no tick here ever
    deploys one of these; the operator does, with `-e target=daniel-pi`.

    Two shapes, one line each: a `roles/containers/<svc>/` change (`cs.services`) and a
    `roles/containers/common/` change (`cs.pi_shared`). Before #2836 only the
    `handle_no_services` path said either, so a Pi change sharing a tick with a promoted image
    bump or a broad plane was merged in silence.

    A contention arm below the call site resets the tree seconds later, which makes the line
    retroactively wrong on that path. Accepted rather than moved: `for_contention` logs its own
    reason, and the next tick re-crosses the range and says it again.
    """
    if cs.services:
        log(
            f"merged Docker role change(s) this host does not deploy: {sorted(cs.services)}"
        )
    if cs.pi_shared:
        log(
            "merged a roles/containers/common change this host does not deploy; "
            "apply it with `./scripts/deploy.sh -e target=daniel-pi`"
        )


def handle_broad(
    tools: DeployTools,
    state: DeployerState,
    config: Config,
    target: TickTarget,
    plan: TickPlan,
) -> int:
    """A change to a whole plane: defer it, or ff-merge and apply the playbook it names."""
    cs, origin = plan.cs, target.origin
    setup_tags = setup_tags_for(plan.paths)
    pending = deploy_defer.unapplyable_setup_roles(cs)
    # DECIDED: a bring-up playbook (and a setup path naming no role) still parks; a setup ROLE
    # this deployer cannot apply no longer does — it fast-forwards and leaves the durable
    # `manual_plane` marker instead. Parking held every other session's landing behind work
    # only a hand could do. `deploy_defer`'s module docstring carries the measurement.
    if deploy_defer.parks_the_tick(cs, setup_tags, pending):
        deploy_defer.park(tools, state, config, origin, cs)
        return 0

    # Everything else fast-forwards and applies what `deploy_narrow.plan` names. The
    # narrowing runs BEFORE the merge, so it reads the two refs this tick pinned.
    #
    # The ff-merge happens FIRST, before the apply, so an unrelated commit sharing this tick lands
    # even if the apply below fails. Stranding a docs-only commit behind somebody else's setup
    # change — a tick that exits 0, logs nothing, and writes behind_since — was the original
    # complaint this arm exists to fix.
    #
    # `applies` gates the narrowing: with no setup tag and no deploy-plane path there is
    # nothing for `deploy_narrow.plan` to plan, and asking it anyway would route a
    # `roles/setup/k3s/` range into `_deploy_plane` — whose refusal branch runs a full
    # `ansible/deploy.yml` for a change that reaches no container at all.
    applies = bool(setup_tags) or cs.broad_deploy
    # Role tag -> role directory, for the setup roles this tick applies ITSELF — the
    # complement of `pending`, which goes to `manual_plane` for a human.
    # `narrowed_setup_tags` keys on the tag because `setup_tags_for` has already mapped each
    # path to one, and `setup_role_tag` is not the identity: `chezmoi_setup` is tagged
    # `chezmoi`, so a map built from the directory name would ask the derivation about a role
    # that does not exist and refuse every time.
    appliable = {
        setup_role_tag(role): role for role in cs.setup_roles if role not in pending
    }
    plans = (
        deploy_narrow.plan(
            tools.narrow_deploy_plane,
            config,
            target,
            setup_tags,
            cs.broad_deploy,
            tools.narrow_setup_role,
            appliable,
            tools.digest_diff,
        )
        if applies
        else []
    )
    tools.run(["git", "merge", "--ff-only", origin], cwd=config.repo)
    log_pi_changes(cs)
    # Recorded at the ff-merge, which is the moment the role becomes merged-and-unapplied —
    # not after the apply below. A mixed range whose apply FAILS returns from the except arm,
    # and a record placed after it never ran: the role sat fast-forwarded on disk with no
    # marker, and once the operator fixed forward past the held SHA, `local..origin` no longer
    # carried that commit and this arm never saw the role again.
    # Kept: what this tick ADDED, so the contention arm can take exactly that back. A role a
    # previous tick already recorded keeps its first-seen stamp and is not in this list.
    recorded = (
        deploy_defer.record(tools, state, config, target, pending)
        if pending
        else deploy_defer.nothing_recorded()
    )
    # FORWARD-ONLY. deploy_logic.broad_budget_ok carries the argument and its 2026-08-29
    # re-derivation: at the 60min ceiling a full deploy.yml (1212s measured 2026-08-22) plus
    # a rollback re-run now fits, so the budget is no longer the reason — but a rollback
    # SIGTERMed partway is still worse than none, and funding one needs a fresh measurement
    # rather than the slack a ceiling raise left behind. On failure: hold, mark the plane, alert.
    #
    # It deliberately does NOT git-reset. Resetting without redeploying would leave the tree
    # claiming the old commit while live state is half-new — undiagnosable from the repo side,
    # where every check would read green against a tree that lies. hold_sha is what stops the
    # retry loop, and it does that whether or not the tree moved.
    #
    # One plan per plane, setup first, SHARING one `broad_deploy_timeout_s`: the unit's
    # TimeoutStartSec treats the broad arm as a single apply plus the flock wait, and a budget
    # per plan would put a mixed range past that ceiling. A failure holds the plane that
    # failed and leaves the earlier plane's marker standing — that apply happened. A busy
    # lock on the second plane resets the ff-merge like one on the first: the setup plane
    # is idempotent, and the next tick re-crosses the whole range.
    deadline = time.monotonic() + config.broad_deploy_timeout_s
    releases_before = deploy_narrow.releases_before(tools.release_records, plans)
    for broad in plans:
        playbook, tags = broad.playbook, broad.tags
        try:
            if broad.apply:
                deploy_io.deploy_broad(
                    config.repo, playbook, tags, max(1.0, deadline - time.monotonic())
                )
        except deploy_locks.ServiceLockBusy as exc:
            # Before the generic arm: nothing was applied, so this plane must not be held —
            # and the reset undoes the ff-merge, so the manual_plane lines this tick just
            # wrote describe a range that is no longer merged. Take them back with their page,
            # and with the receipt an EARLIER plan in this loop wrote (#2382).
            deploy_defer.unrecord(state, origin, recorded)
            return deploy_defer.for_contention(tools, state, config, target, exc)
        except Exception as exc:
            log(f"broad apply failed ({playbook} {tags}): {exc}")
            # `broad.held`, not `tags`: a narrowed setup apply holds each block tag with the
            # role it narrowed from, so that role's whole-role apply clears the hold. The
            # property carries the derivation.
            state.hold_failed_apply(origin, playbook, broad.held)
            # The range is merged and this arm never resets, so nothing re-derives what it
            # carried: the deferred pages go out now, and the failure post below names the
            # promoted bumps, which no later tick's range will contain.
            #
            # DECIDED: the secrets page rides the NON-CONTENTION exits — this arm, and the
            # two in `apply_broad_k8s` — rather than firing once before the loop (#2459).
            # #2383 put it before the loop so no failure arm could drop it, and that also
            # sent it on the contention arm, which resets the tree seconds later. Its text
            # says the range was fast-forwarded and names `ansible-playbook` as the remedy,
            # and both are false after a reset: the operator whose `deploy.sh` holds the lock
            # would have redeployed the OLD secret from a tree back on `local`, and the
            # dedupe marker then suppressed the page the re-merging tick owes. Sending it
            # from each exit that leaves the range merged keeps #2383's property — every one
            # of them is reached with `local == origin`, where no later tick re-evaluates.
            deploy_defer.alert_and_record_deferred(
                tools, state, config, origin, set(), cs, plan.k8s_services
            )
            deploy_alerts.alert_secrets_deferred(tools, state, config, origin, cs)
            posted = deploy_alerts.discord(
                tools,
                config,
                deploy_alert_text.broad_failure_alert(
                    config.hostname,
                    playbook,
                    tags,
                    origin,
                    exc,
                    cs.k8s_deploy,
                ),
            )
            # Exit 0 on a delivered detailed post so systemd's OnFailure generic curl doesn't
            # double-page; exit 1 only if the post failed, leaving OnFailure the backstop.
            return 0 if posted else 1

        # Recorded BEFORE the hold is cleared: `clear_broad_hold` may keep a hold naming a
        # DIFFERENT plane, and this apply still happened. `land.sh` reads the receipt's
        # `applied` half to tell a plane the tick applied from one it merely fast-forwarded
        # past — `behind_since` empty cannot (issue #1537). Written per plan and on nothing
        # else: a range whose whole broad half is a role this deployer cannot apply has no
        # plan, so no receipt says an apply happened — #1537's failure, in reverse.
        deploy_narrow.log_applied_shadow(
            tools.release_records, tools.applied_diff, releases_before, origin, broad
        )
        state.record_receipt(origin, target.local, applied={playbook: tags})
        state.clear_broad_hold(playbook, tags)
        deploy_defer.clear_applied(state, playbook, tags)
    # After the loop, so a broad apply that failed or hit a busy lock has already returned:
    # the k8s half rides on the plane below it, and applying a workload onto a host whose
    # setup plane did not apply is the ordering `main()`'s broad-before-k8s branch exists to
    # prevent. It shares the same `deadline`, for the reason the plans share it, and it sends
    # the deferred-change pages on every path but contention.
    return deploy_broad_k8s.apply_broad_k8s(
        tools, state, config, target, plan, cs, plans, recorded, deadline
    )


def handle_k8s(
    tools: DeployTools,
    state: DeployerState,
    config: Config,
    target: TickTarget,
    plan: TickPlan,
) -> int:
    """The promoted k8s image bumps: ff-merge, deploy, roll back on failure."""
    cs, origin = plan.cs, target.origin
    tools.run(["git", "merge", "--ff-only", origin], cwd=config.repo)
    log_pi_changes(cs)
    try:
        deploy_io.deploy_k8s(config.repo, cs.k8s_deploy, config.k8s_deploy_timeout_s)
    except deploy_locks.ServiceLockBusy as exc:
        # Before the rollback arm: a rollback would revert volumes and redeploy the prior pin
        # over a cluster this tick never touched.
        return deploy_defer.for_contention(tools, state, config, target, exc)
    except Exception as exc:
        return _rollback_k8s(tools, state, config, target, plan, exc)
    # One of the two places a service hold clears (the broad arm's bump path is the other).
    # Without it the first rollback would leave GitOps Deploy — Status red until a manual rm.
    state.clear_service_hold(cs.k8s_deploy)
    # A bump an earlier broad tick deferred for budget is applied by its own later deploy,
    # which is the ordinary way out of the marker (#2449).
    deploy_defer.clear_applied_k8s_deferred(state, cs.k8s_deploy)
    # Only after the gate inside deploy_k8s has passed and the hold is cleared — annotating
    # from inside the try would mark a deploy that the rollout gate went on to reject.
    tools.emit_deploy_annotation(cs.k8s_deploy, origin)
    # A promoted k8s service is image-bump-only, so it is never the consumer of a secret that
    # rode along in the same tick. Without this the rotated value is ff-merged and forgotten.
    deploy_alerts.alert_secrets_deferred(tools, state, config, origin, cs)
    deploy_defer.alert_and_record_deferred(
        tools, state, config, origin, cs.k8s_deploy, cs, plan.k8s_services
    )
    return 0


def _rollback_k8s(
    tools: DeployTools,
    state: DeployerState,
    config: Config,
    target: TickTarget,
    plan: TickPlan,
    exc: Exception,
) -> int:
    """Undo a failed k8s deploy: hold, reset, redeploy the prior pin, revert claimed volumes."""
    cs, local, origin = plan.cs, target.local, target.origin
    # Hold BEFORE the reset: a hung rollback redeploy would otherwise
    # be SIGTERMed before the marker is written, stranding the bad commit into a per-tick
    # redeploy loop.
    log(f"k8s deploy failed for {sorted(cs.k8s_deploy)}: {exc}; rolling back")
    state.write_hold(origin)
    tools.run(["git", "reset", "--hard", local], cwd=config.repo)
    rollback_failed: Exception | None = None
    try:
        # `origin`, not `local`: the tree is already reset to the last-good commit, so the
        # snapshot worth reverting to is the one taken before the FAILED deploy — named for
        # `origin`, the commit being rolled back FROM. Passing `local` looks right and is wrong
        # twice over: on a first rollback it finds no snapshot and fails the deploy, and on a
        # second rollback of the same service it finds a STALE snapshot and reverts to the wrong
        # point.
        # DECIDED: `origin[:8]` is a fixed slice while volume-snapshot names with `--short=8`, a
        # MINIMUM width. They diverge only when 8 chars collide, and then the prefix misses by
        # one character and volume-revert's no-snapshot assert fires before the scale-down — the
        # safe failure. Measured zero ambiguous 8-char prefixes across ~39k objects. Full
        # analysis in this role's CLAUDE.md; two reviewers re-derived it on 2026-08-22, hence the
        # marker.
        deploy_io.deploy_k8s(
            config.repo,
            cs.k8s_deploy,
            config.k8s_rollback_timeout_s,
            restore_sha=origin[:8],
        )
    except Exception as exc2:
        rollback_failed = exc2
        log(f"k8s rollback redeploy of the prior version also failed: {exc2}")
    # Read from the tree AFTER the reset above, matching what roles/k8s/manifests itself reads
    # for the claim list — the failed commit may have added or renamed a claim, and that version
    # is exactly what must NOT decide this note.
    reverting = frozenset(
        svc
        for svc in cs.k8s_deploy
        if declares_snapshot_claims(deploy_io.read_local_k8s_default(config.repo, svc))
    )
    revert_note = rollback_volume_revert_note(
        cs.k8s_deploy,
        reverting,
        str(rollback_failed) if rollback_failed else None,
    )
    posted = deploy_alerts.discord(
        tools,
        config,
        deploy_alert_text.k8s_failure_alert(
            config.hostname, local, origin, cs.k8s_deploy, exc, revert_note
        ),
    )
    return 0 if posted else 1


def handle_no_services(
    tools: DeployTools,
    state: DeployerState,
    config: Config,
    target: TickTarget,
    plan: TickPlan,
) -> int:
    """Nothing maps to a deploy here: ff-merge, then flag what rode along unapplied.

    A Pi Docker role change (`cs.services`, `cs.pi_shared`) lands here too. No has_gitops host runs Docker, so
    the deployer never applied one; it is deployed by hand with `-e target=daniel-pi`.
    """
    cs, origin = plan.cs, target.origin
    tools.run(["git", "merge", "--ff-only", origin], cwd=config.repo)  # docs-only etc.
    log_pi_changes(cs)
    # A secrets-only push (rotated value, no service template changed) maps to nothing, so the
    # ff-merge above is all we can do automatically — but the new value only reaches a container
    # on its next deploy. Defer-and-alert (once per SHA) so the operator redeploys the
    # consumer(s); without this the rotated secret sits stale.
    deploy_alerts.alert_secrets_deferred(tools, state, config, origin, cs)
    # tasks/ changes aren't auto-deployed but DO change what a deploy does, so they must not
    # sit silently ff-merged. Nothing was deployed this tick (deployed=set()), so
    # the full sets are flagged. Same helper runs on the deploy path for a combined push.
    deploy_defer.alert_and_record_deferred(
        tools, state, config, origin, set(), cs, plan.k8s_services
    )
    return 0
