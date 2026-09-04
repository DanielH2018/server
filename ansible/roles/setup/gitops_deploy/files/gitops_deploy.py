#!/usr/bin/env python3
"""GitOps deployer — runs once per systemd-timer tick, on every host with has_gitops set.

Flow: fetch origin/master; if it advanced, require the tip's CI to be green; map changed
templates to services; ff-merge; deploy each via the existing ansible-playbook path;
health-gate each container. On failure: reset to the previous HEAD, redeploy the prior version,
record the bad SHA as a hold marker, and alert the dedicated Discord webhook.

`main()` sequences named phases and decides nothing itself: `assess()` reads git and returns a
`TickTarget`, `plan_tick()` turns the incoming range into a `TickPlan`, and one `handle_*`
function owns each terminal branch. The transport those phases call lives in `deploy_io.py`,
the message bodies in `deploy_alerts.py`, and the decisions in the `deploy_*` modules
`deploy_logic.py` indexes. Reach `deploy_io` and `deploy_alerts` QUALIFIED, never by
from-import — see `deploy_io.py`'s docstring.

Config comes from /etc/gitops-deploy/config.env (KEY=VALUE), written by Ansible:
  REPO_DIR, BRANCH, HOSTNAME, DISCORD_WEBHOOK, HEALTH_TIMEOUT_S,
  REQUIRE_CI, CI_CONTEXTS, GITHUB_REPO

`deploy_io.load_config` parses it into one frozen `Config`, and CONFIG is that object. The
module-level constants below are still derived from it at import, and that is the remaining
coupling: they are what the test suite patches and what `state_dir` repoints, so threading
CONFIG through every function instead would be a second change on top of this one. Parsing
itself no longer raises — a malformed numeric value is collected and reported by
`CONFIG.validate()` inside `main()`, so a bad config.env is a Discord post naming the key
rather than an import traceback before the heartbeat exists.

Stdlib only.
"""

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import deploy_alerts
import deploy_io
from deploy_changes import (
    ChangeSet,
    comment_only_broad_changes,
    services_from_changed_paths,
    setup_tags_for,
    shared_module_consumers,
)
from deploy_git import (
    behind_marker,
    broad_hold_cleared_by,
    ci_verdict,
    dirty_alert_slot,
    dirty_summary,
    github_auth_headers,
    github_token,
    hold_plane_marker,
    is_diverged,
    next_action,
    should_alert_dirty,
)
from deploy_health import (
    PENDING_ALERTS_MAX,
    apply_drain_result,
    apply_send_result,
    cap_pending,
    gate_services,
)
from deploy_inventory import declared_k8s_services, reroute_k8s_services
from deploy_io import log
from deploy_k8s import (
    declared_denylist,
    declares_snapshot_claims,
    is_image_only_diff,
    rollback_volume_revert_note,
    split_k8s_auto_deploy,
)
from deploy_remediation import broad_remediation, deferred_service_alerts
from deploy_staging import (
    STAGING_SKIPPED,
    staging_blocks,
    staging_scope,
    staging_verdict,
    staging_verdict_summary,
)


class RetryableFetchError(Exception):
    """A transient git failure: `git fetch origin`, or `git status` unable to read the tree.

    entrypoint() turns this into a CLEAN skip of the tick — exit 0, NO in-script Discord
    crash-page, NO OnFailure — that also does NOT refresh last_run. So a one-off blip is
    silently retried next tick, while a PERSISTENT fetch failure still surfaces via
    GitOps-Alive going stale over several missed ticks. Distinct from a real crash (unexpected
    exception), which still pages. Before this, a `run()`-raised fetch error propagated to
    entrypoint() and double-paged (the crash Discord + the OnFailure unit) every 30-min tick for
    the whole duration of a GitHub-side incident.
    """


