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
`deploy_alerts.py`, the staging gate's vocabulary in `deploy_staging.py`, the marker files in
`deploy_state.py`, and the decisions in the `deploy_*` modules that `deploy_logic.py` indexes.
Every phase takes the tick's `tools`, `state` and `config` and imports nothing from here — a
leaf that imported this module would get a second copy of it whenever the deployer runs as
`__main__`. Reach `deploy_io` and `deploy_alerts` QUALIFIED, not by from-import.

Config comes from /etc/gitops-deploy/config.env (KEY=VALUE), written by Ansible:
  REPO_DIR, BRANCH, HOSTNAME, DISCORD_WEBHOOK, HEALTH_TIMEOUT_S,
  REQUIRE_CI, CI_CONTEXTS, GITHUB_REPO

`deploy_config.load_config` parses it into one frozen `Config`, and CONFIG is that object. The
module-level constants below are still derived from it at import, and that is the remaining
coupling: they are what the test suite patches and what `state_dir` repoints. `tick_config()`
snapshots them back onto a `Config` once per tick, which is the object every phase then reads.
Parsing itself no longer raises — a malformed numeric value is collected and reported by
`CONFIG.validate()` inside `main()`, so a bad config.env is a Discord post naming the key
rather than an import traceback before the heartbeat exists.

