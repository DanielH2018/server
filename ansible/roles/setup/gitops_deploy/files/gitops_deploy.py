#!/usr/bin/env python3
"""GitOps deployer — runs once per systemd-timer tick on daniel-server.

Flow: fetch origin/master; if it advanced, map changed templates to services;
ff-merge; deploy each via the existing ansible-playbook path; health-gate each
container. On failure: reset to the previous HEAD, redeploy the prior version,
record the bad SHA as a hold marker, and alert the dedicated Discord webhook.

Config comes from /etc/gitops-deploy/config.env (KEY=VALUE), written by Ansible:
  REPO_DIR, BRANCH, DISCORD_WEBHOOK, HEALTH_TIMEOUT_S
Stdlib only.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from deploy_logic import (  # noqa: E402
    ChangeSet,
    apply_drain_result,
    apply_send_result,
    broad_remediation,
    containers_to_gate,
    deferred_service_alerts,
    dirty_alert_slot,
    gate_services,
    health_decision,
    is_diverged,
    next_action,
    services_from_changed_paths,
    should_alert_dirty,
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
    # only the health-gate's docker inspects pass a short bound (see health_ok).
    r = subprocess.run(args, cwd=cwd, text=True, capture_output=True, timeout=timeout)
    if check and r.returncode != 0:
        raise RuntimeError(f"{' '.join(args)} -> {r.returncode}\n{r.stderr}")
    return r.stdout.strip()


def log(msg: str) -> None:
    print(msg, flush=True)


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
    # Split (not `except (A, B)`): this runs on the host's Python 3.12, but ruff (3.14 target, from
    # requires-python) reformats a parenthesized tuple into the 3.14-only `except A, B:` that
    # SyntaxErrors on 3.12. Two clauses give ruff nothing to rewrite. See test_host_scripts_py312.py.
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
    """Fire the tasks/ and meta/deps.yml defer-and-alert for services NOT redeployed this tick.

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
    file doesn't exist (dozzle is daniel-pi-only; the deployer runs on
    daniel-server) — so the caller skips it instead of gating a phantom container
    (see deploy_logic.containers_to_gate). A present compose that declares no
    container_name falls back to [service]."""
    path = os.path.join(REPO, "containers", service, "docker-compose.yml")
    try:
        with open(path) as fh:
            text: str | None = fh.read()
    except FileNotFoundError:
        text = None
    return containers_to_gate(text, service)


def service_healthy(service: str, deadline: float | None = None) -> bool:
    # A role may run several containers; gate every one (the bumped image's
    # container is often not the role-named one). `deadline` (the run-wide gate
    # budget) is threaded to each container's poll loop.
    return all(health_ok(c, deadline=deadline) for c in containers_for(service))


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
    action = next_action(local, origin, hold, dirty, origin_ahead)
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
                "⚠️ gitops-deploy: working tree dirty on daniel-server — skipping. "
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

    paths = run(["git", "diff", "--name-only", f"{local}..{origin}"]).splitlines()
    cs = services_from_changed_paths(paths)

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
    if not cs.services:
        run(["git", "merge", "--ff-only", f"origin/{BRANCH}"])  # docs-only etc.
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

    run(["git", "merge", "--ff-only", f"origin/{BRANCH}"])
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
            f"🚨 gitops-deploy: **deploy failed** on daniel-server.\n"
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
        f"🚨 gitops-deploy: **rollback** on daniel-server.\n"
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
    _write_marker(LAST_RUN, str(time.time()))
    sys.exit(rc)
