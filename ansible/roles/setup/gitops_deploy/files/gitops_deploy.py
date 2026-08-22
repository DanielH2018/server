#!/usr/bin/env python3
"""GitOps deployer — runs once per systemd-timer tick, on every host with has_gitops set.

Flow: fetch origin/master; if it advanced, require the tip's CI to be green; map changed
templates to services; ff-merge; deploy each via the existing ansible-playbook path;
health-gate each container. On failure: reset to the previous HEAD, redeploy the prior version,
record the bad SHA as a hold marker, and alert the dedicated Discord webhook.

Config comes from /etc/gitops-deploy/config.env (KEY=VALUE), written by Ansible:
  REPO_DIR, BRANCH, HOSTNAME, DISCORD_WEBHOOK, HEALTH_TIMEOUT_S,
  REQUIRE_CI, CI_CONTEXTS, GITHUB_REPO
Stdlib only.
"""

from __future__ import annotations

import json
import os
import pathlib
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from deploy_logic import (  # noqa: E402
    ChangeSet,
    apply_drain_result,
    apply_send_result,
    behind_marker,
    broad_remediation,
    ci_verdict,
    containers_to_gate,
    declared_denylist,
    declared_k8s_services,
    declared_services,
    declares_snapshot_claims,
    deferred_service_alerts,
    dirty_alert_slot,
    gate_services,
    health_decision,
    is_diverged,
    is_image_only_diff,
    k8s_role_paths,
    next_action,
    reroute_k8s_services,
    rollback_volume_revert_note,
    services_from_changed_paths,
    should_alert_dirty,
    split_k8s_auto_deploy,
    stale_rendered_services,
)
from host_lib import atomic_write, discord_post, parse_env_file  # noqa: E402


class RetryableFetchError(Exception):
    """A transient `git fetch origin` failure (GitHub blip, momentary DNS). __main__ turns this into
    a CLEAN skip of the tick — exit 0, NO in-script Discord crash-page, NO OnFailure — that also does
    NOT refresh last_run. So a one-off blip is silently retried next tick, while a PERSISTENT fetch
    failure still surfaces via GitOps-Alive going stale over several missed ticks. Distinct from a
    real crash (unexpected exception), which still pages. Before this, a `run()`-raised fetch error
    propagated to __main__ and double-paged (the crash Discord + the OnFailure unit) every 30-min
    tick for the whole duration of a GitHub-side incident."""


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
DIRTY_ALERT_MORNING_HOUR = 8
DIRTY_ALERT_EVENING_HOUR = 20
# Host clock is UTC; the operator wants the twice-daily reminder at 08:00 and 20:00 local time.
CHICAGO = ZoneInfo("America/Chicago")


def cfg() -> dict[str, str]:
    return parse_env_file("/etc/gitops-deploy/config.env")


C = cfg()
REPO = C["REPO_DIR"]
BRANCH = C.get("BRANCH", "master")
HOSTNAME = C.get("HOSTNAME", "unknown-host")
TIMEOUT = int(C.get("HEALTH_TIMEOUT_S", "300"))
# Wall-clock budget (measured from process start, RUN_START) for the whole run's health-gating
# phase. Once spent, the gate stops and rolls back so the rollback (git reset + one redeploy)
# still finishes inside the unit's TimeoutStartSec (25min) — otherwise systemd SIGTERMs the
# deployer mid-gate, before write_hold()/rollback, and the bad commit is left live. RUN_START is
# measured AFTER `flock -w 180` acquires, but TimeoutStartSec counts the flock wait too, so the
# budget is sized 180 (max flock wait) + 1020 (this gate) + 300 (HEALTH_TIMEOUT_S) = 1500 = the
# 25min timeout, keeping the rollback intact even under max lock contention with the weekly
# secret-rotate. See gitops-deploy.service.j2.
RUN_BUDGET_S = int(C.get("RUN_BUDGET_S", "1020"))
RUN_START = time.time()


