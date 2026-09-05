#!/usr/bin/env python3
"""One function per terminal branch of a tick, each returning the process exit code.

`gitops_deploy.main()` picks exactly one of these after `deploy_phases.plan_tick`, in a
load-bearing order — broad before k8s before Docker, because a broad change and a promoted
image bump can arrive in the same range and the broad plane has to win. Everything a handler
needs is a parameter: the tick's `tools`, `state` and `config`, plus the `TickTarget` and
`TickPlan` the phases produced.

Almost every path returns 0. A failed deploy pages through Discord and the hold marker rather
than through the exit code; the `0 if posted else 1` branches are reached only when even the
failure alert could not be delivered, and 1 there is what leaves systemd's OnFailure unit as
the backstop.

The staging gate's I/O shell sits at the bottom of this file — `consult_staging`,
`record_staging_tick` and `consume_staging_override` — rather than beside the pure verdict it
calls. `handle_k8s` is their only production caller, and `deploy_staging.py` has to stay
import-pure: `deploy_logic.py` re-exports it to three tools in `scripts/deploy_tools/` that
import with only this directory on `sys.path`, so an `import deploy_io` there reaches
`host_lib` and breaks `land.sh`.

Reach `deploy_io` and `deploy_alerts` qualified, never by from-import.
"""

import time

import deploy_alerts
import deploy_io
from deploy_changes import setup_tags_for
from deploy_config import CHICAGO, Config, log
from deploy_git import (
    dirty_alert_slot,
    dirty_summary,
    hold_plane_marker,
    should_alert_dirty,
)
from deploy_health import gate_services
from deploy_k8s import declares_snapshot_claims, rollback_volume_revert_note
from deploy_remediation import broad_remediation
from deploy_staging import (
    STAGING_SKIPPED,
    staging_blocks,
    staging_scope,
    staging_tick_outcome,
    staging_verdict,
    staging_verdict_summary,
)
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
            tools, config, deploy_alerts.dirty_tree_alert(config.hostname)
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
    """Master is red: stay on `local`, page once per SHA."""
    deploy_alerts.alert_once(
        tools,
        state,
        config,
        "ci_alerted",
        "ci",
        target.origin,
        deploy_alerts.ci_failed_alert(config.hostname, target.local, target.origin),
    )
    log(f"origin {target.origin[:8]}: CI failed — not deploying")
    return 0


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
    # The MANUAL subset keeps the old behaviour exactly: defer, alert, and do NOT ff-merge.
    # Staying parked is what keeps `behind_since` set, and that marker is the only durable signal
    # that an unapplied plane exists — ff-merging here would clear it and leave the host green
    # while running a plane it never applied.
    #
    # A setup-plane change whose tag cannot be derived joins them: an unresolvable tag means the
    # only automatic option is an UNSCOPED initial_setup.yml, which is a whole-host reprovision
    # rather than the scoped apply this arm is funded for.
    if cs.broad_manual or (cs.broad_setup and not setup_tags):
        # Broad-manual doesn't ff-merge, so it re-evals next tick — the per-SHA marker (inside
        # alert_once) stops a re-queue while the pending queue owns redelivery. Name the RIGHT
        # playbook per plane: deploy.yml applies only container roles, so a setup-plane change
        # needs initial_setup.yml (2026-07-16 review M1).
        deploy_alerts.alert_once(
            tools,
            state,
            config,
            "broad_alerted",
            "broad",
            origin,
            deploy_alerts.broad_deferred_alert(
                origin,
                broad_remediation(
                    cs.broad_deploy, cs.broad_setup, cs.setup_roles, config.branch
                ),
            ),
        )
        return 0

    # Everything else fast-forwards and applies itself.
    #
    # The ff-merge happens FIRST, before the apply, so an unrelated commit sharing this tick lands
    # even if the apply below fails. Stranding a docs-only commit behind somebody else's setup
    # change — a tick that exits 0, logs nothing, and writes behind_since — was the original
    # complaint this arm exists to fix.
    tools.run(["git", "merge", "--ff-only", origin], cwd=config.repo)

    if setup_tags:
        playbook, tags = "ansible/initial_setup.yml", sorted(setup_tags)
    else:
        playbook, tags = "ansible/deploy.yml", []

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
    try:
        deploy_io.deploy_broad(
            config.repo, playbook, tags, config.broad_deploy_timeout_s
        )
    except Exception as exc:
        log(f"broad apply failed ({playbook} {tags}): {exc}")
        state.write_hold(origin)
        state.write("hold_plane", hold_plane_marker(playbook, tags))
        posted = deploy_alerts.discord(
            tools,
            config,
            deploy_alerts.broad_failure_alert(
                config.hostname,
                playbook,
                tags,
                origin,
                exc,
                state.path("hold"),
                state.path("hold_plane"),
            ),
        )
        # Exit 0 on a delivered detailed post so systemd's OnFailure generic curl doesn't
        # double-page; exit 1 only if the post failed, leaving OnFailure the backstop.
        return 0 if posted else 1

    state.clear_broad_hold(playbook, tags)
    deploy_alerts.alert_secrets_deferred(tools, state, config, origin, cs)
    deploy_alerts.alert_deferred(
        tools, state, config, origin, set(), cs, plan.k8s_services
    )
    return 0