Stdlib only.
"""

import dataclasses
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import deploy_alerts
import deploy_defer
import deploy_handlers
import deploy_phases
import deploy_state
import deploy_tick_types
from deploy_config import (
    Config,
    ConfigError,
    csv_set,
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


# Every marker read and write goes through this — `deploy_state.DeployerState.MARKERS` is the
# one table of what lives under /var/lib/gitops-deploy, and the `state_dir` fixture rebuilds
# this object against tmp_path. No path literal for that directory belongs in this module:
# `test_state_dir_repoints_every_state_path_in_the_module` fails on one, because a path built
# here would escape the fixture and write to the host during a test run.
STATE = deploy_state.DeployerState(deploy_state.STATE_DIR)


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
TIMEOUT = CONFIG.health_timeout_s
# Wall-clock budget (measured from process start, `DeployTools.run_start`) for the whole run's
# health-gating phase. Once spent, the gate stops and rolls back so the rollback (git reset +
# one redeploy) still finishes inside the unit's TimeoutStartSec — otherwise systemd SIGTERMs
# the deployer mid-gate, before write_hold()/rollback, and the bad commit is left live.
# `run_start` is measured AFTER `flock -w 180` acquires, but TimeoutStartSec counts the flock
# wait too, so the budget is sized 180 (max flock wait) + 1020 (this gate) + 300
# (HEALTH_TIMEOUT_S) = 1500s, which must fit inside the unit's TimeoutStartSec, keeping the
# rollback intact even under max lock contention with the weekly secret-rotate. Read
# gitops-deploy.service.j2 for the live ceiling rather than a number restated here: this
# comment called 1500s "the 25min timeout" through two raises of that ceiling, and
# test_gitops_deploy_timeout_budgets.py is what holds the sum inside it.
RUN_BUDGET_S = CONFIG.run_budget_s

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
# Bounds ONE ansible-playbook invocation on the k8s path. RUN_BUDGET_S does not reach here: it
# feeds gate_services(), the Docker health gate, which is inert on an all-k8s host — so without
# this the only bound is systemd's TimeoutStartSec SIGTERM, which can land mid-rollback.
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
# is funded on top, including why raising the ceiling for the staging gate did not change that.
BROAD_DEPLOY_TIMEOUT_S = CONFIG.broad_deploy_timeout_s

# ── the staging gate (Phase C slice 3) ──────────────────────────────────────────────────────
# OFF by default. Turning it on costs every k8s-deploying tick the staging deploy's wall-clock,
# so it is a switch rather than a given — and while it is off, this file behaves exactly as it
# did before the gate existed.
STAGING_GATE = CONFIG.staging_gate
# The services staging actually runs (docs/staging-cluster.md, Decision 6). A deploy is split
# against this: the intersection is what staging can speak for, and the remainder is reported as
# unchecked rather than passed. Configurable because the subset grows by config, not by code.
#
# This one and the two timeouts below read their fallback from `C` here rather than from CONFIG,
# and that is load-bearing rather than an oversight: scripts/docs/gen_doc_fragments.py parses
# `C.get("<KEY>", "<literal>")` calls OUT OF THIS FILE by name to publish the staging fragment,
# so a default that moved into load_config would leave the fragment with no source at all.
STAGING_SUBSET = csv_set(
    C.get(
        "STAGING_SUBSET", "traefik,authelia,freshrss,node-exporter,registry,ical-proxy"
    )
)
# Sized from a measured staging deploy, not from K8S_DEPLOY_TIMEOUT_S — see
# defaults/main.yml, which carries the measurement and the unit-budget arithmetic. These
# fallbacks MUST equal the Ansible defaults, because config.env.j2 renders both and a host
# whose config predates that render falls back to exactly these literals. Pinned by
# test_gitops_deploy_staging_timeouts.py::test_staging_timeout_fallbacks_match_the_ansible_defaults.
# A timeout here is NO VERDICT, never a rejection.
#
# The actual parsing lives in deploy_config.load_config now, with the same error-collection as
# every other numeric — a malformed value is recorded in CONFIG.errors rather than raising at
# import. The two `C.get(...)` calls below are unused: they exist only so
# scripts/docs/fragment_readers.py's config_default() parser, which reads a
# `C.get("<KEY>", "<default>")` call out of THIS file by name, still has one to find. Pinned
# against Config's own defaults by test_staging_timeout_module_fallbacks_match_config_defaults.
_STAGING_GATE_TIMEOUT_FALLBACK = C.get("STAGING_GATE_TIMEOUT_S", "600")
_STAGING_EXPECT_TIMEOUT_FALLBACK = C.get("STAGING_EXPECT_TIMEOUT_S", "120")
STAGING_GATE_TIMEOUT_S = CONFIG.staging_gate_timeout_s
STAGING_EXPECT_TIMEOUT_S = CONFIG.staging_expect_timeout_s
# Slice 4. Whether a staging REJECTION stops the prod deploy, or is only logged and alerted.
# A SEPARATE switch from STAGING_GATE, and off by default even where the gate is on: the entry
# condition in docs/staging-phase-c.md is evidence rather than effort, so the code lands long
# before the flip is justified. While this is false the deployer behaves exactly as slice 3 left
# it. What blocking does and does not act on is `staging_blocks`, not this constant.
STAGING_GATE_BLOCKING = CONFIG.staging_gate_blocking

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

    FIVE of the nineteen kwargs below are load-bearing, and fourteen are not — that asymmetry
    is deliberate, so do not prune the fourteen. The four:

      - `staging_subset` is derived from `C` here rather than parsed by `load_config`; its
        literal fallback in this file is what `scripts/docs/gen_doc_fragments.py` reads.
      - `k8s_autodeploy_enabled` is the value AFTER the empty-denylist fail-closed disarm above,
        which is a decision this module makes and `load_config` cannot.
      - `k8s_autodeploy_enabled_in_file` is the value BEFORE it — the pair is what tells a host
        that has the feature off from one whose denylist line was lost, which is the difference
        `deploy_phases.reconcile_denylist` gates on.
      - `repo`, `staging_gate` and `staging_subset` are what `tests/conftest.py`'s `tick`
        fixture repoints — so `staging_subset` is load-bearing twice over.

    The other fourteen equal CONFIG's fields today and are passed anyway, so that a patch of ANY
    module constant above reaches the phases. Dropping them would make the set of constants a
    test may repoint an implicit list nobody maintains, and the failure would be a fixture that
    silently describes the host's settings instead of the scripted ones.

    Called from `main()` and `entrypoint()`, never at import, and that is what keeps the
    constants above the single source: a phase reads `config.repo`, and `config.repo` is
    whatever `REPO` holds at the moment the tick starts — which is how `tests/conftest.py`'s
    `tick` fixture repoints `REPO`, `STAGING_GATE` and `STAGING_SUBSET` without any phase
    importing this module.
    """
    return dataclasses.replace(
        CONFIG,
        repo=REPO,
        branch=BRANCH,
        hostname=HOSTNAME,
        health_timeout_s=TIMEOUT,
        run_budget_s=RUN_BUDGET_S,
        k8s_autodeploy_enabled=K8S_AUTODEPLOY_ENABLED,
        k8s_autodeploy_enabled_in_file=K8S_AUTODEPLOY_ENABLED_IN_FILE,
        k8s_autodeploy_pilot=K8S_AUTODEPLOY_PILOT,
        k8s_autodeploy_denylist=K8S_AUTODEPLOY_DENYLIST,
        k8s_autodeploy_max_per_tick=K8S_AUTODEPLOY_MAX_PER_TICK,
        k8s_autodeploy_max_claim_services_per_tick=K8S_AUTODEPLOY_MAX_CLAIM_SERVICES_PER_TICK,
        k8s_deploy_timeout_s=K8S_DEPLOY_TIMEOUT_S,
        k8s_rollback_timeout_s=K8S_ROLLBACK_TIMEOUT_S,
        broad_deploy_timeout_s=BROAD_DEPLOY_TIMEOUT_S,
        staging_gate=STAGING_GATE,
        staging_gate_blocking=STAGING_GATE_BLOCKING,
        staging_subset=STAGING_SUBSET,
        staging_gate_timeout_s=STAGING_GATE_TIMEOUT_S,
        staging_expect_timeout_s=STAGING_EXPECT_TIMEOUT_S,
    )


