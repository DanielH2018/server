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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from deploy_logic import (  # noqa: E402
    container_names,
    next_action,
    services_from_changed_paths,
)

HOLD_FILE = "/var/lib/gitops-deploy/hold_sha"
LAST_RUN = "/var/lib/gitops-deploy/last_run"
# Last origin SHA we've already alerted on for a broad change, so a deferred
# broad change doesn't re-page Discord every 30-min tick until it's resolved.
BROAD_FILE = "/var/lib/gitops-deploy/broad_alerted_sha"


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


def run(args: list[str], cwd: str | None = REPO, check: bool = True) -> str:
    r = subprocess.run(args, cwd=cwd, text=True, capture_output=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"{' '.join(args)} -> {r.returncode}\n{r.stderr}")
    return r.stdout.strip()


def log(msg: str) -> None:
    print(msg, flush=True)


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


def discord(content: str) -> None:
    url = C.get("DISCORD_WEBHOOK", "")
    if not url:
        return
    data = json.dumps({"content": content[:1900]}).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:  # alerting must never crash the deployer
        log(f"discord alert failed: {e}")


def health_ok(container: str, settle_checks: int = 3) -> bool:
    """True if `container` reaches 'healthy', or — for an image with no
    HEALTHCHECK — stays 'running' across `settle_checks` consecutive polls
    (~20s) so a boot-then-crash loop doesn't slip the gate the way a single
    'running' sample would. Polls until HEALTH_TIMEOUT_S, then fails."""
    deadline = time.time() + TIMEOUT
    running_streak = 0
    while time.time() < deadline:
        st = run(["docker", "inspect", "-f", "{{.State.Health.Status}}", container],
                 cwd=None, check=False)
        if st == "healthy":
            return True
        if st == "":  # no healthcheck (or container gone) -> require sustained running
            run_st = run(["docker", "inspect", "-f", "{{.State.Running}}", container],
                         cwd=None, check=False)
            running_streak = running_streak + 1 if run_st == "true" else 0
            if running_streak >= settle_checks:
                return True
        else:  # 'starting' / 'unhealthy' -> reset the streak, keep waiting
            running_streak = 0
        time.sleep(10)
    return False


def containers_for(service: str) -> list[str]:
    """Container names declared by a deployed service's rendered compose file.
    Falls back to the service name if the file is missing or names none."""
    path = os.path.join(REPO, "containers", service, "docker-compose.yml")
    try:
        with open(path) as fh:
            names = container_names(fh.read())
    except FileNotFoundError:
        names = []
    return names or [service]


def service_healthy(service: str) -> bool:
    # A role may run several containers; gate every one (the bumped image's
    # container is often not the role-named one).
    return all(health_ok(c) for c in containers_for(service))


def deploy(services: set[str]) -> None:
    tags = ",".join(sorted(services))
    # Run via `uv run` so the deploy uses the repo's pinned env (ansible-core plus
    # the community.docker deps requests/docker) — the same toolchain the operator
    # uses. --frozen: install from the committed uv.lock, never mutate it on the host.
    run(["uv", "run", "--frozen", "ansible-playbook", "ansible/deploy.yml", "--tags", tags])


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

    action = next_action(local, origin, hold, dirty)
    if action == "dirty":
        discord("⚠️ gitops-deploy: working tree dirty on daniel-server — skipping. "
                "Resolve manually.")
        return 0
    if action == "noop":
        return 0
    if action == "skip_hold":
        log(f"origin at known-bad {origin[:8]}; holding")
        return 0

    paths = run(["git", "diff", "--name-only", f"{local}..{origin}"]).splitlines()
    cs = services_from_changed_paths(paths)

    if cs.broad:
        if _read_marker(BROAD_FILE) != origin:  # alert once per broad SHA, not every tick
            discord(f"⚠️ gitops-deploy: shared template / inventory changed in "
                    f"`{origin[:8]}` — deferring to a manual full deploy "
                    f"(`ansible-playbook ansible/deploy.yml`), then `git merge --ff-only "
                    f"origin/{BRANCH}` on the host to clear it.")
            _write_marker(BROAD_FILE, origin)
        return 0
    if not cs.services:
        run(["git", "merge", "--ff-only", f"origin/{BRANCH}"])  # docs-only etc.
        return 0

    run(["git", "merge", "--ff-only", f"origin/{BRANCH}"])
    deploy(cs.services)

    failed = [s for s in sorted(cs.services) if not service_healthy(s)]
    if not failed:
        write_hold(None)
        return 0

    # Rollback: reset to prior HEAD, redeploy the failed service(s) on old version.
    log(f"health gate failed for {failed}; rolling back to {local[:8]}")
    run(["git", "reset", "--hard", local])
    deploy(set(failed))
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
