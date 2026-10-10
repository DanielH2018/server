#!/usr/bin/env python3
"""GitOps deployer — runs once per systemd-timer tick, on every host with has_gitops set.

Flow: fetch origin/master; if it advanced, require the tip's CI to be green; map changed
templates to services; ff-merge; deploy each via the existing ansible-playbook path;
health-gate each container. On failure: reset to the previous HEAD, redeploy the prior version,
record the bad SHA as a hold marker, and alert the dedicated Discord webhook.

`main()` sequences named phases and decides nothing itself: `deploy_phases.assess()` reads git
and returns a `TickTarget`, `deploy_phases.plan_tick()` turns the incoming range into a
`TickPlan`, and one `deploy_handlers.handle_*` function owns each terminal branch. The
transport lives in `deploy_io.py` and its leaves, the message bodies and the alert queue in
`deploy_alerts.py`, the marker files in
`deploy_state.py`, and the decisions in the `deploy_*` modules that `deploy_logic.py` indexes.
Every phase takes the tick's `tools`, `state` and `config` and imports nothing from here — a
leaf that imported this module would get a second copy of it whenever the deployer runs as
`__main__`. Reach `deploy_io` and `deploy_alerts` QUALIFIED, not by from-import.

Config comes from /etc/gitops-deploy/config.env (KEY=VALUE), written by Ansible:
  REPO_DIR, BRANCH, HOSTNAME, DISCORD_WEBHOOK,
  REQUIRE_CI, CI_CONTEXTS, GITHUB_REPO

`deploy_config.load_config` parses it into one frozen `Config`, and CONFIG is that object. The
module-level constants below are derived from it at import. `tick_config()` snapshots them back
onto a `Config` once per tick, which is the object every phase then reads.

`main()` and `entrypoint()` build nothing: they take the tick's `tools`, `config` and `state`
as arguments, and only the `__main__` guard at the bottom builds the production three. A test
passes fakes rather than patching this module (#3744).
Parsing itself no longer raises — a malformed numeric value is collected and reported by
`CONFIG.validate()` inside `main()`, so a bad config.env is a Discord post naming the key
rather than an import traceback before the heartbeat exists.

Stdlib only.

Usage: gitops_deploy.py   (takes no arguments; -h/--help prints this text)

gitops-deploy.service runs it from gitops-deploy.timer (every 10 minutes) as
`flock -w 180 <tree lock> uv run --no-project gitops_deploy.py`. A by-hand tick is the
`gitops-tick` skill. It reads GITOPS_DEPLOY_CONFIG (default /etc/gitops-deploy/config.env)
and keeps its markers in the state directory `deploy_state.STATE_DIR` names.
docs/gitops-pipeline.md has the exit codes.
"""

import dataclasses
import os
import sys
import time

if __name__ == "__main__":
    if any(arg in ("-h", "--help") for arg in sys.argv[1:]):
        print(__doc__.strip())
        sys.exit(0)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import deploy_alert_text
import deploy_alerts
import deploy_defer
import deploy_handlers
import deploy_k8s_owed
import deploy_phases
import deploy_state
import deploy_tick_types
from deploy_config import (
    Config,
    ConfigError,
    load_config,
    log,
    read_config_file,
)
from deploy_toolbox import DeployTools, default_tools

# The transient-fetch failure `deploy_phases.assess` raises and `entrypoint()` below catches.
# The class is DEFINED in deploy_tick_types.py because `assess` moved there and a leaf may not
# import this module (that is a cycle, and a second copy of this module under `__main__`). This
# is a second NAME for the same class object, not a subclass: `except RetryableFetchError`
# below and `pytest.raises(gitops_deploy.RetryableFetchError)` in the suite both catch exactly
# what `assess` raises.
RetryableFetchError = deploy_tick_types.RetryableFetchError
# Same arrangement for the refusal `deploy_phases.refuse_unless_deployer` raises (#1733).
NotTheDeployerHost = deploy_tick_types.NotTheDeployerHost


# Overridable so the test suite can import this module against a canned copy
# (tests/conftest.py sets it) instead of the host's 0600 file, which carries the webhook.
CONFIG_PATH = os.environ.get("GITOPS_DEPLOY_CONFIG", "/etc/gitops-deploy/config.env")


def cfg() -> dict[str, str]:
    """The deployer's config file as KEY=VALUE pairs, or {} when it is absent."""
    return read_config_file(CONFIG_PATH)


C = cfg()
CONFIG = load_config(C)
REPO = CONFIG.repo
BRANCH = CONFIG.branch
HOSTNAME = CONFIG.hostname

