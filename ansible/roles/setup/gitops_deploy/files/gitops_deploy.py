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
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from deploy_logic import (  # noqa: E402
    containers_to_gate,
    health_decision,
    next_action,
    services_from_changed_paths,
    should_alert_dirty,
)

HOLD_FILE = "/var/lib/gitops-deploy/hold_sha"
LAST_RUN = "/var/lib/gitops-deploy/last_run"
# Last origin SHA we've already alerted on for a broad change, so a deferred
# broad change doesn't re-page Discord every 30-min tick until it's resolved.
BROAD_FILE = "/var/lib/gitops-deploy/broad_alerted_sha"
# Same throttle for a secrets-only push (rotated value with no service template change):
# alert once per SHA so the operator redeploys the consumer(s), don't re-page every tick.
SECRETS_ALERT_FILE = "/var/lib/gitops-deploy/secrets_alerted_sha"
# Same throttle for a tasks-only push (a role tasks/ change, which isn't auto-deployed): alert once
# per SHA so the operator redeploys the role by hand, don't re-page every tick.
TASKS_ALERT_FILE = "/var/lib/gitops-deploy/tasks_alerted_sha"
# Last CT date (YYYY-MM-DD) we paged for a dirty working tree. The tick runs every
# 30 min, so without this an open edit session would re-alert all day; we throttle
# to one alert per day, fired on the first tick at/after DIRTY_ALERT_HOUR (07:00 CT).
DIRTY_ALERT_FILE = "/var/lib/gitops-deploy/dirty_alerted_date"
DIRTY_ALERT_HOUR = 7
# Host clock is UTC; the operator wants the daily reminder at 07:00 local time.
CHICAGO = ZoneInfo("America/Chicago")


def cfg() -> dict[str, str]:
    out: dict[str, str] = {}
    with open("/etc/gitops-deploy/config.env") as fh:
        for line in fh:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k] = v
    return out


C = cfg()
REPO = C["REPO_DIR"]
BRANCH = C.get("BRANCH", "master")
TIMEOUT = int(C.get("HEALTH_TIMEOUT_S", "300"))


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
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if sha is None:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
    else:
        with open(path, "w") as fh:
            fh.write(sha)


def read_hold() -> str | None:
    return _read_marker(HOLD_FILE)


def write_hold(sha: str | None) -> None:
    _write_marker(HOLD_FILE, sha)


def discord(content: str) -> bool:
    """Post to the alert webhook. Returns True only on a confirmed delivery (a 2xx), so a
    caller can gate its per-SHA dedupe marker on it -- otherwise a transient webhook failure
    (timeout, 5xx, momentary Cloudflare block) would advance the marker and permanently
    suppress that alert (the next tick would see marker==SHA and stay silent). A missing
    webhook or any error returns False, so the alert is retried on the next tick."""
    url = C.get("DISCORD_WEBHOOK", "")
    if not url:
        return False
    data = json.dumps({"content": content[:1900]}).encode()
    # User-Agent required: Discord is behind Cloudflare, which 403s the default Python-urllib
    # UA (error code 1010) — without this the alert silently fails (the except below swallows it).
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json", "User-Agent": "gitops-deploy"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return 200 <= resp.status < 300
    except Exception as e:  # alerting must never crash the deployer
        log(f"discord alert failed: {e}")
        return False


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


def health_ok(container: str, settle_checks: int = 3) -> bool:
    """True if `container` reaches 'healthy', or — for an image with no
    HEALTHCHECK — stays 'running' across `settle_checks` consecutive polls
    (~20s) so a boot-then-crash loop doesn't slip the gate the way a single
    'running' sample would. Polls until HEALTH_TIMEOUT_S, then fails.

    The per-sample pass/wait + streak transition is the pure, unit-tested
    `deploy_logic.health_decision`; this function is just its I/O shell (docker
    inspect, the 10s poll, and the wall-clock deadline). `.State.Running` is only
    inspected in the no-healthcheck case (st == ''), matching the decision's use."""
    deadline = time.time() + TIMEOUT
    running_streak = 0
    while time.time() < deadline:
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