def main(tools: DeployTools | None = None, config: Config | None = None) -> int:
    """Run one gitops-deploy tick end to end, as a sequence of named phases.

    `assess()` reads git and classifies the tick; `plan_tick()` turns the incoming range into a
    ChangeSet; one `handle_*` phase owns each terminal branch and returns the exit code. The
    branch order is load-bearing — broad before k8s before Docker — because a broad change and a
    promoted image bump can arrive in the same range and the broad plane has to win.

    Almost always returns 0 — a failed tick pages via Discord and the hold marker rather than a
    non-zero exit. The exceptions are the few `0 if posted else 1` branches, reached only when
    even the failure alert itself could not be delivered.

    Raises:
        deploy_config.ConfigError: config.env holds a value this deployer cannot use.
        RuntimeError: there is no config at all, so there is no repo to tick.
        RetryableFetchError: from `assess()`; entrypoint() skips the tick on it.
    """
    tools = tools if tools is not None else default_tools(CONFIG)
    config = config if config is not None else tick_config()
    CONFIG.validate()
    if not REPO:
        # No config, no repo to tick: page via the crash handler rather than run every git
        # command below against cwd="".
        raise RuntimeError(f"REPO_DIR is unset: no deployer config at {CONFIG_PATH}")
    # Ahead of the drain and every state write: a host whose inventory says has_gitops: false
    # is not this deployer, whatever payload is installed here (#1733).
    deploy_phases.refuse_unless_deployer(config)
    # Resend any alert a prior tick failed to deliver, BEFORE any short-circuit below: the ff-merged
    # secrets/tasks/meta/combined paths never re-reach their alert code (local==origin -> noop), so a
    # transient webhook failure is only recoverable here, not by discord()'s per-tick re-eval.
    deploy_alerts.drain_pending(tools, STATE, config)
    # Disk-only, independent of git state, so it runs before any branch can short-circuit the
    # tick: page (once per distinct set) when a rendered compose has no containers_list entry —
    # the stale-compose trap, twice now the cause of a phantom health gate + false rollback + hold.
    deploy_alerts.check_stale_composes(tools, STATE, config)
    # Disk-only too, and likewise ahead of every branch that can return. A role in the
    # `manual_plane` marker is owed to a hand on EVERY later tick, and the tick that recorded
    # it fast-forwarded — so from the next tick on this deployer is converged and re-enters
    # the broad arm never again. Without a line here the journal would say nothing at all
    # about a role nobody has applied yet.
    deploy_defer.log_pending(STATE)
    # Same shape, one plane over: a promoted image bump a broad tick deferred for lack of
    # budget is merged, so no later tick's range carries it either (#2449).
    deploy_defer.log_k8s_deferred(STATE)

    target = deploy_phases.assess(tools, STATE, config)
    if target.action == "dirty":
        return deploy_handlers.handle_dirty(tools, STATE, config, target)
    # Before any branch that could return: a stale denylist is healed on an IDLE tick, which is
    # what the tick after an ff-merge is, and that is the tick whose checkout already carries the
    # role that made the config stale. Deliberately after the dirty branch — the render derives
    # the denylist from the working tree, so rendering from a tree an operator is mid-edit in
    # would bake a list nobody pushed.
    if deploy_phases.reconcile_denylist(STATE, config, target.local):
        # A render ENDS the tick, for two reasons. The in-memory config still holds the list the
        # render just proved wrong, so anything below would decide against it. And every arm of
        # this unit is non-stacking by construction — the unit template sizes TimeoutStartSec as
        # max(broad, staging + k8s + rollback), not a sum — so a render that ran on to a k8s
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
        return deploy_handlers.handle_ci_failed(tools, STATE, config, target)
    if target.red_tip:
        # This tick fast-forwarded to a green ancestor of a RED tip, so it deploys — and the
        # tip's own failure still pages once for that SHA. Here rather than inside `assess`
        # because a phase that reads and classifies must not alert, and after the two CI
        # branches above because only a tick that got past them acts on a chosen ancestor.
        deploy_handlers.alert_red_tip(tools, STATE, config, target)

    plan = deploy_phases.plan_tick(tools, STATE, config, target)
    if plan.cs.broad:
        return deploy_handlers.handle_broad(tools, STATE, config, target, plan)
    if plan.cs.k8s_deploy:
        return deploy_handlers.handle_k8s(tools, STATE, config, target, plan)
    if not plan.cs.services:
        return deploy_handlers.handle_no_services(tools, STATE, config, target, plan)
    return deploy_handlers.handle_docker(tools, STATE, config, target, plan)