def run(
    args: list[str],
    cwd: str | None = REPO,
    check: bool = True,
    timeout: float | None = None,
) -> str:
    # timeout defaults to None so the long deploy/git calls are unbounded as before;
    # only the health-gate's docker inspects and the k8s deploy/rollback calls pass one.
    if timeout is None:
        r = subprocess.run(args, cwd=cwd, text=True, capture_output=True, timeout=None)
    else:
        # `uv run ansible-playbook ...` is a GRANDCHILD of this process (uv forks it rather
        # than exec'ing into it). `subprocess.run(timeout=)` DOES return on time even so — its
        # internal communicate() raises on the wall-clock deadline, not on pipe EOF — but on
        # timeout it kills only the direct child (uv). The grandchild (ansible-playbook) is
        # left running, unkilled, an orphan mutating the cluster with nothing left watching it.
        # Verified empirically: a plain subprocess.run(timeout=) returns promptly, and the
        # grandchild is still alive at that moment. That is how K8S_ROLLBACK_TIMEOUT_S stopped
        # being an actual bound on the underlying work: gitops_deploy.py moves on (to a second
        # rollback attempt, or exits and lets the next tick start a fresh run) while the timed-
        # out ansible-playbook keeps applying manifests in the background — the real stop
        # becomes whatever kills that orphan, normally nothing, or systemd's TimeoutStartSec
        # SIGTERM against the WRAPPING unit, which can land mid-rollback.
        #
        # start_new_session puts the direct child in a NEW process group (its pgid equals its
        # own pid), which every process it forks inherits unless one of them calls setsid
        # itself. killpg on timeout then signals that whole group at once, so uv and
        # ansible-playbook die together instead of one outliving the other.
        proc = subprocess.Popen(
            args,
            cwd=cwd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass  # the group is already gone
            # wait(), not communicate(): if a descendant escaped the group by calling setsid
            # itself, its end of the pipe stays open and communicate() would block on it
            # forever. wait() only reaps the direct child's exit status and doesn't touch the
            # pipes — CPython's own subprocess.run() does the same on this path.
            proc.wait()
            raise
        r = subprocess.CompletedProcess(args, proc.returncode, stdout, stderr)
    if check and r.returncode != 0:
        raise RuntimeError(f"{' '.join(args)} -> {r.returncode}\n{r.stderr}")
    return r.stdout.strip()


def log(msg: str) -> None:
    print(msg, flush=True)


def _csv_set(raw: str) -> frozenset[str]:
    return frozenset(s.strip() for s in raw.split(",") if s.strip())


# ── k8s auto-deploy ───────────────────────────────────────────────────────────────────────────
# Defined AFTER log() so the fail-closed guard below can report why it disarmed itself.
# OFF unless the host explicitly enables it, so a host that has not re-templated config.env
# behaves exactly as it does today.
K8S_AUTODEPLOY_ENABLED = C.get("K8S_AUTODEPLOY_ENABLED", "false").lower() == "true"
K8S_AUTODEPLOY_PILOT = _csv_set(C.get("K8S_AUTODEPLOY_PILOT", ""))
# 0 disables the cap. See split_k8s_auto_deploy: the whole promoted set shares one
# ansible-playbook run and one K8S_DEPLOY_TIMEOUT_S, and a timeout rolls the batch back
# together.
K8S_AUTODEPLOY_MAX_PER_TICK = int(C.get("K8S_AUTODEPLOY_MAX_PER_TICK", "0"))
K8S_AUTODEPLOY_DENYLIST = _csv_set(C.get("K8S_AUTODEPLOY_DENYLIST", ""))
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
K8S_DEPLOY_TIMEOUT_S = int(C.get("K8S_DEPLOY_TIMEOUT_S", "900"))
# Bounds the ROLLBACK redeploy specifically — the run that also reverts each claimed volume to
# its pre-deploy snapshot (k8s/volume-revert), which is strictly more work than a forward deploy.
# Sizing, the batch-summation gap this does NOT cover, and the lock-hold consequence all live in
# defaults/main.yml's gitops_deploy_k8s_rollback_timeout_s comment — this fallback is only what a
# host runs on before its config.env is re-templated with the new value.
K8S_ROLLBACK_TIMEOUT_S = int(C.get("K8S_ROLLBACK_TIMEOUT_S", "1320"))

# ── CI gate ───────────────────────────────────────────────────────────────────────────────────
# Refuse to deploy a master tip whose CI is red or unfinished. Without this the deployer applies
# whatever landed on master, green or red: nothing in the pull path ever consulted a workflow
# result, so a broken commit reached the homelab on the next 30-min tick.
#
# OFF unless config.env says otherwise, so a host that has not been re-templated keeps its current
# behaviour, and REQUIRE_CI=false is the documented way back out.
REQUIRE_CI = C.get("REQUIRE_CI", "false").lower() == "true"
# GitHub check-run NAMES that must be green — the same strings branch protection calls contexts.
# Comma-separated; the names contain spaces and parens, never commas.
CI_CONTEXTS = _csv_set(C.get("CI_CONTEXTS", ""))
CI_REPO = C.get("GITHUB_REPO", "")
if REQUIRE_CI and not (CI_CONTEXTS and CI_REPO):
    # Fail closed the same way the k8s denylist does, but in the opposite direction: an empty
    # context list would make ci_verdict() return `pass` for everything, turning a half-rendered
    # config.env into a silently ungated deployer. Better to disarm loudly.
    log(
        "REQUIRE_CI is set but CI_CONTEXTS/GITHUB_REPO is empty — disabling the CI gate"
    )
    REQUIRE_CI = False


def fetch_ci_verdict(sha: str) -> str:
    """`pass` / `pending` / `fail` for `sha`, from GitHub's check-runs API.

    The repo is public, so this is an unauthenticated GET — no token to provision or rotate. The
    rate limit is 60/hour per IP against one call per 30-min tick.

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
    """True if `ancestor` is an ancestor of (or equal to) `descendant`. Used to
    decide whether origin is strictly ahead of local — only then is there
    anything to fast-forward and deploy (see next_action's origin_ahead). A git
    error (bad object, etc.) is a non-zero exit and conservatively reads False,
    so the tick degrades into a no-op rather than a mis-fired deploy."""
    r = subprocess.run(
        ["git", "merge-base", "--is-ancestor", ancestor, descendant],
        cwd=REPO,
        capture_output=True,
    )
    return r.returncode == 0


def _read_marker(path: str) -> str | None:
    try:
        with open(path) as fh:
            return fh.read().strip() or None
    except FileNotFoundError:
        return None


def _write_marker(path: str, sha: str | None) -> None:
    if sha is None:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
    else:
        atomic_write(path, sha)  # torn-write-safe temp+rename, see host_lib


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
        local = run(["git", "rev-parse", "HEAD"])
        origin = run(["git", "rev-parse", f"origin/{BRANCH}"])
        behind = origin != local and is_ancestor(local, origin)
        _write_marker(
            BEHIND_FILE,
            behind_marker(behind, origin, _read_marker(BEHIND_FILE), time.time()),
        )
    except Exception as e:  # noqa: BLE001 - never fail the tick over a status marker
        log(f"could not record behind-origin state: {e}")


def read_hold() -> str | None:
    return _read_marker(HOLD_FILE)


def write_hold(sha: str | None) -> None:
    _write_marker(HOLD_FILE, sha)


def discord(content: str) -> bool:
    """Post to the alert webhook via the shared host_lib.discord_post — see there for the
    Cloudflare-1010 User-Agent + 2xx-only-success contract the per-SHA dedupe markers gate on. A
    missing webhook or any error returns False, so the alert is retried on the next tick."""
    return discord_post(C.get("DISCORD_WEBHOOK", ""), content, "gitops-deploy", log=log)


def _read_pending() -> dict[str, str]:
    try:
        with open(PENDING_ALERTS_FILE) as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    # Split (not `except (A, B)`): ruff (3.14 target, from requires-python) reformats a
    # parenthesized tuple into the 3.14-only `except A, B:` form. Two clauses give ruff nothing
    # to rewrite. Still load-bearing: unlike its siblings this unit has NOT yet moved to the
    # pinned 3.14 (docs/host-python-314-plan.md, task 6), so it runs on the host's 3.12 today and
    # the rewritten form would SyntaxError. Keep the split until that task lands.
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError:
        return {}


def _write_pending(pending: dict[str, str]) -> None:
    # atomic_write does the same makedirs + temp + os.replace (see host_lib) — a torn write mustn't
    # strand the queue; already used for the SHA markers above.
    atomic_write(PENDING_ALERTS_FILE, json.dumps(pending))


def deliver(key: str, content: str) -> bool:
    """Post an alert now, queuing it (keyed by "<channel>:<sha>") for retry on a delivery FAILURE so a
    transient webhook blip can't permanently drop it — the ff-merged secrets/tasks/meta/combined paths
    never re-reach their alert code on the next (noop) tick, so `discord()`'s own 'retry next tick'
    doesn't hold for them. drain_pending() resends any queued entry every tick. Returns discord()'s result."""
    pending = _read_pending()
    delivered = discord(content)
    updated = apply_send_result(pending, key, content, delivered)
    if updated != pending:
        _write_pending(updated)
    return delivered


def drain_pending() -> None:
    """Resend every queued-but-undelivered alert. Runs first thing each tick — BEFORE the
    noop/hold/dirty short-circuits — so an alert whose original tick ff-merged (local==origin -> the
    next tick noops) still gets redelivered. Clears each entry on a confirmed 2xx."""
    pending = _read_pending()
    if not pending:
        return
    delivered = {k for k, c in pending.items() if discord(c)}
    updated = apply_drain_result(pending, delivered)
    if updated != pending:
        _write_pending(updated)


def alert_once(marker_file: str, channel: str, origin: str, content: str) -> None:
    """Deliver a per-SHA-deduped alert on `channel`. No-op if this origin SHA was already
    alerted (marker == origin). Otherwise mark DETECTION here (advance the marker once per SHA)
    and hand delivery + retry to deliver()/the pending queue — the marker advances on DETECTION,
    NOT delivery, so a transient webhook blip is redelivered by drain_pending() rather than
    silently dropped, and an ff-merged path that noops next tick doesn't re-page."""
    if _read_marker(marker_file) == origin:
        return
    _write_marker(marker_file, origin)
    deliver(f"{channel}:{origin}", content)


def alert_deferred(origin: str, deployed: set[str], cs: ChangeSet) -> None:
    """Fire the tasks/, meta/deps.yml, and k8s-role defer-and-alert for changes NOT redeployed
    this tick.

    Runs on BOTH the no-services branch (deployed=set()) and after a SUCCESSFUL deploy
    (deployed=cs.services): a combined push (svcA template + svcB meta/deps.yml) deploys svcA but
    leaves svcB's deploy-graph change ff-merged and unapplied. The pending remainder is the pure
    `deferred_service_alerts`; this is its I/O shell (per-SHA dedupe marker + deliver). Each channel
    alerts at most once per origin SHA; its marker advances on DETECTION (deliver() and the pending
    queue own delivery + retry), so a transient webhook blip is redelivered, not silently dropped."""
    pending_tasks, pending_meta = deferred_service_alerts(cs, deployed)
    if pending_tasks:
        alert_once(
            TASKS_ALERT_FILE,
            "tasks",
            origin,
            f"⚠️ gitops-deploy: a structural dir (`tasks/`/`defaults/`/`vars/`/`handlers/`) changed "
            f"for `{', '.join(sorted(pending_tasks))}` in `{origin[:8]}` with no redeploy of those "
            f"service(s) — fast-forwarded but **not applied** (those dirs aren't auto-deployed). "
            f"Redeploy by hand: `ansible-playbook ansible/deploy.yml --tags <svc>`.",
        )
    if pending_meta:
        alert_once(
            META_ALERT_FILE,
            "meta",
            origin,
            f"⚠️ gitops-deploy: `meta/deps.yml` changed for "
            f"`{', '.join(sorted(pending_meta))}` in `{origin[:8]}` with no redeploy of those "
            f"service(s) — fast-forwarded but **not applied** (meta/ isn't auto-deployed; it "
            f"changes deploy ordering + dep closure). Redeploy the affected service(s) by hand: "
            f"`ansible-playbook ansible/deploy.yml --tags <svc>`.",
        )
    if cs.k8s:
        # No `- deployed` subtraction (unlike tasks/meta): this deployer never auto-deploys a
        # k8s-platform role at all, so there's no scoped redeploy for a k8s change to have ridden.
        alert_once(
            K8S_ALERT_FILE,
            "k8s",
            origin,
            f"⚠️ gitops-deploy: k8s role(s) `{', '.join(sorted(cs.k8s))}` changed in "
            f"`{origin[:8]}` — fast-forwarded but **not applied** (this deployer only "
            f"auto-deploys Docker-platform services; k8s roles are defer-and-alert). Redeploy by "
            f"hand: `ansible-playbook ansible/deploy.yml --tags <svc>`.",
        )


def _inspect(fmt: str, container: str, timeout: float = 15.0) -> str:
    """One `docker inspect -f` field, or '' if empty/gone — or if the call exceeds
    `timeout`. The deadline in health_ok() is only checked between calls, so a wedged
    daemon on an unbounded inspect would block the whole deployer forever; bounding each
    inspect lets a hang degrade into a failed gate instead."""
    try:
        return run(
            ["docker", "inspect", "-f", fmt, container],
            cwd=None,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return ""


def health_ok(
    container: str, settle_checks: int = 3, deadline: float | None = None
) -> bool:
    """True if `container` reaches 'healthy', or — for an image with no
    HEALTHCHECK — stays 'running' across `settle_checks` consecutive polls
    (~20s) so a boot-then-crash loop doesn't slip the gate the way a single
    'running' sample would. Polls until HEALTH_TIMEOUT_S — or the earlier
    `deadline` (the run-wide gate budget), so one slow container can't blow the
    whole gate past the unit timeout — then fails.

    The per-sample pass/wait + streak transition is the pure, unit-tested
    `deploy_logic.health_decision`; this function is just its I/O shell (docker
    inspect, the 10s poll, and the wall-clock deadline). `.State.Running` is only
    inspected in the no-healthcheck case (st == ''), matching the decision's use."""
    per_deadline = time.time() + TIMEOUT
    if deadline is not None:
        per_deadline = min(per_deadline, deadline)
    running_streak = 0
    while time.time() < per_deadline:
        st = _inspect("{{.State.Health.Status}}", container)
        running = st == "" and _inspect("{{.State.Running}}", container) == "true"
        verdict, running_streak = health_decision(
            st, running, running_streak, settle_checks
        )
        if verdict == "healthy":
            return True
        time.sleep(10)
    return False


def containers_for(service: str) -> list[str]:
    """Container names to health-gate for a deployed service, from its rendered
    compose. Empty when the service isn't deployed on THIS host — its rendered
    file doesn't exist (dozzle is daniel-pi-only, and the deployer doesn't run
    on the Pi at all) — so the caller skips it instead of gating a phantom
    container (see deploy_logic.containers_to_gate). A present compose that declares no
    container_name falls back to [service]."""
    path = os.path.join(REPO, "containers", service, "docker-compose.yml")
    try:
        with open(path) as fh:
            text: str | None = fh.read()
    except FileNotFoundError:
        text = None
    return containers_to_gate(text, service)


def check_stale_composes() -> None:
    """Page (once per distinct set) when containers/<svc>/docker-compose.yml exists on disk
    but <svc> has no containers_list entry — the stale-compose trap (see
    deploy_logic.stale_rendered_services for the incident history). Detection only, never
    cleanup: the remedy removes containers and directories, which stays an operator action."""
    containers_dir = os.path.join(REPO, "containers")
    hostvars = os.path.join(
        REPO, "ansible", "inventory", "host_vars", f"{HOSTNAME}.yml"
    )
    try:
        with open(hostvars) as fh:
            declared = declared_services(fh.read())
        rendered = [
            d
            for d in os.listdir(containers_dir)
            if os.path.isfile(os.path.join(containers_dir, d, "docker-compose.yml"))
        ]
    except OSError:
        return  # unreadable inventory/tree — not this watchdog's failure to page about
    stale = stale_rendered_services(rendered, declared)
    marker = ",".join(stale)
    if _read_marker(STALE_COMPOSE_FILE) == (marker or None):
        return
    _write_marker(STALE_COMPOSE_FILE, marker or None)
    if stale:
        deliver(
            f"stale-composes:{marker}",
            f"⚠️ gitops-deploy: stale rendered compose(s) on {HOSTNAME} with no "
            f"containers_list entry: `{', '.join(stale)}` — a retired/migrated service "
            f"left its render behind, and its phantom containers will fail the health "
            f"gate on that service's next deploy (false rollback + hold). Clean up: "
            f"`docker rm -f <its containers>` then `rm -rf containers/<svc>`.",
        )


def service_healthy(service: str, deadline: float | None = None) -> bool:
    # A role may run several containers; gate every one (the bumped image's
    # container is often not the role-named one). `deadline` (the run-wide gate
    # budget) is threaded to each container's poll loop.
    return all(health_ok(c, deadline=deadline) for c in containers_for(service))


def k8s_declarations_at(ref: str) -> dict[str, str | None]:
    """Every k8s role's defaults/main.yml as it exists at `ref`.

    Reads the ref directly rather than the working tree: the promotion decision runs BEFORE the
    ff-merge, so the working tree still holds the pre-merge declarations — exactly as stale as
    the config we are checking it against.

    A role directory present at the ref with no defaults/main.yml maps to None, which
    declared_denylist() reads as denied. The path parsing itself is k8s_role_paths(), a pure
    function unit-tested without git; this function does only the git I/O around it.
    """
    listing = run(["git", "ls-tree", "-r", "--name-only", ref, "ansible/roles/k8s/"])
    paths = k8s_role_paths(listing)
    return {
        role: run(["git", "show", f"{ref}:{path}"]) if path is not None else None
        for role, path in paths.items()
    }


def k8s_image_diff(local: str, origin: str, svc: str) -> str:
    """Unified diff of one k8s role's defaults/main.yml across the incoming range.

    -U0 drops context lines, so is_image_only_diff classifies changed lines only — an
    unrelated neighbouring var sitting next to the pin cannot make a clean bump look dirty.
    """
    return run(
        [
            "git",
            "diff",
            "-U0",
            f"{local}..{origin}",
            "--",
            f"ansible/roles/k8s/{svc}/defaults/main.yml",
        ]
    )


def deploy_k8s(
    services: set[str], timeout: float, restore_sha: str | None = None
) -> None:
    """Deploy k8s services by tag. The rollout gate lives INSIDE the role.

    No health-poll phase here on purpose: the play already runs apply (roles/k8s/manifests)
    -> `rollout status --timeout` (roles/k8s/rollout-drain) -> a post-Available soak
    (post_tasks/k8s_stabilise_gate.yml) that hard-fails on a restart-count delta or a
    readiness shortfall. Polling again would duplicate it, and containers_for() — the Docker
    gate's input — returns [] for a k8s service, which is exactly the 2026-08-08 configarr
    false-rollback.

    The wait and the soak moved out of roles/k8s/manifests in 5eea64e6, when rollouts were
    batched and the stabilisation window deferred to end-of-play; the sequence above is
    unchanged, but assert_stable.yml is no longer on this path (claude-otel still imports it
    as its own variant, since it rolls itself).

    restore_sha, when given, is passed to the play as the `k8s_restore_snapshot_sha` extra-var,
    which roles/k8s/manifests reads to revert each service's claimed volumes to the snapshot
    named for that SHA before re-applying. Omitted (the ordinary deploy) or blank, the extra-var
    is never added — the call is byte-identical to before this argument existed.
    """
    tags = ",".join(sorted(services))
    log(f"deploying k8s services: {tags} (timeout {timeout:.0f}s)")
    argv = [
        "uv",
        "run",
        "--frozen",
        "ansible-playbook",
        "ansible/deploy.yml",
        "--tags",
        tags,
    ]
    if restore_sha is not None and restore_sha.strip():
        argv += ["-e", f"k8s_restore_snapshot_sha={restore_sha}"]
    run(argv, timeout=timeout)


def read_local_k8s_default(role: str) -> str | None:
    """A k8s role's defaults/main.yml, read from the CURRENT working tree rather than via `git
    show`. Only called from the rollback path, after `git reset --hard local` — so a plain read
    matches exactly what roles/k8s/manifests itself reads for the claim list (see the comment
    above the revert task in that role's tasks/main.yml)."""
    path = (
        pathlib.Path(REPO)
        / "ansible"
        / "roles"
        / "k8s"
        / role
        / "defaults"
        / "main.yml"
    )
    try:
        return path.read_text()
    except FileNotFoundError:
        return None


def deploy(services: set[str]) -> None:
    tags = ",".join(sorted(services))
    # Run via `uv run` so the deploy uses the repo's pinned env (ansible-core plus
    # the community.docker deps requests/docker) — the same toolchain the operator
    # uses. --frozen: install from the committed uv.lock, never mutate it on the host.
    run(
        [
            "uv",
            "run",
            "--frozen",
            "ansible-playbook",
            "ansible/deploy.yml",
            "--tags",
            tags,
        ]
    )


def main() -> int:
    # Resend any alert a prior tick failed to deliver, BEFORE any short-circuit below: the ff-merged
    # secrets/tasks/meta/combined paths never re-reach their alert code (local==origin -> noop), so a
    # transient webhook failure is only recoverable here, not by discord()'s per-tick re-eval.
    drain_pending()

    # Disk-only, independent of git state, so it runs before any branch can short-circuit
    # the tick: page (once per distinct set) when a rendered compose has no containers_list
    # entry — the stale-compose trap, twice now the cause of a phantom health gate + false
    # rollback + hold.
    check_stale_composes()

    # A dirty working tree (operator may be mid-edit) is a healthy skip, not an
    # outage: we never deploy from it, but the tick completes and writes last_run so
    # a long edit session doesn't falsely trip the GitOps-Alive monitor.
    # (git fetch is safe on a dirty tree — it only updates remote-tracking refs.)
    dirty = bool(run(["git", "status", "--porcelain"]))

    # NOT `run(...)` (which raises RuntimeError → the generic crash-page): a transient fetch failure
    # is retryable, so raise RetryableFetchError and let __main__ skip the tick cleanly. subprocess
    # directly (like is_ancestor) to read the returncode/stderr `run(check=False)` would discard.
    fetch = subprocess.run(
        ["git", "fetch", "origin", BRANCH], cwd=REPO, text=True, capture_output=True
    )
    if fetch.returncode != 0:
        raise RetryableFetchError(
            fetch.stderr.strip() or f"git fetch exited {fetch.returncode}"
        )
    local = run(["git", "rev-parse", "HEAD"])
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
    origin = run(["git", "rev-parse", f"origin/{BRANCH}"])
    hold = read_hold()

    # origin is "ahead" only if local is an ancestor of it — i.e. it carries
    # commits we don't have. If origin is behind (the operator committed locally
    # but hasn't pushed) or the two diverged, there is nothing to fast-forward and
    # next_action() makes this a no-op instead of mis-firing on the reverse diff.
    origin_ahead = is_ancestor(local, origin)
    # Divergence watchdog: if local and origin differ but neither is an ancestor of the other, the
    # deployer can't fast-forward and every tick noops while origin's new commits never deploy —
    # invisible otherwise (last_run keeps ticking, no hold). Record it so GitOps Status pages; clear
    # it once resolved. A committed-but-unpushed local commit (local_ahead — secret-rotate's domain)
    # is a plain noop, NOT flagged here. Managed every tick regardless of `action`.
    local_ahead = is_ancestor(origin, local)
    _write_marker(
        DIVERGED_FILE,
        origin if is_diverged(origin, local, origin_ahead, local_ahead) else None,
    )
    # Only spend the GitHub call on a tick that would otherwise deploy. These conditions mirror
    # next_action's own short-circuits above it, so a noop/dirty/held tick costs no API request —
    # which keeps the unauthenticated 60/hour rate limit irrelevant at one tick per 30 min.
    ci = "pass"
    if not dirty and origin_ahead and origin != local and origin != hold:
        ci = fetch_ci_verdict(origin)
    action = next_action(local, origin, hold, dirty, origin_ahead, ci)
    if action == "dirty":
        # Healthy skip (operator mid-edit). Throttle the page to twice a day at
        # ~08:00 and ~20:00 CT instead of every 30-min tick (see DIRTY_ALERT_FILE).
        now_ct = datetime.now(CHICAGO)
        if should_alert_dirty(
            now_ct,
            _read_marker(DIRTY_ALERT_FILE),
            DIRTY_ALERT_MORNING_HOUR,
            DIRTY_ALERT_EVENING_HOUR,
        ):
            # Mark as alerted only on confirmed delivery, else retry next tick (see discord()).
            if discord(
                f"⚠️ gitops-deploy: working tree dirty on {HOSTNAME} — skipping. "
                "Resolve manually."
            ):
                _write_marker(
                    DIRTY_ALERT_FILE,
                    dirty_alert_slot(
                        now_ct, DIRTY_ALERT_MORNING_HOUR, DIRTY_ALERT_EVENING_HOUR
                    ),
                )
        return 0
    if action == "noop":
        return 0
    if action == "skip_hold":
        log(f"origin at known-bad {origin[:8]}; holding")
        return 0
    if action == "ci_pending":
        # Normal for the first tick after a push: the workflow is still running. No alert — it
        # resolves on its own, and a host left behind for hours is the behind-origin watchdog's
        # job, not this branch's.
        log(f"origin {origin[:8]}: CI not finished — deferring, will retry next tick")
        return 0
    if action == "ci_failed":
        alert_once(
            CI_ALERT_FILE,
            "ci",
            origin,
            f"⛔ gitops-deploy: CI is RED on `{origin[:8]}` — NOT deploying on {HOSTNAME}. "
            f"The host stays on `{local[:8]}` until master is green; fix forward or revert. "
            "(GitOps Status pages separately once the host has been behind for 6h.)",
        )
        log(f"origin {origin[:8]}: CI failed — not deploying")
        return 0

    paths = run(["git", "diff", "--name-only", f"{local}..{origin}"]).splitlines()
    cs = services_from_changed_paths(paths)
    # A path under ansible/roles/containers/<svc>/ maps to <svc> by NAME ALONE — it doesn't know
    # this host might run that same-named service under k8s (wg-easy: a Docker role, but
    # platform: k8s on daniel-box). Route those into the k8s defer-and-alert set instead of
    # deploying a tag that resolves to deploy.yml's K8S play (an idempotent no-op whose health
    # gate silently no-ops too, since containers_for() renders nothing for a k8s entry).
    hostvars_path = os.path.join(
        REPO, "ansible", "inventory", "host_vars", f"{HOSTNAME}.yml"
    )
    try:
        with open(hostvars_path) as fh:
            k8s_services = declared_k8s_services(fh.read())
    except OSError:
        k8s_services = set()
    cs = reroute_k8s_services(cs, k8s_services)
    # The denylist in config.env is baked at Ansible template time. A later declaration flip
    # lands under roles/k8s/, which routes to ChangeSet.k8s and alerts naming deploy.yml — a
    # playbook that runs no setup role and never re-renders this config. Without this check the
    # host would keep acting on the old list, leaving a role that was just denied still
    # auto-deployable. Disarm loudly rather than acting on a stale boundary.
    autodeploy_enabled = K8S_AUTODEPLOY_ENABLED
    if autodeploy_enabled:
        try:
            # `origin` (the SHA pinned above, not f"origin/{BRANCH}") — the diff and the alert
            # already evaluate against that exact commit; re-resolving the ref here would open a
            # TOCTOU where a concurrent fetch lands between the two reads.
            declared = declared_denylist(k8s_declarations_at(origin))
            read_error = None
        except Exception as exc:  # noqa: BLE001 - any failure here must disarm, not crash
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
                STALE_DENYLIST_FILE,
                "stale_denylist",
                origin,
                f"⚠️ gitops-deploy: `/etc/gitops-deploy/config.env` denylist is stale against "
                f"`{origin[:8]}` — {detail}. k8s auto-deploy is DISARMED until this is fixed: "
                f"{fix}.",
            )
    # Promote image-bump-only k8s changes to the auto-deploy channel. Everything not promoted
    # stays in cs.k8s and defer-and-alerts exactly as before, so this is inert until a service
    # passes BOTH the diff-shape test and the denylist.
    cs = split_k8s_auto_deploy(
        cs,
        paths,
        denylist=K8S_AUTODEPLOY_DENYLIST,
        pilot=K8S_AUTODEPLOY_PILOT,
        enabled=autodeploy_enabled,
        image_only=lambda svc: is_image_only_diff(k8s_image_diff(local, origin, svc)),
        max_per_tick=K8S_AUTODEPLOY_MAX_PER_TICK,
    )

    if cs.broad:
        # Broad doesn't ff-merge, so it re-evals next tick — the per-SHA marker (inside alert_once)
        # stops a re-queue while the pending queue owns redelivery. Name the RIGHT playbook per plane:
        # deploy.yml applies only container roles, so a setup-plane change (roles/setup/,
        # requirements.yml, bring-up playbooks) needs initial_setup.yml (2026-07-16 review M1).
        alert_once(
            BROAD_FILE,
            "broad",
            origin,
            f"⚠️ gitops-deploy: broad change (shared template / inventory / setup role) in "
            f"`{origin[:8]}` — deferring to a manual deploy. Run "
            f"{broad_remediation(cs.broad_deploy, cs.broad_setup)} on the host, then "
            f"`git merge --ff-only origin/{BRANCH}` to clear it.",
        )
        return 0
    if cs.k8s_deploy:
        run(["git", "merge", "--ff-only", origin])
        try:
            deploy_k8s(cs.k8s_deploy, K8S_DEPLOY_TIMEOUT_S)
        except Exception as exc:  # noqa: BLE001 — any playbook failure, or the timeout above
            # Hold BEFORE the reset, same as the Docker paths: a hung rollback redeploy would
            # otherwise be SIGTERMed before the marker is written, stranding the bad commit into
            # a per-tick redeploy loop.
            log(f"k8s deploy failed for {sorted(cs.k8s_deploy)}: {exc}; rolling back")
            write_hold(origin)
            run(["git", "reset", "--hard", local])
            rollback_failed: Exception | None = None
            try:
                # `origin`, not `local`: the tree is already reset to the last-good commit, so
                # the snapshot worth reverting to is the one taken before the FAILED deploy —
                # named for `origin`, the commit being rolled back FROM. Passing `local` looks
                # right and is wrong twice over: on a first rollback it finds no snapshot and
                # fails the deploy, and on a second rollback of the same service it finds a
                # STALE snapshot and reverts to the wrong point.
                deploy_k8s(
                    cs.k8s_deploy, K8S_ROLLBACK_TIMEOUT_S, restore_sha=origin[:8]
                )
            except Exception as exc2:  # noqa: BLE001 — best-effort restore; we still hold + alert
                rollback_failed = exc2
                log(f"k8s rollback redeploy of the prior version also failed: {exc2}")
            # Read from the tree AFTER the reset above, matching what roles/k8s/manifests itself
            # reads for the claim list — the failed commit may have added or renamed a claim, and
            # that version is exactly what must NOT decide this note.
            reverting = frozenset(
                svc
                for svc in cs.k8s_deploy
                if declares_snapshot_claims(read_local_k8s_default(svc))
            )
            revert_note = rollback_volume_revert_note(
                cs.k8s_deploy,
                reverting,
                str(rollback_failed) if rollback_failed else None,
            )
            posted = discord(
                f"🚨 gitops-deploy: **k8s deploy failed** on {HOSTNAME}.\n"
                f"`{', '.join(sorted(cs.k8s_deploy))}` from `{origin[:8]}` failed its rollout "
                f"gate:\n`{exc}`\n"
                f"Rolled back locally to `{local[:8]}`.\n"
                f"{revert_note}"
                f"**The bad pin is still live on master.** The hold only skips THIS commit — "
                f"`skip_hold` matches while `origin_head == hold_sha`, so the next push past it "
                f"redeploys the same pin.\n"
                f"**Action:** revert the offending commit on the remote, or pin the bad version "
                f"out via Renovate `allowedVersions`."
            )
            return 0 if posted else 1
        # The ONLY place a hold can clear on an all-k8s host. write_hold(None) otherwise lives
        # solely in the Docker health-gate branch below, which such a host never reaches — so
        # without this the first rollback would leave GitOps Deploy — Status red forever and
        # need a manual rm (the trap this role's CLAUDE.md documents).
        write_hold(None)
        alert_deferred(origin, cs.k8s_deploy, cs)
        return 0
    if not cs.services:
        run(["git", "merge", "--ff-only", origin])  # docs-only etc.
        # A secrets-only push (rotated value, no service template changed) maps to nothing,
        # so the ff-merge above is all we can do automatically — but the new value only
        # reaches a container on its next deploy. Defer-and-alert (once per SHA) so the
        # operator redeploys the consumer(s); without this the rotated secret sits stale.
        if cs.secrets:
            alert_once(
                SECRETS_ALERT_FILE,
                "secrets",
                origin,
                f"⚠️ gitops-deploy: `secrets.yml` changed in `{origin[:8]}` with no "
                f"service template — fast-forwarded but **nothing was redeployed**. The "
                f"rotated secret won't reach its container(s) until you redeploy them "
                f"(`ansible-playbook ansible/deploy.yml --tags <svc>`).",
            )
        # tasks/ and meta/deps.yml changes aren't auto-deployed but DO change what a deploy does,
        # so they must not sit silently ff-merged. Nothing was deployed this tick (deployed=set()),
        # so the full sets are flagged. Same helper runs on the deploy path for a combined push.
        alert_deferred(origin, set(), cs)
        return 0

    run(["git", "merge", "--ff-only", origin])
    try:
        deploy(cs.services)
    except Exception as exc:  # noqa: BLE001 — any ansible-playbook failure
        # Deploy-EXECUTION failure (ansible-playbook itself errored: bad image manifest, a failed
        # task) — distinct from the health gate below. Without this the exception propagates to
        # __main__, which alerts but re-raises WITHOUT writing last_run AND leaves the repo
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
        run(["git", "reset", "--hard", local])
        try:
            deploy(cs.services)
        except Exception as exc2:  # noqa: BLE001 — best-effort restore; we still hold + alert
            log(f"rollback redeploy of the prior version also failed: {exc2}")
        posted = discord(
            f"🚨 gitops-deploy: **deploy failed** on {HOSTNAME}.\n"
            f"`ansible-playbook` errored deploying `{', '.join(sorted(cs.services))}` from "
            f"`{origin[:8]}`:\n`{exc}`\n"
            f"Rolled back to `{local[:8]}`; the bad commit is held until origin advances past it.\n"
            f"**Action:** fix or revert the offending commit."
        )
        # A rollback already surfaces via THIS detailed post + the GitOps Deploy — Status monitor
        # (hold_sha). Exit 0 when the detailed post was delivered so systemd's
        # OnFailure=gitops-deploy-alert.service (a GENERIC "unit failed" curl) doesn't ALSO fire — one
        # detailed page, not a duplicate. Only if the detailed post failed (Cloudflare-1010/webhook
        # down) exit 1, so OnFailure is the guaranteed backstop. last_run is written either way (the
        # tick completed; the deployer is alive — GitOps-Alive stays green, Status carries the hold).
        return 0 if posted else 1

    # Health-gate only services actually deployed on THIS host. A changed template
    # for an other-host-only service (dozzle is daniel-pi-only) renders no compose
    # here, so containers_for() returns [] and service_healthy() is vacuously true —
    # without this the gate would poll a phantom container to timeout and trigger a
    # false rollback. (deploy(cs.services) above is a harmless no-op for those tags.)
    skipped = sorted(s for s in cs.services if not containers_for(s))
    if skipped:
        log(f"not deployed on this host; skipping health gate: {skipped}")
    # Budget the gate so gate+rollback finishes inside the unit's TimeoutStartSec (see
    # RUN_BUDGET_S): once the deadline passes, gate_services marks the rest failed and we roll
    # back, rather than polling to HEALTH_TIMEOUT_S per container and getting SIGTERMed mid-gate
    # (which would strand the bad commit live). RUN_START is measured from process start.
    gate_deadline = RUN_START + RUN_BUDGET_S
    failed = gate_services(cs.services, service_healthy, gate_deadline, time.time)
    if not failed:
        write_hold(None)
        # Combined-push safety: a tasks/ or meta/deps.yml change bundled for a service OTHER than
        # the one(s) just deployed is ff-merged but unapplied — flag that remainder (a bundled
        # change to a DEPLOYED service rode its own --tags redeploy, so it's excluded). Only on a
        # clean deploy: a rollback below git-resets the whole commit, reverting those changes too.
        alert_deferred(origin, cs.services, cs)
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
    run(["git", "reset", "--hard", local])
    try:
        deploy(cs.services)
    except Exception as exc:  # noqa: BLE001 — best-effort restore; we still hold + alert
        log(f"rollback redeploy of the prior version also failed: {exc}")
    posted = discord(
        f"🚨 gitops-deploy: **rollback** on {HOSTNAME}.\n"
        f"Service(s) `{', '.join(failed)}` from commit `{origin[:8]}` failed the health "
        f"gate and were rolled back to `{local[:8]}`.\n"
        f"**Action:** revert the offending Renovate PR — the bad commit is held until you do."
    )
    # Exit 0 on a delivered detailed post so OnFailure's generic curl doesn't double-page (see the
    # exec-failure path above); exit 1 only if the detailed post failed, leaving OnFailure the backstop.
    return 0 if posted else 1


if __name__ == "__main__":
    try:
        rc = main()
    except RetryableFetchError as e:
        # Transient `git fetch` failure: skip this tick without paging (no crash Discord, and exit 0
        # so the OnFailure alert unit doesn't fire either) and WITHOUT writing last_run — a one-off
        # blip is invisibly retried next tick, while a persistent fetch break ages last_run and trips
        # GitOps-Alive. Must precede the generic handler below (Python matches except-clauses in order).
        log(f"git fetch failed (retryable) — skipping tick, will retry next run: {e}")
        sys.exit(0)
    except Exception as e:
        discord(f"🚨 gitops-deploy crashed: {e}")
        raise
    # Liveness marker: a tick that completed without crashing (incl. a rollback, rc=1).
    # monitor-bridge reads this; a crash skips the write so the Alive monitor goes stale.
    _record_behind()
    _write_marker(LAST_RUN, str(time.time()))
    sys.exit(rc)