# The state directory's fifteen marker files, each kept as a module-level literal. Two things
# depend on that shape: `tests/conftest.py`'s `state_dir` fixture repoints every constant whose
# value starts with the prefix (a path built by an f-string would keep pointing at the host), and
# `test_the_tick_ledger_constant_matches_the_ansible_default` reads STAGING_TICK_LEDGER's literal
# out of this file. `STATE` below is the object the code actually reads and writes them through;
# `tests/test_deployer_state.py` asserts the two agree path for path.
HOLD_FILE = "/var/lib/gitops-deploy/hold_sha"
LAST_RUN = "/var/lib/gitops-deploy/last_run"
# Origin SHA recorded while local and origin have DIVERGED (see deploy_logic.is_diverged): the
# deployer can't fast-forward and noops forever, so origin's new commits never deploy while both
# GitOps monitors stay green. monitor-bridge's check_gitops_status reads this (same :ro mount as
# hold_sha) and pages GitOps Status until the host tree is reconciled. Cleared once resolved.
DIVERGED_FILE = "/var/lib/gitops-deploy/diverged_sha"
# "<origin_sha> <unix_ts_first_seen>" while the host is BEHIND origin at the end of a tick — origin
# strictly ahead and we did not converge. Every reason lands here: a deferred broad change, a
# long-dirty tree, a hold. The broad path in particular is invisible otherwise — it never
# ff-merges, so the host parks behind master indefinitely while last_run keeps ticking (Alive
# green) and is_diverged stays false (origin is a strict descendant, so Status green too). That is
# how daniel-server sat on a 12-commit-old tree for hours on 2026-08-02 with every GitOps signal
# green, until the un-deployed Pi-hole DNS records were noticed by hand.
#
# The timestamp is what makes this safe to page on: a normal push is behind for one tick, and an
# operator mid-edit (the dirty path, deliberately treated as healthy) is behind for as long as they
# are editing. Only sustained behind-ness is a problem, so monitor-bridge applies an age threshold.
# The first-seen stamp is preserved across ticks and reset ONLY on convergence — not per-SHA, or a
# steady trickle of pushes to a permanently-stuck host would keep restarting the clock.
BEHIND_FILE = "/var/lib/gitops-deploy/behind_since"
# The playbook (and tags) whose broad apply failed, written alongside hold_sha. hold_sha
# alone is service-shaped — monitor-bridge's message says "revert the offending PR", which
# is the wrong remediation here: the tree is already fast-forwarded and a playbook is what
# broke, so reverting the PR undoes nothing. This names what to re-run instead.
HOLD_PLANE_FILE = "/var/lib/gitops-deploy/hold_plane"
# The sorted stale-compose set last alerted on, so a lingering stale dir doesn't re-page
# every tick — only a CHANGED set (new stale dir, or one cleaned up) re-alerts.
STALE_COMPOSE_FILE = "/var/lib/gitops-deploy/stale_composes_alerted"
# Last origin SHA we've already alerted on for a broad change, so a deferred
# broad change doesn't re-page Discord every 30-min tick until it's resolved.
BROAD_FILE = "/var/lib/gitops-deploy/broad_alerted_sha"
# Same throttle for a secrets-only push (rotated value with no service template change):
# alert once per SHA so the operator redeploys the consumer(s), don't re-page every tick.
SECRETS_ALERT_FILE = "/var/lib/gitops-deploy/secrets_alerted_sha"
# Same throttle for a tasks-only push (a role tasks/ change, which isn't auto-deployed): alert once
# per SHA so the operator redeploys the role by hand, don't re-page every tick.
TASKS_ALERT_FILE = "/var/lib/gitops-deploy/tasks_alerted_sha"
# Same throttle for a meta-only push (a role meta/deps.yml change — the cross-service deploy
# graph, not auto-deployed): alert once per SHA so the operator redeploys the affected service(s).
META_ALERT_FILE = "/var/lib/gitops-deploy/meta_alerted_sha"
# Same throttle for a k8s-role push (ansible/roles/k8s/<role>/...): alert once per SHA so the
# operator redeploys it by hand. Unlike tasks/meta this deployer has no mechanism that ever
# applies a k8s role change, so there's no "rode a redeploy" case to dedupe against `deployed`.
K8S_ALERT_FILE = "/var/lib/gitops-deploy/k8s_alerted_sha"
# Per-SHA throttle for the stale-denylist alert. The DISARM itself is stateless — recomputed
# every tick, so it clears the moment an operator re-renders the config. Only the page is
# throttled.
STALE_DENYLIST_FILE = "/var/lib/gitops-deploy/stale_denylist_alerted_sha"
# Same throttle for a master tip that FAILED CI: alert once per SHA so the operator fixes or
# reverts, instead of re-paging every 30-min tick for as long as master stays red. There is no
# marker for the `ci_pending` path — an unfinished run is the normal state for the first tick or
# two after a push and resolves itself, so it logs and stays silent.
CI_ALERT_FILE = "/var/lib/gitops-deploy/ci_alerted_sha"
STAGING_ALERT_FILE = "/var/lib/gitops-deploy/staging_alerted_sha"
# Undelivered post-merge alerts, retried at the TOP of every tick. The secrets/tasks/meta/combined
# channels `git merge --ff-only` BEFORE their delivery-gated marker write, so once merged
# local==origin and the next tick short-circuits at `noop` (main) before ever re-reaching the alert
# code — a single transient discord() failure (timeout/5xx/Cloudflare-1010/DNS blip) would otherwise
# drop that alert forever (the rotated secret sits stale in its container / the tasks|meta change sits
# ff-merged-but-unapplied, with no other signal). This queue decouples DELIVERY from the git action:
# an alert that fails to send is persisted here keyed by "<channel>:<sha>" and drain_pending() resends
# it every tick until a confirmed 2xx clears it. The per-SHA markers above still gate DETECTION (so a
# delivered alert isn't re-queued on the broad path's every-tick re-eval); this queue owns delivery.
PENDING_ALERTS_FILE = "/var/lib/gitops-deploy/pending_alerts.json"
# Last dirty-alert slot (YYYY-MM-DD:am|pm) we paged for a dirty working tree. The tick runs every
# 30 min, so without this an open edit session would re-alert all day; we throttle to one alert per
# slot — a morning slot fired on the first tick at/after DIRTY_ALERT_MORNING_HOUR (08:00 CT) and an
# evening slot at/after DIRTY_ALERT_EVENING_HOUR (20:00 CT). See deploy_logic.dirty_alert_slot.
DIRTY_ALERT_FILE = "/var/lib/gitops-deploy/dirty_alerted_date"
# Where a real gated tick's verdict is recorded. Deliberately NOT the backfill ledger: that file
# is planned from — `backfill_staging_gate.py --since-ledger` reads its newest row to build the
# next window — so a tick row in it would send the hourly ratchet to a window it cannot run.
# `gitops_deploy_staging_tick_ledger` in the role's defaults is the same path, tied by
# test_the_tick_ledger_constant_matches_the_ansible_default.
STAGING_TICK_LEDGER = "/var/lib/gitops-deploy/staging-ticks.jsonl"
# The operator's one-tick escape hatch, armed by creating the file and disarmed by removing it.
# Decision 4: "Build the override before the gate. A gate with no escape hatch becomes a gate
# somebody deletes at 2 AM, and nobody reviews the deletion."
#
# It is CONSUMED at the point the gate would block, never at the point it is read. Consuming on
# entry would spend it on the first tick after arming — which is usually a tick with nothing to
# gate — and leave the operator's actual push facing the block with the hatch already gone.
STAGING_OVERRIDE_FILE = "/var/lib/gitops-deploy/staging_gate_override"

DIRTY_ALERT_MORNING_HOUR = 8
DIRTY_ALERT_EVENING_HOUR = 20
# Host clock is UTC; the operator wants the twice-daily reminder at 08:00 and 20:00 local time.
CHICAGO = ZoneInfo("America/Chicago")

# Every marker read and write goes through this. The `state_dir` fixture rebuilds it against
# tmp_path alongside the constants above.
STATE = deploy_io.DeployerState(deploy_io.STATE_DIR)


# Overridable so the test suite can import this module against a canned copy
# (tests/conftest.py sets it) instead of the host's 0600 file, which carries the webhook.
CONFIG_PATH = os.environ.get("GITOPS_DEPLOY_CONFIG", "/etc/gitops-deploy/config.env")


def cfg() -> dict[str, str]:
    """The deployer's config file as KEY=VALUE pairs, or {} when it is absent."""
    return deploy_io.read_config_file(CONFIG_PATH)


C = cfg()
CONFIG = deploy_io.load_config(C)
REPO = CONFIG.repo
BRANCH = CONFIG.branch
HOSTNAME = CONFIG.hostname
TIMEOUT = CONFIG.health_timeout_s
# Wall-clock budget (measured from process start, RUN_START) for the whole run's health-gating
# phase. Once spent, the gate stops and rolls back so the rollback (git reset + one redeploy)
# still finishes inside the unit's TimeoutStartSec (25min) — otherwise systemd SIGTERMs the
# deployer mid-gate, before write_hold()/rollback, and the bad commit is left live. RUN_START is
# measured AFTER `flock -w 180` acquires, but TimeoutStartSec counts the flock wait too, so the
# budget is sized 180 (max flock wait) + 1020 (this gate) + 300 (HEALTH_TIMEOUT_S) = 1500 = the
# 25min timeout, keeping the rollback intact even under max lock contention with the weekly
# secret-rotate. See gitops-deploy.service.j2.
RUN_BUDGET_S = CONFIG.run_budget_s
RUN_START = time.time()