# ── k8s auto-deploy ───────────────────────────────────────────────────────────────────────────
# OFF unless the host explicitly enables it, so a host that has not re-templated config.env
# behaves exactly as it does today.
K8S_AUTODEPLOY_ENABLED = CONFIG.k8s_autodeploy_enabled
# The same key, kept as the file asked for it — the fail-closed disarm below mutates only
# K8S_AUTODEPLOY_ENABLED. `deploy_phases.reconcile_denylist` gates on this one so that the
# damaged state the disarm produces stays healable; the DECIDED marker at that gate is the long
# form. No new config.env key: both read K8S_AUTODEPLOY_ENABLED.
K8S_AUTODEPLOY_ENABLED_IN_FILE = CONFIG.k8s_autodeploy_enabled_in_file
K8S_AUTODEPLOY_PILOT = CONFIG.k8s_autodeploy_pilot
# 0 disables the cap. See split_k8s_auto_deploy: the whole promoted set shares one
# ansible-playbook run and one K8S_DEPLOY_TIMEOUT_S, and a timeout rolls the batch back
# together.
# DECIDED: falls back to the role default 3, NOT to 0 — same argument as the claim cap below,
# which this line did not carry until 2026-08-24. 0 means UNCAPPED, so a config.env that lost
# this key would restore the unbounded batch on exactly the host whose config is damaged. The
# live case is truncation, not age: in templates/config.env.j2 the denylist is line 23 and this
# key is line 27, so a half-written file keeps a matching denylist plus ENABLED=true and drops
# only the cap — passing the fail-closed denylist guard below while uncapped.
K8S_AUTODEPLOY_MAX_PER_TICK = CONFIG.k8s_autodeploy_max_per_tick
# Defaults to 1, not 0: an older config.env rendered before this key existed must get the SAFE
# cap, not an absent one. 0 here would silently restore the unbounded-batch behaviour this
# closes, on exactly the hosts whose config is stale (2026-08-22 review H2).
K8S_AUTODEPLOY_MAX_CLAIM_SERVICES_PER_TICK = (
    CONFIG.k8s_autodeploy_max_claim_services_per_tick
)
K8S_AUTODEPLOY_DENYLIST = CONFIG.k8s_autodeploy_denylist
if K8S_AUTODEPLOY_ENABLED and not K8S_AUTODEPLOY_DENYLIST:
    # Fail closed. An absent or empty denylist means "nothing is eligible", never "everything
    # is" — a truncated or half-rendered config.env must not silently widen what auto-deploys
    # to the whole cluster, platform roles included.
    # K8S_AUTODEPLOY_ENABLED_IN_FILE above is deliberately NOT flipped: it is what makes this
    # state distinguishable from a host that legitimately has the feature off, and therefore
    # what lets `deploy_phases.reconcile_denylist` re-render the file whose damage caused it.
    log(
        "K8S_AUTODEPLOY_ENABLED is set but the denylist is empty — disabling k8s auto-deploy"
    )
    K8S_AUTODEPLOY_ENABLED = False
# Bounds ONE ansible-playbook invocation on the k8s path. Without it the only bound is systemd's
# TimeoutStartSec SIGTERM, which can land mid-rollback.
K8S_DEPLOY_TIMEOUT_S = CONFIG.k8s_deploy_timeout_s
# Bounds the ROLLBACK redeploy specifically — the run that also reverts each claimed volume to
# its pre-deploy snapshot (k8s/volume-revert), which is strictly more work than a forward deploy.
# Sizing, the batch-summation gap this does NOT cover, and the lock-hold consequence all live in
# defaults/main.yml's gitops_deploy_k8s_rollback_timeout_s comment — this fallback is only what a
# host runs on before its config.env is re-templated with the new value.
K8S_ROLLBACK_TIMEOUT_S = CONFIG.k8s_rollback_timeout_s
# Bounds ONE broad-plane apply (initial_setup.yml --tags <role>, or a full deploy.yml).
# Bounded because that arm is forward-only: without a timeout a wedged run spends the unit's
# whole TimeoutStartSec and is SIGTERMed with no hold written and no alert sent, leaving the
# tree fast-forwarded onto a commit nothing recorded as bad. 1800s covers the 1212s measured
# full deploy (2026-08-22) with headroom, inside TimeoutStartSec alongside the 180s max flock
# wait. This arm does not stack with the k8s one — it returns before that block — so it is
# never the term that sizes the ceiling. See deploy_logic.broad_budget_ok for why no rollback
# is funded on top.
BROAD_DEPLOY_TIMEOUT_S = CONFIG.broad_deploy_timeout_s