def service_healthy(service: str) -> bool:
    # A role may run several containers; gate every one (the bumped image's
    # container is often not the role-named one).
    return all(health_ok(c) for c in containers_for(service))


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
    # A dirty working tree (operator may be mid-edit) is a healthy skip, not an
    # outage: we never deploy from it, but the tick completes and writes last_run so
    # a long edit session doesn't falsely trip the GitOps-Alive monitor.
    # (git fetch is safe on a dirty tree — it only updates remote-tracking refs.)
    dirty = bool(run(["git", "status", "--porcelain"]))

    run(["git", "fetch", "origin", BRANCH])
    local = run(["git", "rev-parse", "HEAD"])
    origin = run(["git", "rev-parse", f"origin/{BRANCH}"])
    hold = read_hold()

    # origin is "ahead" only if local is an ancestor of it — i.e. it carries
    # commits we don't have. If origin is behind (the operator committed locally
    # but hasn't pushed) or the two diverged, there is nothing to fast-forward and
    # next_action() makes this a no-op instead of mis-firing on the reverse diff.
    origin_ahead = is_ancestor(local, origin)
    action = next_action(local, origin, hold, dirty, origin_ahead)
    if action == "dirty":
        # Healthy skip (operator mid-edit). Throttle the page to once a day at
        # ~07:00 CT instead of every 30-min tick (see DIRTY_ALERT_FILE).
        now_ct = datetime.now(CHICAGO)
        if should_alert_dirty(now_ct, _read_marker(DIRTY_ALERT_FILE), DIRTY_ALERT_HOUR):
            # Mark as alerted only on confirmed delivery, else retry next tick (see discord()).
            if discord(
                "⚠️ gitops-deploy: working tree dirty on daniel-server — skipping. "
                "Resolve manually."
            ):
                _write_marker(DIRTY_ALERT_FILE, now_ct.date().isoformat())
        return 0
    if action == "noop":
        return 0
    if action == "skip_hold":
        log(f"origin at known-bad {origin[:8]}; holding")
        return 0

    paths = run(["git", "diff", "--name-only", f"{local}..{origin}"]).splitlines()
    cs = services_from_changed_paths(paths)

    if cs.broad:
        if (
            _read_marker(BROAD_FILE) != origin
        ):  # alert once per broad SHA, not every tick
            # Mark only on confirmed delivery, else retry next tick (see discord()).
            if discord(
                f"⚠️ gitops-deploy: shared template / inventory changed in "
                f"`{origin[:8]}` — deferring to a manual full deploy "
                f"(`ansible-playbook ansible/deploy.yml`), then `git merge --ff-only "
                f"origin/{BRANCH}` on the host to clear it."
            ):
                _write_marker(BROAD_FILE, origin)
        return 0
    if not cs.services:
        run(["git", "merge", "--ff-only", f"origin/{BRANCH}"])  # docs-only etc.
        # A secrets-only push (rotated value, no service template changed) maps to nothing,
        # so the ff-merge above is all we can do automatically — but the new value only
        # reaches a container on its next deploy. Defer-and-alert (once per SHA) so the
        # operator redeploys the consumer(s); without this the rotated secret sits stale.
        if cs.secrets and _read_marker(SECRETS_ALERT_FILE) != origin:
            # Mark only on confirmed delivery, else retry next tick (see discord()).
            if discord(
                f"⚠️ gitops-deploy: `secrets.yml` changed in `{origin[:8]}` with no "
                f"service template — fast-forwarded but **nothing was redeployed**. The "
                f"rotated secret won't reach its container(s) until you redeploy them "
                f"(`ansible-playbook ansible/deploy.yml --tags <svc>`)."
            ):
                _write_marker(SECRETS_ALERT_FILE, origin)
        # A tasks-only push (a role's tasks/main.yml changed with no template/files change) maps to
        # no scoped deploy — tasks/ isn't auto-deployed (structural, deploy by hand) — but unlike a
        # doc edit it changes what a deploy does, so it must not sit silently unapplied. Defer-and-
        # alert (once per SHA); mirrors the secrets path above.
        if cs.tasks and _read_marker(TASKS_ALERT_FILE) != origin:
            # Mark only on confirmed delivery, else retry next tick (see discord()).
            if discord(
                f"⚠️ gitops-deploy: only `tasks/` changed for "
                f"`{', '.join(sorted(cs.tasks))}` in `{origin[:8]}` — fast-forwarded but "
                f"**nothing was redeployed** (tasks/ isn't auto-deployed). Redeploy by hand: "
                f"`ansible-playbook ansible/deploy.yml --tags <svc>`."
            ):
                _write_marker(TASKS_ALERT_FILE, origin)
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
        run(["git", "reset", "--hard", local])
        try:
            deploy(cs.services)
        except Exception as exc2:  # noqa: BLE001 — best-effort restore; we still hold + alert
            log(f"rollback redeploy of the prior version also failed: {exc2}")
        write_hold(origin)
        discord(
            f"🚨 gitops-deploy: **deploy failed** on daniel-server.\n"
            f"`ansible-playbook` errored deploying `{', '.join(sorted(cs.services))}` from "
            f"`{origin[:8]}`:\n`{exc}`\n"
            f"Rolled back to `{local[:8]}`; the bad commit is held until origin advances past it.\n"
            f"**Action:** fix or revert the offending commit."
        )
        return 1

    # Health-gate only services actually deployed on THIS host. A changed template
    # for an other-host-only service (dozzle is daniel-pi-only) renders no compose
    # here, so containers_for() returns [] and service_healthy() is vacuously true —
    # without this the gate would poll a phantom container to timeout and trigger a
    # false rollback. (deploy(cs.services) above is a harmless no-op for those tags.)
    skipped = sorted(s for s in cs.services if not containers_for(s))
    if skipped:
        log(f"not deployed on this host; skipping health gate: {skipped}")
    failed = [s for s in sorted(cs.services) if not service_healthy(s)]
    if not failed:
        write_hold(None)
        return 0

    # Rollback: reset to prior HEAD, redeploy the prior version. Redeploy the WHOLE batch
    # (cs.services), not just `failed`: in a multi-service tick the services that DID pass
    # were recreated on the new images, so after the git reset they'd otherwise stay on the
    # new images while the tree points at old — partial-batch drift. Mirror the exec-failure
    # path (above): guard the redeploy and write the hold regardless, else a raise here skips
    # write_hold and the next tick re-merges the bad commit and loops every 30 min.
    log(f"health gate failed for {failed}; rolling back to {local[:8]}")
    run(["git", "reset", "--hard", local])
    try:
        deploy(cs.services)
    except Exception as exc:  # noqa: BLE001 — best-effort restore; we still hold + alert
        log(f"rollback redeploy of the prior version also failed: {exc}")
    write_hold(origin)
    discord(
        f"🚨 gitops-deploy: **rollback** on daniel-server.\n"
        f"Service(s) `{', '.join(failed)}` from commit `{origin[:8]}` failed the health "
        f"gate and were rolled back to `{local[:8]}`.\n"
        f"**Action:** revert the offending Renovate PR — the bad commit is held until you do."
    )
    return 1


if __name__ == "__main__":
    try:
        rc = main()
    except Exception as e:
        discord(f"🚨 gitops-deploy crashed: {e}")
        raise
    # Liveness marker: a tick that completed without crashing (incl. a rollback, rc=1).
    # monitor-bridge reads this; a crash skips the write so the Alive monitor goes stale.
    _write_marker(LAST_RUN, str(time.time()))
    sys.exit(rc)