# ── k8s auto-deploy ───────────────────────────────────────────────────────────────────────────
# OFF unless the host explicitly enables it, so a host that has not re-templated config.env
# behaves exactly as it does today.
K8S_AUTODEPLOY_ENABLED = CONFIG.k8s_autodeploy_enabled
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
STAGING_SUBSET = deploy_io.csv_set(
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
# The actual parsing lives in deploy_io.load_config now, with the same error-collection as
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
# The disarm for an empty CI_CONTEXTS/GITHUB_REPO (a half-rendered config.env) is decided inside
# deploy_io.load_config, which is also where it logs — CONFIG.require_ci is the one value, so a
# module global rebound separately here could disagree with the frozen CONFIG it was copied from.
REQUIRE_CI = CONFIG.require_ci
# GitHub check-run NAMES that must be green — the same strings branch protection calls contexts.
# Comma-separated; the names contain spaces and parens, never commas.
CI_CONTEXTS = CONFIG.ci_contexts
CI_REPO = CONFIG.ci_repo


# ── what one phase hands the next ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class TickTarget:
    """What `assess()` found when it looked at git, and what `next_action` made of it.

    Attributes:
        local: the commit this checkout is on.
        origin: `origin/<branch>`, resolved ONCE — see `assess()` for why re-resolving it
            anywhere below would open a window for an unchecked commit to deploy.
        hold: the SHA this host refuses to redeploy, or None.
        dirty: whether `git status --porcelain` reported anything, untracked files included.
        status: that command's raw stdout, so the dirty branch can name the paths.
        action: `next_action`'s word — noop, dirty, skip_hold, ci_pending, ci_failed, deploy.
    """

    local: str
    origin: str
    hold: str | None
    dirty: bool
    status: str
    action: str


@dataclass(frozen=True)
class TickPlan:
    """What the incoming range means for this host, once classified.

    Attributes:
        cs: the ChangeSet, after k8s rerouting and the auto-deploy promotion split.
        paths: the changed paths, minus any comment-only broad change dropped as quiet.
        k8s_services: this host's `platform: k8s` containers_list entries, which decide
            whether a deferred k8s alert can name a `--tags` redeploy at all.
    """

    cs: ChangeSet
    paths: list[str]
    k8s_services: set[str]


# ── git and CI ────────────────────────────────────────────────────────────────────────────────


def fetch_ci_verdict(sha: str) -> str:
    """`pass` / `pending` / `fail` for `sha`, from GitHub's check-runs API.

    Authenticated through `gh auth token` when the CLI is logged in (deploy_logic.github_token
    says why: the anonymous 60/hour limit is per source IP and shared with every landing's
    `await_ci.py` poll, and two landings exhaust it), anonymous otherwise.

    An unreachable or malformed API reads as `pending`, never `pass`: the gate has to fail closed
    or it is not a gate. That defers the tick and retries in 30 minutes, and because the tick still
    completes normally (writing `last_run`), a GitHub outage does NOT trip GitOps-Alive the way a
    RetryableFetchError would. Sustained unavailability instead leaves the host behind origin,
    which `behind_marker` records and the 6h behind-origin watchdog pages on.
    """
    if not REQUIRE_CI:
        return "pass"
    url = (
        f"https://api.github.com/repos/{CI_REPO}/commits/{sha}/check-runs?per_page=100"
    )
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "gitops-deploy",
            **github_auth_headers(github_token(os.environ, subprocess.run)),
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            payload = json.load(resp)
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as e:
        log(f"CI status unavailable for {sha[:8]} ({e}) — deferring this tick")
        return "pending"
    return ci_verdict(payload.get("check_runs", []), CI_CONTEXTS)


def is_ancestor(ancestor: str, descendant: str) -> bool:
    """True if `ancestor` is an ancestor of (or equal to) `descendant`, in this host's repo."""
    return deploy_io.is_ancestor(REPO, ancestor, descendant)


def _record_behind() -> None:
    """Record whether this host ended the tick behind origin (see BEHIND_FILE).

    Runs after main() so it reads the state we actually finished in, not the one we started in —
    a tick that deployed successfully converged and must clear the marker rather than leave a
    stale one for the next 30 minutes.

    Best-effort: a `git rev-parse` failure here must not turn an otherwise-fine tick into a
    "gitops-deploy crashed" page. The tick has already done its work by this point, and a
    persistently broken repo surfaces through last_run/Alive anyway.
    """
    try:
        local = deploy_io.run(["git", "rev-parse", "HEAD"], cwd=REPO)
        origin = deploy_io.run(["git", "rev-parse", f"origin/{BRANCH}"], cwd=REPO)
        behind = origin != local and is_ancestor(local, origin)
        STATE.write(
            "behind", behind_marker(behind, origin, STATE.behind_since, time.time())
        )
    except Exception as e:
        log(f"could not record behind-origin state: {e}")


def read_hold() -> str | None:
    return STATE.hold_sha


def write_hold(sha: str | None) -> None:
    STATE.write("hold", sha)


def clear_broad_hold(playbook: str, tags: list[str]) -> None:
    """Clear the hold after a broad apply, but only if this apply covered the held plane.

    A hold says one plane is unapplied, and every consumer gates on `hold_sha` — so clearing
    it after a success in a DIFFERENT plane turns GitOps Deploy — Status green over a plane
    nothing has applied (issue #878). When the hold survives, the tick still succeeded: the
    marker is the only thing kept.
    """
    held = STATE.hold_plane or ""
    if not broad_hold_cleared_by(held, playbook, tags):
        log(
            f"hold kept: {held} is still unapplied "
            f"(this tick applied {hold_plane_marker(playbook, tags)})"
        )
        return
    STATE.write("hold_plane", None)
    write_hold(None)


def clear_service_hold() -> None:
    """Clear a hold after a successful service deploy, unless a broad plane is unapplied.

    A k8s or Docker deploy applies no plane, so it is never evidence that the plane a broad
    hold names has been applied. Without this, an unrelated service deploy clears `hold_sha`
    and orphans `hold_plane`, which `gitops_status` never reads on its own.
    """
    held = STATE.hold_plane
    if held:
        log(f"hold kept: {held} is still unapplied; a service deploy does not clear it")
        return
    write_hold(None)


# ── delivering an alert ───────────────────────────────────────────────────────────────────────


def discord(content: str) -> bool:
    """Post to the alert webhook. False on any failure, so the alert is retried next tick."""
    return deploy_alerts.post(CONFIG.discord_webhook, content, log_fn=log)


def deliver(key: str, content: str) -> bool:
    """Post an alert now, queuing it (keyed by "<channel>:<sha>") for retry on a delivery failure.

    A transient webhook blip can't permanently drop it — the ff-merged secrets/tasks/meta/
    combined paths never re-reach their alert code on the next (noop) tick, so `discord()`'s
    own 'retry next tick' doesn't hold for them. drain_pending() resends any queued entry every
    tick. Returns discord()'s result.

    The queue write happens BEFORE the send, so a process death during discord() leaves the
    alert queued rather than lost — see the DECIDED note below.
    """
    pending = deploy_alerts.read_pending(PENDING_ALERTS_FILE)
    # DECIDED: queue BEFORE the send, not after. discord() blocks for up to 10s in urlopen, and
    # alert_once has already advanced its per-SHA marker by the time we get here — so a process
    # death inside that window (a reboot, a `systemctl stop`, the UPS shutdown chain) used to leave
    # a durable "already alerted" marker with nothing delivered and nothing queued, and the
    # ff-merged channels never re-reach their alert code on a later tick. Queue-first trades
    # lost-on-crash for duplicate-on-crash: a death after the 2xx but before the removal write below
    # makes drain_pending() repost once. At-least-once is the right side for an alert.
    queued = apply_send_result(pending, key, content, False)
    if queued != pending:
        # Deliberately uncapped: capping here could evict a real backlogged alert to make room for
        # one that is about to be delivered anyway. The queue may sit at PENDING_ALERTS_MAX + 1 for
        # the length of one discord() call; the post-send write below is what enforces the cap.
        deploy_alerts.write_pending(PENDING_ALERTS_FILE, queued)
    delivered = discord(content)
    # `queued`, NOT `pending`, is the baseline from here on. Comparing the removal against the
    # pre-queue dict would make it a permanent no-op, so the entry would never leave and every
    # alert would repost on every tick.
    updated = apply_send_result(queued, key, content, delivered)
    updated, dropped = cap_pending(updated)
    for stale in dropped:
        # Logged, never silent: this is an alert being discarded undelivered, which is the exact
        # outcome the queue exists to prevent. A backlog this deep means the webhook itself has
        # been broken for over a day, and DISCORD_CONSECUTIVE has been paging about that.
        log(
            f"pending-alert queue over {PENDING_ALERTS_MAX}; dropping oldest undelivered {stale}"
        )
    if updated != queued:
        deploy_alerts.write_pending(PENDING_ALERTS_FILE, updated)
    return delivered


def drain_pending() -> None:
    """Resend every queued-but-undelivered alert.

    Runs first thing each tick — BEFORE the noop/hold/dirty short-circuits — so an alert whose
    original tick ff-merged (local==origin -> the next tick noops) still gets redelivered. Clears
    each entry on a confirmed 2xx.
    """
    pending = deploy_alerts.read_pending(PENDING_ALERTS_FILE)
    if not pending:
        return
    delivered = {k for k, c in pending.items() if discord(c)}
    updated = apply_drain_result(pending, delivered)
    if updated != pending:
        deploy_alerts.write_pending(PENDING_ALERTS_FILE, updated)


def alert_once(marker: str, channel: str, origin: str, content: str) -> None:
    """Deliver a per-SHA-deduped alert on `channel`.

    Args:
        marker: the `DeployerState` marker holding the last SHA alerted on this channel.
        channel: the queue key's prefix.
        origin: the SHA being alerted about.
        content: the message body, from `deploy_alerts`.

    No-op if this origin SHA was already alerted (marker == origin). Otherwise mark DETECTION here
    (advance the marker once per SHA) and hand delivery + retry to deliver()/the pending queue — the
    marker advances on DETECTION, NOT delivery, so a transient webhook blip is redelivered by
    drain_pending() rather than silently dropped, and an ff-merged path that noops next tick doesn't
    re-page.
    """
    if STATE.read(marker) == origin:
        return
    STATE.write(marker, origin)
    deliver(f"{channel}:{origin}", content)


def alert_secrets_deferred(origin: str, cs: ChangeSet) -> None:
    """Alert (once per SHA) that `secrets.yml` was ff-merged with no consumer redeployed.

    Split out of the no-services branch on 2026-08-24 so the k8s auto-deploy path can fire it too.
    That path ff-merges, deploys the promoted service and returns without ever reading cs.secrets,
    so a rotation push and a Renovate image bump landing in the same 30-minute window arrive as ONE
    ChangeSet and the rotated secret goes silently stale — and because the merge already happened,
    no later tick re-evaluates it.

    Why it is safe to fire on the k8s path but NOT on the Docker deploy path: a k8s service is
    promoted to auto-deploy only when its sole changed path is defaults/main.yml — image-bump-only
    by construction (see split_k8s_auto_deploy) — so a promoted service can never itself be the
    secret's consumer. The Docker path is the opposite case: the /add-secret flow ships secrets.yml
    WITH its consuming template, so the consumer IS in cs.services and alerting there would
    false-fire on the happy path. That asymmetry is why this is a separate helper rather than a
    line inside alert_deferred(), which runs on both.
    """
    if not cs.secrets:
        return
    alert_once(
        "secrets_alerted",
        "secrets",
        origin,
        deploy_alerts.secrets_deferred_alert(origin),
    )


def alert_deferred(
    origin: str,
    deployed: set[str],
    cs: ChangeSet,
    declared_k8s: set[str] | None = None,
) -> None:
    """Fire the tasks/, meta/deps.yml, and k8s-role defer-and-alert for changes not redeployed.

    Runs on BOTH the no-services branch (deployed=set()) and after a SUCCESSFUL deploy
    (deployed=cs.services): a combined push (svcA template + svcB meta/deps.yml) deploys svcA but
    leaves svcB's deploy-graph change ff-merged and unapplied. The pending remainder is the pure
    `deferred_service_alerts`; this is its I/O shell (per-SHA dedupe marker + deliver). Each channel
    alerts at most once per origin SHA; its marker advances on DETECTION (deliver() and the pending
    queue own delivery + retry), so a transient webhook blip is redelivered, not silently dropped.

    `declared_k8s` is this host's `platform: k8s` containers_list entries, used to decide whether
    the k8s alert can name a `--tags` redeploy at all (see k8s_remediation). It defaults to None
    for the caller that has not read the inventory; None is treated as the EMPTY set, which makes
    every changed role read as untaggable and prescribes a full deploy. That is the fail-safe
    direction: a full deploy is slower than necessary but always applies the change, whereas a
    `--tags` line for a role with no entry exits 0 having applied nothing.
    """
    declared_k8s = declared_k8s or set()
    pending_tasks, pending_meta = deferred_service_alerts(cs, deployed)
    if pending_tasks:
        alert_once(
            "tasks_alerted",
            "tasks",
            origin,
            deploy_alerts.tasks_deferred_alert(origin, pending_tasks),
        )
    if pending_meta:
        alert_once(
            "meta_alerted",
            "meta",
            origin,
            deploy_alerts.meta_deferred_alert(origin, pending_meta),
        )
    if cs.k8s:
        # No `- deployed` subtraction (unlike tasks/meta): this deployer never auto-deploys a
        # k8s-platform role at all, so there's no scoped redeploy for a k8s change to have ridden.
        #
        # DECIDED: this alert is a one-shot detection, not the durable signal. It fires once per
        # origin SHA (alert_once) and the ff-merge below clears `behind_since` -- the deployer's
        # own "still behind" marker -- so every other monitored marker reads clean while the
        # cluster keeps running the old manifests (issue #947). The durable signal is a daniel-box
        # cron reading `probe.py releases --stale-only` against the release records
        # `roles/k8s/manifests/tasks/release_stamp.yml` writes on every real apply -- see this
        # role's CLAUDE.md, "k8s-platform roles are auto-deployed ONLY for an image-pin bump...".
        alert_once(
            "k8s_alerted",
            "k8s",
            origin,
            deploy_alerts.k8s_deferred_alert(
                origin, cs.k8s, declared_k8s, cs.k8s_consumers
            ),
        )


def check_stale_composes() -> None:
    """Page (once per distinct set) when a rendered compose has no matching containers_list entry.

    containers/<svc>/docker-compose.yml exists on disk but <svc> has no containers_list entry —
    the stale-compose trap (see deploy_inventory.stale_rendered_services for the incident
    history). Detection only, never cleanup: the remedy removes containers and directories, which
    stays an operator action.
    """
    stale = deploy_io.stale_composes(REPO, HOSTNAME)
    if stale is None:
        return  # unreadable inventory/tree — not this watchdog's failure to page about
    marker = ",".join(stale)
    if STATE.read("stale_composes") == (marker or None):
        return
    STATE.write("stale_composes", marker or None)
    if stale:
        deliver(
            f"stale-composes:{marker}",
            deploy_alerts.stale_composes_alert(HOSTNAME, stale),
        )


# ── the staging gate ──────────────────────────────────────────────────────────────────────────


def record_staging_tick(sha: str, gated: set[str], verdict: str) -> None:
    """Append this tick's verdict to the tick ledger. Never raises. See deploy_io."""
    deploy_io.record_staging_tick(
        STAGING_TICK_LEDGER, CHICAGO, datetime.now, sha, gated, verdict
    )


def consume_staging_override() -> bool:
    """Spend the operator's one-tick override, if it is armed. True when it was."""
    return deploy_io.consume_override(STAGING_OVERRIDE_FILE)


def consult_staging(services: set[str], origin: str) -> str:
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
    if not STAGING_GATE:
        return STAGING_SKIPPED
    gated, ungated = staging_scope(services, STAGING_SUBSET)
    if not gated:
        log(staging_verdict_summary(gated, ungated, 0, 0))
        return STAGING_SKIPPED

    deploy_rc, expect_rc = deploy_io.run_staging_scripts(
        REPO,
        origin,
        ",".join(sorted(gated)),
        STAGING_GATE_TIMEOUT_S,
        STAGING_EXPECT_TIMEOUT_S,
    )
    summary = staging_verdict_summary(gated, ungated, deploy_rc, expect_rc)
    log(summary)
    # Alerted, not silent: a journal line alone collects no operator judgement about whether a
    # failure was staging's fault or the change's, which is the one thing the entry condition's
    # false-failure rate is made of.
    if deploy_rc != 0 or expect_rc != 0:
        alert_once(
            "staging_alerted",
            "staging",
            origin,
            deploy_alerts.staging_verdict_alert(origin, summary, STAGING_GATE_BLOCKING),
        )
    verdict = staging_verdict(deploy_rc, expect_rc)
    record_staging_tick(origin, gated, verdict)
    return verdict


# ── the phases ────────────────────────────────────────────────────────────────────────────────


def assess() -> TickTarget:
    """Read git, decide what kind of tick this is, and manage the divergence marker.

    Returns:
        A `TickTarget` carrying both HEADs, the hold, the dirty state and `next_action`'s word.

    Raises:
        RetryableFetchError: `git status` or `git fetch` failed. entrypoint() skips the tick
            cleanly on it, without writing last_run.
    """
    # A dirty working tree (operator may be mid-edit) is a healthy skip, not an outage: we never
    # deploy from it, but the tick completes and writes last_run so a long edit session doesn't
    # falsely trip the GitOps-Alive monitor. (git fetch is safe on a dirty tree — it only updates
    # remote-tracking refs.) Skipping is safe precisely because it does NOT write last_run: a
    # checkout that is genuinely broken keeps failing, ages the marker past GITOPS_MAX_AGE_S and
    # still pages via GitOps-Alive ~60min later, instead of double-paging 48x/day forever.
    status = deploy_io.git_status(REPO)
    if status.returncode != 0:
        raise RetryableFetchError(
            status.stderr.strip() or f"git status exited {status.returncode}"
        )
    dirty = bool(status.stdout.strip())

    fetch = deploy_io.git_fetch(REPO, BRANCH)
    if fetch.returncode != 0:
        raise RetryableFetchError(
            fetch.stderr.strip() or f"git fetch exited {fetch.returncode}"
        )
    local = deploy_io.run(["git", "rev-parse", "HEAD"], cwd=REPO)
    # Pinned ONCE, and every decision below plus every merge uses this value rather than
    # re-resolving `origin/<branch>`. The CI verdict, the changed-path diff, the denylist read and
    # the broad marker all evaluate against this exact commit; a merge that re-resolved the ref
    # could land a DIFFERENT one, and `--ff-only` would happily accept it because it is still a
    # descendant. That commit's CI was never checked (REQUIRE_CI defaults true), its paths were
    # never classified — and because the tree then equals origin, next_action() returns "noop"
    # from that point on, so it is never deployed and never defer-and-alerted either, with the
    # hold marker and the behind-origin watchdog both reading green.
    #
    # The window is real, not theoretical: `scripts/deploy.sh` runs deploy_staleness.py (which
    # fetches) BEFORE it takes /var/lock/server-git-tree.lock, and --dry-run returns before the
    # lock entirely — so a dry run in another session moves this repo's remote-tracking ref
    # mid-tick. The ref lives in the shared .git dir every worktree points at.
    origin = deploy_io.run(["git", "rev-parse", f"origin/{BRANCH}"], cwd=REPO)
    hold = read_hold()

    # origin is "ahead" only if local is an ancestor of it — i.e. it carries commits we don't
    # have. If origin is behind (the operator committed locally but hasn't pushed) or the two
    # diverged, there is nothing to fast-forward and next_action() makes this a no-op instead of
    # mis-firing on the reverse diff.
    origin_ahead = is_ancestor(local, origin)
    # Divergence watchdog: if local and origin differ but neither is an ancestor of the other, the
    # deployer can't fast-forward and every tick noops while origin's new commits never deploy —
    # invisible otherwise (last_run keeps ticking, no hold). Record it so GitOps Status pages; clear
    # it once resolved. A committed-but-unpushed local commit (local_ahead — secret-rotate's domain)
    # is a plain noop, NOT flagged here. Managed every tick regardless of `action`.
    local_ahead = is_ancestor(origin, local)
    STATE.write(
        "diverged",
        origin if is_diverged(origin, local, origin_ahead, local_ahead) else None,
    )
    # Only spend the GitHub call on a tick that would otherwise deploy. These conditions mirror
    # next_action's own short-circuits above it, so a noop/dirty/held tick costs no API request —
    # which keeps the gate's share of the GitHub rate limit at one request per 30 min.
    ci = "pass"
    if not dirty and origin_ahead and origin != local and origin != hold:
        ci = fetch_ci_verdict(origin)
    return TickTarget(
        local=local,
        origin=origin,
        hold=hold,
        dirty=dirty,
        status=status.stdout,
        action=next_action(local, origin, hold, dirty, origin_ahead, ci),
    )


def plan_tick(target: TickTarget) -> TickPlan:
    """Classify the incoming range into the ChangeSet this tick will act on.

    Runs BEFORE the ff-merge, so every read here is at the pinned `origin` rather than the
    working tree — see `deploy_io.k8s_declarations_at`.
    """
    paths = deploy_io.run(
        ["git", "diff", "--name-only", f"{target.local}..{target.origin}"], cwd=REPO
    ).splitlines()
    # A comment-only edit to a bring-up playbook is not a change the deployer must park on;
    # it parked three sessions' landings on 2026-09-02 (PR #746) until an operator ff-merged
    # by hand. The paths dropped here would have set broad_manual by prefix alone.
    quiet = comment_only_broad_changes(
        paths,
        target.local,
        target.origin,
        lambda ref, p: deploy_io.run(["git", "show", f"{ref}:{p}"], cwd=REPO),
    )
    if quiet:
        log(
            f"comment-only change in {', '.join(sorted(quiet))} — "
            "not parking; the tick treats it as no change"
        )
        paths = [p for p in paths if p not in quiet]
    cs = services_from_changed_paths(paths)
    cs.k8s_consumers = shared_module_consumers(paths, REPO)
    # A path under ansible/roles/containers/<svc>/ maps to <svc> by NAME ALONE — it doesn't know
    # this host might run that same-named service under k8s (wg-easy: a Docker role, but
    # platform: k8s on daniel-box). Route those into the k8s defer-and-alert set instead of
    # deploying a tag that resolves to deploy.yml's K8S play (an idempotent no-op whose health
    # gate silently no-ops too, since containers_for() renders nothing for a k8s entry).
    hostvars = deploy_io.host_vars_text(REPO, HOSTNAME)
    k8s_services = declared_k8s_services(hostvars) if hostvars is not None else set()
    cs = reroute_k8s_services(cs, k8s_services)
    cs = _promote_k8s_auto_deploys(cs, paths, target)
    return TickPlan(cs=cs, paths=paths, k8s_services=k8s_services)


def _promote_k8s_auto_deploys(
    cs: ChangeSet, paths: list[str], target: TickTarget
) -> ChangeSet:
    """Move image-bump-only k8s changes from defer-and-alert into the auto-deploy channel.

    Disarms itself first when this host's baked denylist disagrees with the declarations at
    origin: that config is rendered only by `initial_setup.yml --tags gitops_deploy`, while a
    declaration flip lands under roles/k8s/ and alerts naming `deploy.yml` — a playbook that
    never re-renders it. Without the check the host would keep acting on the old list, leaving a
    role that was just denied still auto-deployable. Disarm loudly rather than acting on a stale
    boundary.
    """
    autodeploy_enabled = K8S_AUTODEPLOY_ENABLED
    k8s_defaults_at_origin: dict[str, str | None] = {}
    if autodeploy_enabled:
        try:
            # `target.origin` (the SHA pinned in assess(), not f"origin/{BRANCH}") — the diff and
            # the alert already evaluate against that exact commit; re-resolving the ref here
            # would open a TOCTOU where a concurrent fetch lands between the two reads.
            k8s_defaults_at_origin = deploy_io.k8s_declarations_at(REPO, target.origin)
            declared = declared_denylist(k8s_defaults_at_origin)
            read_error = None
        except Exception as exc:
            k8s_defaults_at_origin = {}
            declared = None
            read_error = f"{type(exc).__name__}: {exc}"
            log(f"could not read k8s declarations at origin: {read_error}")
        if declared is None or declared != K8S_AUTODEPLOY_DENYLIST:
            autodeploy_enabled = False
            if declared is not None:
                added = sorted(declared - K8S_AUTODEPLOY_DENYLIST)
                removed = sorted(K8S_AUTODEPLOY_DENYLIST - declared)
                detail = (
                    f"denied at origin but not in config: {added or 'none'}; "
                    f"in config but not at origin: {removed or 'none'}"
                )
                # Both directions are usually "config is behind origin" and want the same fix:
                # a re-render. `added` means a role was newly denied at origin; `removed` means a
                # role was PROMOTED there — the denylist shrank — which this host has not picked
                # up yet. `removed` has one other cause, an operator who rendered locally before
                # pushing, so it names that as a secondary check. Naming `git push` FIRST on
                # `removed` was wrong: it is the less common cause and the fix does nothing for
                # the other one, which is what a promotion looks like.
                fix = (
                    "run `uv run ansible-playbook ansible/initial_setup.yml --tags "
                    "gitops_deploy` on the host (`deploy.yml` does not re-render config.env)"
                )
                if removed and not added:
                    fix += (
                        ". If that changes nothing, the config was rendered from an unpushed "
                        "tree instead — `git push` it and re-render"
                    )
            else:
                detail = f"the declarations at origin could not be read ({read_error})"
                fix = "check the ref/path on the host — this clears on its own once it reads again"
            log(f"k8s auto-deploy disarmed — stale denylist ({detail})")
            alert_once(
                "stale_denylist_alerted",
                "stale_denylist",
                target.origin,
                deploy_alerts.stale_denylist_alert(target.origin, detail, fix),
            )
    # Everything not promoted stays in cs.k8s and defer-and-alerts exactly as before, so this is
    # inert until a service passes BOTH the diff-shape test and the denylist.
    return split_k8s_auto_deploy(
        cs,
        paths,
        denylist=K8S_AUTODEPLOY_DENYLIST,
        pilot=K8S_AUTODEPLOY_PILOT,
        enabled=autodeploy_enabled,
        image_only=lambda svc: is_image_only_diff(
            deploy_io.k8s_image_diff(REPO, target.local, target.origin, svc)
        ),
        max_per_tick=K8S_AUTODEPLOY_MAX_PER_TICK,
        # Read at the PINNED origin, like the denylist above and for the same reason — the
        # promotion decision runs before the ff-merge, so the working tree still holds the
        # pre-merge declarations. `.get(svc)` (not `[svc]`): a role absent from the listing is
        # already denied by the stale-denylist comparison, and an absent entry must not raise
        # here.
        declares_claims=lambda svc: declares_snapshot_claims(
            k8s_defaults_at_origin.get(svc)
        ),
        max_claim_services_per_tick=K8S_AUTODEPLOY_MAX_CLAIM_SERVICES_PER_TICK,
    )


def handle_dirty(target: TickTarget) -> int:
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
    now_ct = datetime.now(CHICAGO)
    if should_alert_dirty(
        now_ct,
        STATE.read("dirty_alerted"),
        DIRTY_ALERT_MORNING_HOUR,
        DIRTY_ALERT_EVENING_HOUR,
    ):
        # Mark as alerted only on confirmed delivery, else retry next tick (see discord()).
        if discord(deploy_alerts.dirty_tree_alert(HOSTNAME)):
            STATE.write(
                "dirty_alerted",
                dirty_alert_slot(
                    now_ct, DIRTY_ALERT_MORNING_HOUR, DIRTY_ALERT_EVENING_HOUR
                ),
            )
    return 0


def handle_ci_failed(target: TickTarget) -> int:
    """Master is red: stay on `local`, page once per SHA."""
    alert_once(
        "ci_alerted",
        "ci",
        target.origin,
        deploy_alerts.ci_failed_alert(HOSTNAME, target.local, target.origin),
    )
    log(f"origin {target.origin[:8]}: CI failed — not deploying")
    return 0


def handle_broad(target: TickTarget, plan: TickPlan) -> int:
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
        alert_once(
            "broad_alerted",
            "broad",
            origin,
            deploy_alerts.broad_deferred_alert(
                origin,
                broad_remediation(
                    cs.broad_deploy, cs.broad_setup, cs.setup_roles, BRANCH
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
    deploy_io.run(["git", "merge", "--ff-only", origin], cwd=REPO)

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
        deploy_io.deploy_broad(REPO, playbook, tags, BROAD_DEPLOY_TIMEOUT_S)
    except Exception as exc:
        log(f"broad apply failed ({playbook} {tags}): {exc}")
        write_hold(origin)
        STATE.write("hold_plane", hold_plane_marker(playbook, tags))
        posted = discord(
            deploy_alerts.broad_failure_alert(
                HOSTNAME, playbook, tags, origin, exc, HOLD_FILE, HOLD_PLANE_FILE
            )
        )
        # Exit 0 on a delivered detailed post so systemd's OnFailure generic curl doesn't
        # double-page; exit 1 only if the post failed, leaving OnFailure the backstop.
        return 0 if posted else 1

    clear_broad_hold(playbook, tags)
    alert_secrets_deferred(origin, cs)
    alert_deferred(origin, set(), cs, plan.k8s_services)
    return 0


def handle_k8s(target: TickTarget, plan: TickPlan) -> int:
    """The promoted k8s image bumps: consult staging, ff-merge, deploy, roll back on failure."""
    cs, local, origin = plan.cs, target.local, target.origin
    # DECIDED: consult the gate BEFORE the ff-merge, never after. consult_staging blocks for up
    # to STAGING_GATE_TIMEOUT_S + STAGING_EXPECT_TIMEOUT_S, and a process death inside that
    # window used to leave local == origin with nothing deployed — next_action() then returns
    # noop forever (the SHA is already merged, so nothing re-triggers), `last_run` keeps ticking,
    # and both Kuma tiles stay green over a permanently stranded deploy. Merging after the gate
    # makes the same death self-healing: local is still behind, so the next tick re-evaluates.
    # Whether the verdict blocks is staging_blocks' decision; while STAGING_GATE_BLOCKING is
    # false it never does, and this branch is the slice-3 behaviour unchanged.
    verdict = consult_staging(cs.k8s_deploy, origin)
    if staging_blocks(verdict, blocking=STAGING_GATE_BLOCKING):
        if consume_staging_override():
            discord(
                deploy_alerts.staging_override_alert(
                    HOSTNAME, origin, STAGING_OVERRIDE_FILE
                )
            )
            log(f"staging rejected {origin[:8]}; override armed, deploying prod anyway")
        else:
            # No reset and no volume revert: consult_staging runs BEFORE the ff-merge, so the
            # tree is still on `local` and prod was never applied. That asymmetry is Phase C's
            # main prize — a staging failure costs nothing to undo. Do not add a reset here
            # without also moving the gate, or the two will disagree.
            write_hold(origin)
            posted = discord(
                deploy_alerts.staging_rejected_alert(
                    HOSTNAME, local, origin, cs.k8s_deploy, STAGING_OVERRIDE_FILE
                )
            )
            log(f"staging rejected {origin[:8]}; holding, prod not deployed")
            return 0 if posted else 1
    deploy_io.run(["git", "merge", "--ff-only", origin], cwd=REPO)
    try:
        deploy_io.deploy_k8s(REPO, cs.k8s_deploy, K8S_DEPLOY_TIMEOUT_S)
    except Exception as exc:
        return _rollback_k8s(target, plan, exc)
    # The ONLY place a hold can clear on an all-k8s host. write_hold(None) otherwise lives
    # solely in the Docker health-gate branch below, which such a host never reaches — so
    # without this the first rollback would leave GitOps Deploy — Status red forever and
    # need a manual rm (the trap this role's CLAUDE.md documents).
    clear_service_hold()
    # Only after the gate inside deploy_k8s has passed and the hold is cleared — annotating
    # from inside the try would mark a deploy that the rollout gate went on to reject.
    deploy_io.emit_deploy_annotation(cs.k8s_deploy, origin)
    # A promoted k8s service is image-bump-only, so it is never the consumer of a secret that
    # rode along in the same tick. Without this the rotated value is ff-merged and forgotten.
    alert_secrets_deferred(origin, cs)
    alert_deferred(origin, cs.k8s_deploy, cs, plan.k8s_services)
    return 0


def _rollback_k8s(target: TickTarget, plan: TickPlan, exc: Exception) -> int:
    """Undo a failed k8s deploy: hold, reset, redeploy the prior pin, revert claimed volumes."""
    cs, local, origin = plan.cs, target.local, target.origin
    # Hold BEFORE the reset, same as the Docker paths: a hung rollback redeploy would otherwise
    # be SIGTERMed before the marker is written, stranding the bad commit into a per-tick
    # redeploy loop.
    log(f"k8s deploy failed for {sorted(cs.k8s_deploy)}: {exc}; rolling back")
    write_hold(origin)
    deploy_io.run(["git", "reset", "--hard", local], cwd=REPO)
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
            REPO, cs.k8s_deploy, K8S_ROLLBACK_TIMEOUT_S, restore_sha=origin[:8]
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
        if declares_snapshot_claims(deploy_io.read_local_k8s_default(REPO, svc))
    )
    revert_note = rollback_volume_revert_note(
        cs.k8s_deploy,
        reverting,
        str(rollback_failed) if rollback_failed else None,
    )
    posted = discord(
        deploy_alerts.k8s_failure_alert(
            HOSTNAME, local, origin, cs.k8s_deploy, exc, revert_note
        )
    )
    return 0 if posted else 1


def handle_no_services(target: TickTarget, plan: TickPlan) -> int:
    """Nothing maps to a deploy here: ff-merge, then flag what rode along unapplied."""
    cs, origin = plan.cs, target.origin
    deploy_io.run(["git", "merge", "--ff-only", origin], cwd=REPO)  # docs-only etc.
    # A secrets-only push (rotated value, no service template changed) maps to nothing, so the
    # ff-merge above is all we can do automatically — but the new value only reaches a container
    # on its next deploy. Defer-and-alert (once per SHA) so the operator redeploys the
    # consumer(s); without this the rotated secret sits stale.
    alert_secrets_deferred(origin, cs)
    # tasks/ and meta/deps.yml changes aren't auto-deployed but DO change what a deploy does, so
    # they must not sit silently ff-merged. Nothing was deployed this tick (deployed=set()), so
    # the full sets are flagged. Same helper runs on the deploy path for a combined push.
    alert_deferred(origin, set(), cs, plan.k8s_services)
    return 0


def handle_docker(target: TickTarget, plan: TickPlan) -> int:
    """Deploy this host's Docker services, health-gate them, and roll back if the gate fails."""
    cs, local, origin = plan.cs, target.local, target.origin
    deploy_io.run(["git", "merge", "--ff-only", origin], cwd=REPO)
    try:
        deploy_io.deploy(REPO, cs.services)
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
        write_hold(origin)
        deploy_io.run(["git", "reset", "--hard", local], cwd=REPO)
        try:
            deploy_io.deploy(REPO, cs.services)
        except Exception as exc2:
            log(f"rollback redeploy of the prior version also failed: {exc2}")
        posted = discord(
            deploy_alerts.deploy_failure_alert(
                HOSTNAME, local, origin, cs.services, exc
            )
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
    skipped = sorted(s for s in cs.services if not deploy_io.containers_for(REPO, s))
    if skipped:
        log(f"not deployed on this host; skipping health gate: {skipped}")
    # Budget the gate so gate+rollback finishes inside the unit's TimeoutStartSec (see
    # RUN_BUDGET_S): once the deadline passes, gate_services marks the rest failed and we roll
    # back, rather than polling to HEALTH_TIMEOUT_S per container and getting SIGTERMed mid-gate
    # (which would strand the bad commit live). RUN_START is measured from process start.
    gate_deadline = RUN_START + RUN_BUDGET_S
    failed = gate_services(
        cs.services,
        lambda svc, deadline: deploy_io.service_healthy(REPO, svc, TIMEOUT, deadline),
        gate_deadline,
        time.time,
    )
    if not failed:
        clear_service_hold()
        # Combined-push safety: a tasks/ or meta/deps.yml change bundled for a service OTHER than
        # the one(s) just deployed is ff-merged but unapplied — flag that remainder (a bundled
        # change to a DEPLOYED service rode its own --tags redeploy, so it's excluded). Only on a
        # clean deploy: a rollback below git-resets the whole commit, reverting those changes too.
        alert_deferred(origin, cs.services, cs, plan.k8s_services)
        return 0
    if time.time() >= gate_deadline:
        log(f"health-gate budget ({RUN_BUDGET_S}s) exhausted before gating completed")

    # Rollback: reset to prior HEAD, redeploy the prior version. Redeploy the WHOLE batch
    # (cs.services), not just `failed`: in a multi-service tick the services that DID pass
    # were recreated on the new images, so after the git reset they'd otherwise stay on the
    # new images while the tree points at old — partial-batch drift. Hold BEFORE the reset +
    # redeploy (see the exec-failure path above): a hung rollback redeploy would otherwise be
    # SIGTERMed before write_hold, stranding the bad commit into a per-tick redeploy loop.
    log(f"health gate failed for {failed}; rolling back to {local[:8]}")
    write_hold(origin)
    deploy_io.run(["git", "reset", "--hard", local], cwd=REPO)
    try:
        deploy_io.deploy(REPO, cs.services)
    except Exception as exc:
        log(f"rollback redeploy of the prior version also failed: {exc}")
    posted = discord(deploy_alerts.rollback_alert(HOSTNAME, local, origin, failed))
    # Exit 0 on a delivered detailed post so OnFailure's generic curl doesn't double-page (see the
    # exec-failure path above); exit 1 only if the detailed post failed, leaving OnFailure the backstop.
    return 0 if posted else 1


def main() -> int:
    """Run one gitops-deploy tick end to end, as a sequence of named phases.

    `assess()` reads git and classifies the tick; `plan_tick()` turns the incoming range into a
    ChangeSet; one `handle_*` phase owns each terminal branch and returns the exit code. The
    branch order is load-bearing — broad before k8s before Docker — because a broad change and a
    promoted image bump can arrive in the same range and the broad plane has to win.

    Almost always returns 0 — a failed tick pages via Discord and the hold marker rather than a
    non-zero exit. The exceptions are the few `0 if posted else 1` branches, reached only when
    even the failure alert itself could not be delivered.

    Raises:
        deploy_io.ConfigError: config.env holds a value this deployer cannot use.
        RuntimeError: there is no config at all, so there is no repo to tick.
        RetryableFetchError: from `assess()`; entrypoint() skips the tick on it.
    """
    CONFIG.validate()
    if not REPO:
        # No config, no repo to tick: page via the crash handler rather than run every git
        # command below against cwd="".
        raise RuntimeError(f"REPO_DIR is unset: no deployer config at {CONFIG_PATH}")
    # Resend any alert a prior tick failed to deliver, BEFORE any short-circuit below: the ff-merged
    # secrets/tasks/meta/combined paths never re-reach their alert code (local==origin -> noop), so a
    # transient webhook failure is only recoverable here, not by discord()'s per-tick re-eval.
    drain_pending()
    # Disk-only, independent of git state, so it runs before any branch can short-circuit the
    # tick: page (once per distinct set) when a rendered compose has no containers_list entry —
    # the stale-compose trap, twice now the cause of a phantom health gate + false rollback + hold.
    check_stale_composes()

    target = assess()
    if target.action == "dirty":
        return handle_dirty(target)
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
        return handle_ci_failed(target)

    plan = plan_tick(target)
    if plan.cs.broad:
        return handle_broad(target, plan)
    if plan.cs.k8s_deploy:
        return handle_k8s(target, plan)
    if not plan.cs.services:
        return handle_no_services(target, plan)
    return handle_docker(target, plan)


def entrypoint() -> int:
    """One tick as systemd runs it.

    main() plus the exit-code contract around it. Returns the process exit code; the `__main__`
    guard below only hands it to sys.exit, so a test can call this directly
    (test_gitops_deploy_fetch_skip.py).
    """
    try:
        rc = main()
    except RetryableFetchError as e:
        # Transient `git fetch` failure: skip this tick without paging (no crash Discord, and exit 0
        # so the OnFailure alert unit doesn't fire either) and WITHOUT writing last_run — a one-off
        # blip is invisibly retried next tick, while a persistent fetch break ages last_run and trips
        # GitOps-Alive. Must precede the generic handler below (Python matches except-clauses in order).
        log(f"git fetch failed (retryable) — skipping tick, will retry next run: {e}")
        return 0
    except deploy_io.ConfigError as e:
        # One clear line, not a traceback. This was an unhandled ValueError raised during IMPORT
        # until load_config deferred the parse, so it reached an operator as a stack trace with no
        # key name in it, before any of the alerting below existed in the process.
        log(f"gitops-deploy: {e}")
        posted = discord(deploy_alerts.bad_config_alert(HOSTNAME, CONFIG_PATH, e))
        # Exit 0 on a delivered detailed post so OnFailure's generic curl doesn't double-page,
        # same convention as the other `0 if posted else 1` branches; exit 1 only if the
        # detailed post itself failed, leaving OnFailure the backstop.
        return 0 if posted else 1
    except Exception as e:
        discord(deploy_alerts.crash_alert(e))
        raise
    # Liveness marker: a tick that completed without crashing (incl. a rollback, rc=1).
    # monitor-bridge reads this; a crash skips the write so the Alive monitor goes stale.
    _record_behind()
    STATE.write("last_run", str(time.time()))
    return rc


if __name__ == "__main__":
    sys.exit(entrypoint())
