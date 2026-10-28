#!/usr/bin/env python3
"""One function per terminal branch of a tick, each returning the process exit code.

`gitops_deploy.main()` picks exactly one of these after `deploy_phases.plan_tick`. Everything a
handler needs is a parameter: the tick's `tools`, `state` and `config`, plus the `TickTarget` and
`TickPlan` the phases produced.

Almost every path returns 0. A failed deploy pages through Discord and the hold marker rather
than through the exit code; the `0 if posted else 1` branches are reached only when even the
failure alert could not be delivered, and 1 there is what leaves systemd's OnFailure unit as
the backstop.

Reach `deploy_io` and `deploy_alerts` qualified, never by from-import.
"""

import time

from deployer_lib import deploy_alert_text
from deployer_lib import deploy_alerts
from deployer_lib import deploy_defer
from deployer_lib import deploy_io
from deployer_lib import deploy_locks
from deployer_lib import deploy_narrow
from deployer_lib.deploy_changes import setup_tags_for, setup_role_tag
from deployer_lib.deploy_config import CHICAGO, ERR, WARNING, Config, log
from deployer_lib.deploy_git import (
    dirty_alert_slot,
    dirty_summary,
    should_alert_dirty,
)
from deployer_lib.deploy_state import DeployerState
from deployer_lib.deploy_tick_types import TickPlan, TickTarget
from deployer_lib.deploy_toolbox import DeployTools

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
        "tick: working tree dirty — skipping (git status --porcelain counts untracked files): "
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
    log(f"ci: origin {target.origin[:8]} failed — not deploying", level=WARNING)
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
    `handle_no_services` path said either, so a Pi change sharing a tick with a broad plane
    was merged in silence.

    A contention arm below the call site resets the tree seconds later, which makes the line
    retroactively wrong on that path. Accepted rather than moved: `for_contention` logs its own
    reason, and the next tick re-crosses the range and says it again.
    """
    if cs.services:
        log(
            f"docker: merged Docker role change(s) this host does not deploy: {sorted(cs.services)}"
        )
    if cs.pi_shared:
        log(
            "docker: merged a roles/containers/common change this host does not deploy; "
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
    setup_tags = setup_tags_for(plan.paths) | deploy_defer.secret_reader_tags(cs)
    pending = deploy_defer.unapplyable_setup_roles(cs)
    # DECIDED: a bring-up playbook (and a setup path naming no role) still parks; a setup ROLE
    # this deployer cannot apply no longer does — it fast-forwards and leaves the durable
    # `manual_plane` marker instead. Parking held every other session's landing behind work
    # only a hand could do. `deploy_defer`'s module docstring carries the measurement.
    # One exception narrows this (#4326): a range carrying a setup role parks while the
    # routing derivation failed or timed out, because that is transient and the next tick
    # retries. A deleted role is dropped and an unplaced one is recorded; neither parks.
    if deploy_defer.parks_the_tick(cs, setup_tags, pending):
        deploy_defer.park(tools, state, config, origin, cs)
        tools.note_outcome("parked")
        return 0

    # Everything else fast-forwards and applies what `deploy_narrow.plan` names: the setup
    # roles this host runs, narrowed to their own block tags. The narrowing runs BEFORE the
    # merge, so it reads the two refs this tick pinned. A deploy-plane path adds no plan, so
    # the tick fast-forwards past it as it passes a docs-only one (D6.12): the renderer reads
    # it, and `promote` carries its cluster half.
    #
    # The ff-merge happens FIRST, before the apply, so an unrelated commit sharing this tick lands
    # even if the apply below fails. Stranding a docs-only commit behind somebody else's setup
    # change — a tick that exits 0, logs nothing, and writes behind_since — was the original
    # complaint this arm exists to fix.
    #
    # Role tag -> role directory, for the setup roles this tick applies ITSELF — the
    # complement of `pending`, which goes to `manual_plane` for a human.
    # `narrowed_setup_tags` keys on the tag because `setup_tags_for` has already mapped each
    # path to one, and `setup_role_tag` is not the identity: `chezmoi_setup` is tagged
    # `chezmoi`, so a map built from the directory name would ask the derivation about a role
    # that does not exist and refuse every time.
    appliable = {
        setup_role_tag(role): role for role in cs.setup_roles if role not in pending
    }
    plans = deploy_narrow.plan(
        config, target, setup_tags, tools.narrow_setup_role, appliable
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
    # The setup plan runs inside one `broad_deploy_timeout_s`: the unit's TimeoutStartSec
    # treats the broad arm as a single apply plus the flock wait.
    deadline = time.monotonic() + config.broad_deploy_timeout_s
    for broad in plans:
        playbook, tags = broad.playbook, broad.tags
        try:
            if broad.apply:
                deploy_io.deploy_broad(
                    config.repo,
                    playbook,
                    tags,
                    max(1.0, deadline - time.monotonic()),
                    run=tools.run,
                )
        except deploy_locks.ServiceLockBusy as exc:
            # Before the generic arm: nothing was applied, so this plane must not be held —
            # and the reset undoes the ff-merge, so the manual_plane lines this tick just
            # wrote describe a range that is no longer merged. Take them back with their page
            # and their receipt (#2382).
            deploy_defer.unrecord(state, origin, recorded)
            return deploy_defer.for_contention(tools, state, config, target, exc)
        except Exception as exc:
            log(f"broad: apply failed ({playbook} {tags}): {exc}", level=ERR)
            # `broad.held`, not `tags`: a narrowed setup apply holds each block tag with the
            # role it narrowed from, so that role's whole-role apply clears the hold. The
            # property carries the derivation.
            state.hold_failed_apply(origin, playbook, broad.held)
            # The range is merged and this arm never resets, so nothing re-derives what it
            # carried: the deferred pages go out now.
            #
            # DECIDED: the secrets page rides the NON-CONTENTION exits — this arm, and the
            # tail of this function — rather than firing once before the loop (#2459).
            # #2383 put it before the loop so no failure arm could drop it, and that also
            # sent it on the contention arm, which resets the tree seconds later. Its text
            # says the range was fast-forwarded and names `ansible-playbook` as the remedy,
            # and both are false after a reset: the operator whose `deploy.sh` holds the lock
            # would have redeployed the OLD secret from a tree back on `local`, and the
            # dedupe marker then suppressed the page the re-merging tick owes. Sending it
            # from each exit that leaves the range merged keeps #2383's property — every one
            # of them is reached with `local == origin`, where no later tick re-evaluates.
            deploy_alerts.alert_deferred(tools, state, config, origin, set(), cs)
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
        state.record_receipt(origin, target.local, applied={playbook: tags})
        state.clear_broad_hold(playbook, tags)
        deploy_defer.clear_applied(state, playbook, tags)
    # The tick's last exit, reached only with `local == origin`, as the failure arm is. A
    # range with nothing deferred reaches only this exit, so the two pages are here as well
    # as on the failure arm (#2459). Nothing was redeployed, so `deployed` is empty.
    deploy_alerts.alert_deferred(tools, state, config, origin, set(), cs)
    deploy_alerts.alert_secrets_deferred(tools, state, config, origin, cs)
    return 0


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
    # sit silently ff-merged. Nothing was deployed this tick, so `deployed` is empty.
    deploy_alerts.alert_deferred(tools, state, config, origin, set(), cs)
    return 0