def entrypoint(tools: DeployTools | None = None) -> int:
    """One tick as systemd runs it.

    main() plus the exit-code contract around it. Returns the process exit code; the `__main__`
    guard below only hands it to sys.exit, so a test can call this directly
    (test_gitops_deploy_fetch_skip.py).
    """
    tools = tools if tools is not None else default_tools(CONFIG)
    config = tick_config()
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
        rc = main(tools, config)
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
            tools, config, deploy_alerts.bad_config_alert(HOSTNAME, CONFIG_PATH, e)
        )
        # Exit 0 on a delivered detailed post so OnFailure's generic curl doesn't double-page,
        # same convention as the other `0 if posted else 1` branches; exit 1 only if the
        # detailed post itself failed, leaving OnFailure the backstop.
        return 0 if posted else 1
    except Exception as e:
        deploy_alerts.discord(tools, config, deploy_alerts.crash_alert(e))
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
        STATE.record_behind(
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
    if STATE.clear_contention_unless_touched_since(tick_started):
        log("contention_since cleared: this tick was not deferred on a service lock")
    # Liveness marker: a tick that completed without crashing (incl. a rollback, rc=1).
    # monitor-bridge reads this; a crash skips the write so the Alive monitor goes stale.
    STATE.write("last_run", str(time.time()))
    return rc


if __name__ == "__main__":
    sys.exit(entrypoint())