def handle_k8s(
    tools: DeployTools,
    state: DeployerState,
    config: Config,
    target: TickTarget,
    plan: TickPlan,
) -> int:
    """The promoted k8s image bumps: consult staging, ff-merge, deploy, roll back on failure."""
    cs, local, origin = plan.cs, target.local, target.origin
    # DECIDED: consult the gate BEFORE the ff-merge, never after. consult_staging blocks for up
    # to STAGING_GATE_TIMEOUT_S + STAGING_EXPECT_TIMEOUT_S, and a process death inside that
    # window used to leave local == origin with nothing deployed — next_action() then returns
    # noop forever (the SHA is already merged, so nothing re-triggers), `last_run` keeps ticking,
    # and both Kuma tiles stay green over a permanently stranded deploy. Merging after the gate
    # makes the same death self-healing: local is still behind, so the next tick re-evaluates.
    # Whether the verdict blocks is staging_blocks' decision; while config.staging_gate_blocking is
    # false it never does, and this branch is the slice-3 behaviour unchanged.
    verdict = consult_staging(tools, state, config, cs.k8s_deploy, origin)
    if staging_blocks(verdict, blocking=config.staging_gate_blocking):
        if consume_staging_override(state):
            deploy_alerts.discord(
                tools,
                config,
                deploy_alerts.staging_override_alert(
                    config.hostname, origin, state.path("staging_override")
                ),
            )
            log(f"staging rejected {origin[:8]}; override armed, deploying prod anyway")
        else:
            # No reset and no volume revert: consult_staging runs BEFORE the ff-merge, so the
            # tree is still on `local` and prod was never applied. That asymmetry is Phase C's
            # main prize — a staging failure costs nothing to undo. Do not add a reset here
            # without also moving the gate, or the two will disagree.
            state.write_hold(origin)
            posted = deploy_alerts.discord(
                tools,
                config,
                deploy_alerts.staging_rejected_alert(
                    config.hostname,
                    local,
                    origin,
                    cs.k8s_deploy,
                    state.path("staging_override"),
                ),
            )
            log(f"staging rejected {origin[:8]}; holding, prod not deployed")
            return 0 if posted else 1
    tools.run(["git", "merge", "--ff-only", origin], cwd=config.repo)
    try:
        deploy_io.deploy_k8s(config.repo, cs.k8s_deploy, config.k8s_deploy_timeout_s)
    except Exception as exc:
        return _rollback_k8s(tools, state, config, target, plan, exc)
    # The ONLY place a hold can clear on an all-k8s host. state.write_hold(None) otherwise lives
    # solely in the Docker health-gate branch below, which such a host never reaches — so
    # without this the first rollback would leave GitOps Deploy — Status red forever and
    # need a manual rm (the trap this role's CLAUDE.md documents).
    state.clear_service_hold()
    # Only after the gate inside deploy_k8s has passed and the hold is cleared — annotating
    # from inside the try would mark a deploy that the rollout gate went on to reject.
    tools.emit_deploy_annotation(cs.k8s_deploy, origin)
    # A promoted k8s service is image-bump-only, so it is never the consumer of a secret that
    # rode along in the same tick. Without this the rotated value is ff-merged and forgotten.
    deploy_alerts.alert_secrets_deferred(tools, state, config, origin, cs)
    deploy_alerts.alert_deferred(
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
    # Hold BEFORE the reset, same as the Docker paths: a hung rollback redeploy would otherwise
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
        deploy_alerts.k8s_failure_alert(
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
    """Nothing maps to a deploy here: ff-merge, then flag what rode along unapplied."""
    cs, origin = plan.cs, target.origin
    tools.run(["git", "merge", "--ff-only", origin], cwd=config.repo)  # docs-only etc.
    # A secrets-only push (rotated value, no service template changed) maps to nothing, so the
    # ff-merge above is all we can do automatically — but the new value only reaches a container
    # on its next deploy. Defer-and-alert (once per SHA) so the operator redeploys the
    # consumer(s); without this the rotated secret sits stale.
    deploy_alerts.alert_secrets_deferred(tools, state, config, origin, cs)
    # tasks/ and meta/deps.yml changes aren't auto-deployed but DO change what a deploy does, so
    # they must not sit silently ff-merged. Nothing was deployed this tick (deployed=set()), so
    # the full sets are flagged. Same helper runs on the deploy path for a combined push.
    deploy_alerts.alert_deferred(
        tools, state, config, origin, set(), cs, plan.k8s_services
    )
    return 0


def handle_docker(
    tools: DeployTools,
    state: DeployerState,
    config: Config,
    target: TickTarget,
    plan: TickPlan,
) -> int:
    """Deploy this host's Docker services, health-gate them, and roll back if the gate fails."""
    cs, local, origin = plan.cs, target.local, target.origin
    tools.run(["git", "merge", "--ff-only", origin], cwd=config.repo)
    try:
        deploy_io.deploy(config.repo, cs.services)
    except Exception as exc:
        # Deploy-EXECUTION failure (ansible-playbook itself errored: bad image manifest, a failed
        # task) — distinct from the health gate below. Without this the exception propagates to
        # entrypoint(), which alerts but re-raises WITHOUT writing last_run AND leaves the repo
        # ff-merged at the bad commit with no hold + no rollback — so the next tick (local==origin)
        # noops and the deployer silently parks on the broken commit. Mirror the health-gate
        # rollback: reset to the prior HEAD, redeploy the prior (known-good) version (ansible is
        # idempotent, so re-applying old after a partial run is safe), hold the bad SHA, and alert.
        log(
            f"deploy execution failed for {sorted(cs.services)}: {exc}; rolling back to {local[:8]}"
        )
        # Hold BEFORE the reset + rollback redeploy. deploy() is unbounded (timeout=None) with no
        # SIGTERM handler, so if the rollback redeploy HANGS (wedged docker daemon, stalled pull)
        # systemd SIGTERMs at TimeoutStartSec before a trailing write_hold could run — leaving no
        # marker, origin still ahead, and the next tick re-merging + redeploying the same bad commit
        # in a per-tick loop. Holding first makes the next tick skip_hold even if we're killed
        # mid-rollback. (A catchable raise below is already handled; this covers the kill/hang.)
        state.write_hold(origin)
        tools.run(["git", "reset", "--hard", local], cwd=config.repo)
        try:
            deploy_io.deploy(config.repo, cs.services)
        except Exception as exc2:
            log(f"rollback redeploy of the prior version also failed: {exc2}")
        posted = deploy_alerts.discord(
            tools,
            config,
            deploy_alerts.deploy_failure_alert(
                config.hostname, local, origin, cs.services, exc
            ),
        )
        # A rollback already surfaces via THIS detailed post + the GitOps Deploy — Status monitor
        # (hold_sha). Exit 0 when the detailed post was delivered so systemd's
        # OnFailure=gitops-deploy-alert.service (a GENERIC "unit failed" curl) doesn't ALSO fire — one
        # detailed page, not a duplicate. Only if the detailed post failed (Cloudflare-1010/webhook
        # down) exit 1, so OnFailure is the guaranteed backstop. last_run is written either way (the
        # tick completed; the deployer is alive — GitOps-Alive stays green, Status carries the hold).
        return 0 if posted else 1

    # Health-gate only services actually deployed on THIS host. A changed template for an
    # other-host-only service (dozzle is daniel-pi-only) renders no compose here, so
    # containers_for() returns [] and service_healthy() is vacuously true — without this the gate
    # would poll a phantom container to timeout and trigger a false rollback. (deploy(cs.services)
    # above is a harmless no-op for those tags.)
    skipped = sorted(
        s for s in cs.services if not deploy_io.containers_for(config.repo, s)
    )
    if skipped:
        log(f"not deployed on this host; skipping health gate: {skipped}")
    # Budget the gate so gate+rollback finishes inside the unit's TimeoutStartSec (see
    # config.run_budget_s): once the deadline passes, gate_services marks the rest failed and we roll
    # back, rather than polling to HEALTH_TIMEOUT_S per container and getting SIGTERMed mid-gate
    # (which would strand the bad commit live). tools.run_start is measured from process start.
    gate_deadline = tools.run_start + config.run_budget_s
    failed = gate_services(
        cs.services,
        lambda svc, deadline: tools.service_healthy(
            config.repo, svc, config.health_timeout_s, deadline
        ),
        gate_deadline,
        time.time,
    )
    if not failed:
        state.clear_service_hold()
        # Combined-push safety: a tasks/ or meta/deps.yml change bundled for a service OTHER than
        # the one(s) just deployed is ff-merged but unapplied — flag that remainder (a bundled
        # change to a DEPLOYED service rode its own --tags redeploy, so it's excluded). Only on a
        # clean deploy: a rollback below git-resets the whole commit, reverting those changes too.
        deploy_alerts.alert_deferred(
            tools, state, config, origin, cs.services, cs, plan.k8s_services
        )
        return 0
    if time.time() >= gate_deadline:
        log(
            f"health-gate budget ({config.run_budget_s}s) exhausted before gating completed"
        )

    # Rollback: reset to prior HEAD, redeploy the prior version. Redeploy the WHOLE batch
    # (cs.services), not just `failed`: in a multi-service tick the services that DID pass
    # were recreated on the new images, so after the git reset they'd otherwise stay on the
    # new images while the tree points at old — partial-batch drift. Hold BEFORE the reset +
    # redeploy (see the exec-failure path above): a hung rollback redeploy would otherwise be
    # SIGTERMed before write_hold, stranding the bad commit into a per-tick redeploy loop.
    log(f"health gate failed for {failed}; rolling back to {local[:8]}")
    state.write_hold(origin)
    tools.run(["git", "reset", "--hard", local], cwd=config.repo)
    try:
        deploy_io.deploy(config.repo, cs.services)
    except Exception as exc:
        log(f"rollback redeploy of the prior version also failed: {exc}")
    posted = deploy_alerts.discord(
        tools,
        config,
        deploy_alerts.rollback_alert(config.hostname, local, origin, failed),
    )
    # Exit 0 on a delivered detailed post so OnFailure's generic curl doesn't double-page (see the
    # exec-failure path above); exit 1 only if the detailed post failed, leaving OnFailure the backstop.
    return 0 if posted else 1


# ── the staging gate's I/O shell ─────────────────────────────────────────────────────────


def record_staging_tick(
    tools: DeployTools,
    state: DeployerState,
    sha: str,
    gated: set[str],
    verdict: str,
) -> None:
    """Append this tick's verdict to the tick ledger. Never raises. See deploy_io.

    A tick that measured nothing writes nothing — `staging_tick_outcome` returns None for
    SKIPPED, and the tick runs every ten minutes, so recording those would bury the real
    samples. That decision is made HERE rather than inside `deploy_io.record_staging_tick`,
    which would otherwise have to import this module and close a cycle through
    `deploy_toolbox`.
    """
    outcome = staging_tick_outcome(verdict)
    if outcome is None:
        return
    deploy_io.record_staging_tick(
        state.path("staging_ticks"),
        CHICAGO,
        tools.now,
        sha,
        gated,
        verdict,
        outcome,
    )


def consume_staging_override(state: DeployerState) -> bool:
    """Spend the operator's one-tick override, if it is armed. True when it was."""
    return deploy_io.consume_override(state.path("staging_override"))


def consult_staging(
    tools: DeployTools,
    state: DeployerState,
    config: Config,
    services: set[str],
    origin: str,
) -> str:
    """Ask the staging cluster about this commit, and return the one-word verdict.

    The verdict is `staging_verdict`'s vocabulary: pass, rejected, no_verdict, or skipped when
    nothing was asked at all. Whether it stops the prod deploy is `staging_blocks`' decision, not
    this function's — returning a word and acting on it are kept apart so the gate can stay
    advisory (slice 3) while the verdict is already the thing being logged and measured.

    NOTHING HERE MAY BREAK A PROD DEPLOY, blocking or not. Every failure path — a missing script,
    an ssh outage, a wedged guest, a bug in this function — is caught by
    `deploy_io.run_staging_scripts` and reported as NO VERDICT, which `staging_blocks` never
    blocks on. An internal error alerts on the same path as any other non-PASS: a silent
    pass-through would make a bug here the one way past the gate that nobody sees.

    Off by default (`STAGING_GATE` in the unit's env). Turning it on costs every k8s tick the
    staging deploy's wall-clock, which is why it is a switch rather than a given.
    """
    if not config.staging_gate:
        return STAGING_SKIPPED
    # An ARMED gate with an empty subset can never gate anything, and the SKIPPED it returns
    # below is the same word a tick that simply touched no staging service gets. Those two
    # states are worth telling apart in the journal: the second is the ordinary case, the first
    # means the operator turned the gate on and it is doing nothing. `load_config` does not
    # parse STAGING_SUBSET — it is a `gitops_deploy.py` constant that `tick_config()` snapshots
    # — so a Config built anywhere else carries the fail-safe empty default and lands here.
    if not config.staging_subset:
        log(
            "staging: gate is ARMED but STAGING_SUBSET is empty — nothing can be gated, so "
            "every service is reported unchecked"
        )
    gated, ungated = staging_scope(services, config.staging_subset)
    if not gated:
        log(staging_verdict_summary(gated, ungated, 0, 0))
        return STAGING_SKIPPED

    deploy_rc, expect_rc = tools.run_staging_scripts(
        config.repo,
        origin,
        ",".join(sorted(gated)),
        config.staging_gate_timeout_s,
        config.staging_expect_timeout_s,
    )
    summary = staging_verdict_summary(gated, ungated, deploy_rc, expect_rc)
    log(summary)
    # Alerted, not silent: a journal line alone collects no operator judgement about whether a
    # failure was staging's fault or the change's, which is the one thing the entry condition's
    # false-failure rate is made of.
    if deploy_rc != 0 or expect_rc != 0:
        deploy_alerts.alert_once(
            tools,
            state,
            config,
            "staging_alerted",
            "staging",
            origin,
            deploy_alerts.staging_verdict_alert(
                origin, summary, config.staging_gate_blocking
            ),
        )
    verdict = staging_verdict(deploy_rc, expect_rc)
    record_staging_tick(tools, state, origin, gated, verdict)
    return verdict