# ── CI gate ───────────────────────────────────────────────────────────────────────────────────
# Refuse to deploy a master tip whose CI is red or unfinished. Without this the deployer applies
# whatever landed on master, green or red: nothing in the pull path ever consulted a workflow
# result, so a broken commit reached the homelab on the next 30-min tick.
#
# OFF unless config.env says otherwise, so a host that has not been re-templated keeps its current
# behaviour, and REQUIRE_CI=false is the documented way back out.
#
# The gate itself is `DeployTools.fetch_ci_verdict`, which `deploy_toolbox.default_tools` binds to
# CONFIG.require_ci, CONFIG.ci_repo and CONFIG.ci_contexts. No module global copies those three:
# a copy rebound here could disagree with the frozen CONFIG it came from.
#
# The disarm for an empty CI_CONTEXTS/GITHUB_REPO (a half-rendered config.env) is decided inside
# deploy_config.load_config, which is also where it logs.
# ── the tick's settings, as one snapshot ──────────────────────────────────────


def tick_config() -> Config:
    """The settings this tick runs on: CONFIG, with the module constants above snapshotted back.

    Every phase takes a `deploy_config.Config` rather than a type of the deployer's own.

    TWO of the twelve kwargs below are load-bearing, and ten are not. The two:

      - `k8s_autodeploy_enabled` is the value AFTER the empty-denylist fail-closed disarm above,
        which is a decision this module makes and `load_config` cannot.
      - `k8s_autodeploy_enabled_in_file` is the value BEFORE it — the pair is what tells a host
        that has the feature off from one whose denylist line was lost, which is the difference
        `deploy_phases.reconcile_denylist` gates on.

    The other ten, `repo` among them, equal CONFIG's fields and are passed anyway, so that
    this snapshot, not CONFIG, stays the one object a phase reads. A test does not patch these
    constants: it builds its config with `dataclasses.replace(tick_config(), ...)`.

    Called from the `__main__` guard, never at import.
    """
    return dataclasses.replace(
        CONFIG,
        repo=REPO,
        branch=BRANCH,
        hostname=HOSTNAME,
        k8s_autodeploy_enabled=K8S_AUTODEPLOY_ENABLED,
        k8s_autodeploy_enabled_in_file=K8S_AUTODEPLOY_ENABLED_IN_FILE,
        k8s_autodeploy_pilot=K8S_AUTODEPLOY_PILOT,
        k8s_autodeploy_denylist=K8S_AUTODEPLOY_DENYLIST,
        k8s_autodeploy_max_per_tick=K8S_AUTODEPLOY_MAX_PER_TICK,
        k8s_autodeploy_max_claim_services_per_tick=K8S_AUTODEPLOY_MAX_CLAIM_SERVICES_PER_TICK,
        k8s_deploy_timeout_s=K8S_DEPLOY_TIMEOUT_S,
        k8s_rollback_timeout_s=K8S_ROLLBACK_TIMEOUT_S,
        broad_deploy_timeout_s=BROAD_DEPLOY_TIMEOUT_S,
    )


def main(tools: DeployTools, config: Config, state: deploy_state.DeployerState) -> int:
    """Run one gitops-deploy tick end to end, as a sequence of named phases.

    `assess()` reads git and classifies the tick; `plan_tick()` turns the incoming range into a
    ChangeSet; one `handle_*` phase owns each terminal branch and returns the exit code. The
    branch order is load-bearing — broad before k8s — because a broad change and a
    promoted image bump can arrive in the same range and the broad plane has to win.

    Almost always returns 0 — a failed tick pages via Discord and the hold marker rather than a
    non-zero exit. The exceptions are the few `0 if posted else 1` branches, reached only when
    even the failure alert itself could not be delivered.

    Args:
        tools: every process boundary the tick crosses (`deploy_toolbox.DeployTools`).
        config: the tick's settings, `tick_config()` in production.
        state: the marker files. No default, so a call that forgot it cannot fall back to
            the host's state directory.

    Raises:
        deploy_config.ConfigError: config.env holds a value this deployer cannot use.
        RuntimeError: there is no config at all, so there is no repo to tick.
        RetryableFetchError: from `assess()`; entrypoint() skips the tick on it.
    """
    CONFIG.validate()
    if not config.repo:
        # No config, no repo to tick: page via the crash handler rather than run every git
        # command below against cwd="".
        raise RuntimeError(f"REPO_DIR is unset: no deployer config at {CONFIG_PATH}")
    # Ahead of the drain and every state write: a host whose inventory says has_gitops: false
    # is not this deployer, whatever payload is installed here (#1733).
    deploy_phases.refuse_unless_deployer(config)
    # Resend any alert a prior tick failed to deliver, BEFORE any short-circuit below: the ff-merged
    # secrets/tasks/meta/combined paths never re-reach their alert code (local==origin -> noop), so a
    # transient webhook failure is only recoverable here, not by discord()'s per-tick re-eval.
    deploy_alerts.drain_pending(tools, state, config)
    # And the verdicts `deploy --detach` queued, for the same reason: its notifier runs once
    # per deploy and never again, so this tick is the only retry they get (#3987).
    deploy_alerts.flush_detach_spool(tools, state, config)
    # Disk-only too, and likewise ahead of every branch that can return. A role in the
    # `manual_plane` marker is owed to a hand on EVERY later tick, and the tick that recorded
    # it fast-forwarded — so from the next tick on this deployer is converged and re-enters
    # the broad arm never again. Without a line here the journal would say nothing at all
    # about a role nobody has applied yet.
    # Route setup roles by the checkout before anything prints a role's command: the
    # deployer's own directory cannot derive the routing, so until this runs it routes none.
    # `plan_tick` re-reads it at origin for the range it classifies.
    deploy_phases.adopt_setup_routing(tools, config, "HEAD")
    deploy_defer.log_pending(state)
    # Same shape, one plane over: the k8s changes a tick merged and did not apply (#2449,
    # #2570). `reconcile` discharges what a deploy has since covered, then names the rest.
    deploy_k8s_owed.reconcile(tools, state, config)

    target = deploy_phases.assess(tools, state, config)
    if target.action == "dirty":
        return deploy_handlers.handle_dirty(tools, state, config, target)
    # Before any branch that could return: a stale denylist is healed on an IDLE tick, which is
    # what the tick after an ff-merge is, and that is the tick whose checkout already carries the
    # role that made the config stale. Deliberately after the dirty branch — the render derives
    # the denylist from the working tree, so rendering from a tree an operator is mid-edit in
    # would bake a list nobody pushed.
    if deploy_phases.reconcile_denylist(tools, state, config, target.local):
        # A render ENDS the tick, for two reasons. The in-memory config still holds the list the
        # render just proved wrong, so anything below would decide against it. And every arm of
        # this unit is non-stacking by construction — the unit template sizes TimeoutStartSec as
        # max(broad, k8s + rollback), not a sum — so a render that ran on to a k8s
        # deploy would be the first arm to add its budget to another's and could be SIGTERMed
        # mid-rollback. The next tick is ten minutes away and reads the fresh config.
        return 0
    if target.action == "noop":
        return 0
    if target.action == "skip_hold":
        log(f"origin at known-bad {target.origin[:8]}; holding")
        return 0
    if target.action == "ci_pending":
        # Normal for the first tick after a push: the workflow is still running. No alert — it
        # resolves on its own, and a host left behind for hours is the behind-origin watchdog's
        # job, not this branch's.
        log(
            f"origin {target.origin[:8]}: CI not finished — deferring, will retry next tick"
        )
        return 0
    if target.action == "ci_failed":
        return deploy_handlers.handle_ci_failed(tools, state, config, target)
    if target.red_tip:
        # This tick fast-forwarded to a green ancestor of a RED tip, so it deploys — and the
        # tip's own failure still pages once for that SHA. Here rather than inside `assess`
        # because a phase that reads and classifies must not alert, and after the two CI
        # branches above because only a tick that got past them acts on a chosen ancestor.
        deploy_handlers.alert_red_tip(tools, state, config, target)

    plan = deploy_phases.plan_tick(tools, state, config, target)
    if plan.cs.broad:
        return deploy_handlers.handle_broad(tools, state, config, target, plan)
    if plan.cs.k8s_deploy:
        return deploy_handlers.handle_k8s(tools, state, config, target, plan)
    return deploy_handlers.handle_no_services(tools, state, config, target, plan)


def entrypoint(
    tools: DeployTools,
    config: Config,
    state: deploy_state.DeployerState,
    *,
    main=main,
) -> int:
    """One tick as systemd runs it.

    main() plus the exit-code contract around it. Returns the process exit code; the `__main__`
    guard below builds the arguments and hands the result to sys.exit, so a test can call this
    directly (test_gitops_deploy_fetch_skip.py). `main` is a parameter so that a test can
    script main()'s outcome without patching this module.
    """
    # When this tick began, so the contention streak below can tell a marker this tick wrote
    # from one an earlier tick left: `for_contention` stamps `last_seen` with the wall clock.
    tick_started = time.time()
    # HEAD as the tick found it, so the behind-origin stamp below can tell a tick that MOVED
    # the tree from one that parked. None when it could not be read, which disarms the
    # re-stamp rather than guessing: an unreadable HEAD is not evidence of progress.
    try:
        head_before = tools.run(["git", "rev-parse", "HEAD"], cwd=config.repo)
    except Exception as e:
        log(f"could not read HEAD before the tick: {e}")
        head_before = None
    try:
        rc = main(tools, config, state)
    except RetryableFetchError as e:
        # Transient `git fetch` failure: skip this tick without paging (no crash Discord, and exit 0
        # so the OnFailure alert unit doesn't fire either) and WITHOUT writing last_run — a one-off
        # blip is invisibly retried next tick, while a persistent fetch break ages last_run and trips
        # GitOps-Alive. Must precede the generic handler below (Python matches except-clauses in order).
        log(f"git fetch failed (retryable) — skipping tick, will retry next run: {e}")
        return 0
    except deploy_tick_types.NotTheDeployerHost as e:
        # DECIDED: exit 0, no Discord post, no last_run. The unit only reaches this branch on a
        # host whose inventory has already retired the deployer, so a non-zero exit would page
        # through OnFailure every ten minutes from a webhook that host should no longer hold,
        # and a last_run write would stamp liveness onto state nothing reads. The journal line
        # is the signal; the fix is the role's teardown, not a louder tick. A spurious refusal
        # on the real deployer is not silent either: last_run stops advancing and GitOps-Alive
        # goes stale inside GITOPS_MAX_AGE_MIN (90 minutes), which is the backstop this exit 0
        # leans on.
        log(f"gitops-deploy: {e}")
        return 0
    except ConfigError as e:
        # One clear line, not a traceback. This was an unhandled ValueError raised during IMPORT
        # until load_config deferred the parse, so it reached an operator as a stack trace with no
        # key name in it, before any of the alerting below existed in the process.
        log(f"gitops-deploy: {e}")
        posted = deploy_alerts.discord(
            tools, config, deploy_alert_text.bad_config_alert(HOSTNAME, CONFIG_PATH, e)
        )
        # Exit 0 on a delivered detailed post so OnFailure's generic curl doesn't double-page,
        # same convention as the other `0 if posted else 1` branches; exit 1 only if the
        # detailed post itself failed, leaving OnFailure the backstop.
        return 0 if posted else 1
    except Exception as e:
        deploy_alerts.discord(tools, config, deploy_alert_text.crash_alert(e))
        raise
    # Whether this host ENDED the tick behind origin, read after everything the tick did rather
    # than before it: a tick that deployed successfully converged and must clear the marker
    # rather than leave a stale one for the next 30 minutes. The rev-parses and the ancestry
    # query live here because they reach git; `DeployerState.record_behind` owns the marker
    # itself. The comparison against `head_before` is what the stamp is FOR: it measures time
    # without a fast-forward, so a tick that landed at a green ancestor re-stamps even though
    # it ends behind the tip.
    #
    # Best-effort: a `git rev-parse` failure must not turn an otherwise-fine tick into a
    # "gitops-deploy crashed" page. The tick has already done its work by this point, and a
    # persistently broken repo surfaces through last_run/Alive anyway.
    try:
        local = tools.run(["git", "rev-parse", "HEAD"], cwd=config.repo)
        origin = tools.run(
            ["git", "rev-parse", f"origin/{config.branch}"], cwd=config.repo
        )
        state.record_behind(
            origin,
            origin != local and tools.is_ancestor(config.repo, local, origin),
            time.time(),
            fast_forwarded=head_before is not None and local != head_before,
        )
    except Exception as e:
        log(f"could not record behind-origin state: {e}")
    # A tick that got here without deferring on a service lock ends any contention streak:
    # the lock has stopped wedging the deployer, whatever else this tick did. Only a tick
    # that reaches this line clears it — a crash above raises past here, and a crash is not
    # evidence the lock was released.
    if state.clear_contention_unless_touched_since(tick_started):
        log("contention_since cleared: this tick was not deferred on a service lock")
    # Liveness marker: a tick that completed without crashing (incl. a rollback, rc=1).
    # monitor-bridge reads this; a crash skips the write so the Alive monitor goes stale.
    state.write("last_run", str(time.time()))
    return rc


if __name__ == "__main__":
    sys.exit(
        entrypoint(
            default_tools(CONFIG),
            tick_config(),
            deploy_state.DeployerState(deploy_state.STATE_DIR),
        )
    )
