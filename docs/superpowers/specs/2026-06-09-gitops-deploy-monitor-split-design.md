# GitOps Deploy monitoring — split liveness from deploy-status

**Date:** 2026-06-09 · **Status:** Draft (awaiting review) · **Host:** daniel-server

## Problem

The single `GitOps Deploy` Uptime-Kuma **push** monitor (AutoKuma id `gitops-deploy`,
interval 2400s) keeps flapping to **down** during normal operator activity, not during real
failures. Observed: a ~26-hour dead-zone over the 06-08 maintenance window, plus a fresh
`01:10 up → 01:50 down` flap.

### Root cause (confirmed)

One tight-window push monitor is being asked to signal **two unrelated things at once**:

1. **liveness** — "the deployer is alive and ticking" (a dead-man's-switch), and
2. **deploy outcome** — "the last deploy succeeded" (`down` is pushed only on rollback).

The single heartbeat that feeds it is emitted **at the very end of the deployer's serial
tick** (`gitops_deploy.py` → `kuma_push`), so the beat is hostage to everything the tick
does. It lands late or not at all whenever the operator is working:

- a real deploy runs the health-gate first (`HEALTH_TIMEOUT_S=300s` **per container**, serial)
  before the end-of-tick push;
- the **manual** full-deploys that `broad change deferred` forces on the operator emit **no
  beat at all**;
- reboots / maintenance stop the timer entirely;
- the host→`docker run`→curl push path is itself fragile (it was failing intermittently this
  session), and `kuma_push` swallows failures (logs, never raises) so the service still
  exits `0` — the broken beat is invisible outside the root-only journal.

With a 40-min window vs a 30–32 min cadence (~8–10 min slack), any of the above trips a false
`down`. **A watchdog that cries wolf during normal work trains the operator to ignore it** —
so it won't be believed on the day a deploy actually breaks.

## Goals

- **Two separate monitors**, each with correct semantics, neither able to mask the other:
  - **Alive** — deployer host/scheduler is up and the deployer is ticking. Never tripped by a
    slow deploy, dirty tree, broad-defer, or idle. Detects a dead host/bridge within ~10 min
    (Kuma window) and a stalled deployer within ~90 min (the freshness threshold — bounded
    below by the 30-min tick cadence, so sub-tick detection isn't meaningful here).
  - **Status** — a real deploy failed (rollback) and is unresolved. Never tripped by
    inactivity; self-heals when resolved.
- **Decouple the heartbeat from the deploy work** — the pulse must depend on *nothing but the
  clock and cheap state files*.
- Reuse the **proven** in-network push path (`monitor-bridge`), eliminating the fragile
  host→`docker run` push that caused this incident.

## Non-goals / out of scope

- **Push-driven deploys** (webhook/git-hook to deploy instantly instead of ≤30-min poll) —
  a worthwhile separate enhancement, explicitly out of scope here.
- Changing the deploy/rollback/hold logic itself — unchanged.

## Design overview

Invert the dependency: **the deployer stops talking to Kuma entirely** and only writes two
state files to disk. **`monitor-bridge`** (a long-lived container already on the `monitoring`
network, already pushing 12 monitors reliably every 5 min) reads those files and pushes the
two new monitors, using the same `check.py` pattern as every other check.

```
deployer tick (every ~30m)                 monitor-bridge loop (every 5m)
  ├─ writes  last_run  (each non-crash tick)   ├─ reads /gitops-state/last_run  → push Alive  (up|down)
  └─ writes  hold_sha  (rollback set/clear)    └─ reads /gitops-state/hold_sha  → push Status (up|down)
        (no Kuma contact at all)                      (in-network push: reliable)
```

## State-file contract (`/var/lib/gitops-deploy/`, owned `ubuntu:ubuntu` 0750)

| File | Writer | Format | Meaning |
|---|---|---|---|
| `last_run` | deployer | epoch seconds (float, single line) | timestamp of the last tick that completed **without crashing** |
| `hold_sha` | deployer (existing) | 40-char sha or absent | a rolled-back commit is held pending operator revert |

## Component changes

### 1. Deployer — `ansible/roles/setup/gitops_deploy/files/gitops_deploy.py`

- **Add** `LAST_RUN = "/var/lib/gitops-deploy/last_run"` and write it on every non-crashing
  completion. In the `__main__` wrapper:
  ```python
  if __name__ == "__main__":
      try:
          rc = main()
      except Exception as e:                       # crash → no last_run write → Alive goes stale
          discord(f"🚨 gitops-deploy crashed: {e}")
          raise
      _write_marker(LAST_RUN, str(time.time()))    # completed a tick (incl. rollback rc=1)
      sys.exit(rc)
  ```
  Writing at completion (not start) means a **crash-loop** also stales `last_run` → Alive goes
  red, while a slow-but-successful deploy still refreshes it well inside the freshness window.
- **Remove** `kuma_push()` and all its call sites (every `kuma_push(...)` line in `main()`),
  plus the now-unused `CURL_IMAGE` and `NET` constants and the `KUMA_PUSH_TOKEN` /
  `KUMA_PUSH_URL_BASE` / `MONITORING_NETWORK` reads.
- `hold_sha` read/write, Discord alerting, deploy/health-gate/rollback logic: **unchanged**.

### 2. Deployer config — `templates/config.env.j2`

Remove the three now-unused lines: `KUMA_PUSH_TOKEN`, `KUMA_PUSH_URL_BASE`,
`MONITORING_NETWORK`. (`REPO_DIR`, `BRANCH`, `HEALTH_TIMEOUT_S`, `DISCORD_WEBHOOK` stay.)

### 3. monitor-bridge logic — `ansible/roles/containers/monitor-bridge/files/check.py`

Add **two pure functions** (testable without I/O, like `backup_age_hours`):

```python
def gitops_alive(age_s, max_age_s):
    if age_s <= max_age_s:
        return True, "deployer ran %.0fm ago" % (age_s / 60)
    return False, "deployer last ran %.0fm ago (> %.0fm)" % (age_s / 60, max_age_s / 60)

def gitops_status(hold_sha):
    if not hold_sha:
        return True, "no held deploy"
    return False, "deploy held at %s — revert the offending PR" % hold_sha[:8]
```

And **two checks** that read the bind-mounted files and delegate to the pure functions:

```python
GITOPS_STATE_DIR = _env("GITOPS_STATE_DIR", "/gitops-state")
GITOPS_MAX_AGE_S = float(_env("GITOPS_MAX_AGE_MIN", "90")) * 60

def check_gitops_alive():
    try:
        with open(os.path.join(GITOPS_STATE_DIR, "last_run")) as fh:
            ts = float(fh.read().strip())
    except FileNotFoundError:
        return False, "no last_run marker (deployer never completed a tick?)"
    except ValueError:
        return False, "last_run marker unparseable"
    return gitops_alive(time.time() - ts, GITOPS_MAX_AGE_S)

def check_gitops_status():
    try:
        with open(os.path.join(GITOPS_STATE_DIR, "hold_sha")) as fh:
            hold = fh.read().strip() or None
    except FileNotFoundError:
        hold = None
    return gitops_status(hold)
```

Append to the `CHECKS` list:
```python
("gitops_alive",  _env("KUMA_PUSH_GITOPS_ALIVE",  ""), check_gitops_alive),
("gitops_status", _env("KUMA_PUSH_GITOPS_STATUS", ""), check_gitops_status),
```

### 4. monitor-bridge compose — `templates/docker-compose.yml.j2`

- **Bind-mount** the deployer state dir read-only (alongside `./check.py`):
  ```yaml
  - /var/lib/gitops-deploy:/gitops-state:ro
  ```
- **Env**: `GITOPS_STATE_DIR=/gitops-state`, `GITOPS_MAX_AGE_MIN=90`, and the two tokens
  `KUMA_PUSH_GITOPS_ALIVE={{ monitor_bridge_gitops_alive_push_token }}`,
  `KUMA_PUSH_GITOPS_STATUS={{ monitor_bridge_gitops_status_push_token }}`.
- **AutoKuma labels**: replace the single `gitops-deploy` label (line 76) with two
  (interval 600 = 10-min window, 2× the 5-min loop, matching the other monitor-bridge
  monitors):
  ```jinja
  {{ kuma('gitops-deploy-alive',  monitor_type='push', name='GitOps Deploy — Alive',  interval=600, push_token=monitor_bridge_gitops_alive_push_token) }}
  {{ kuma('gitops-deploy-status', monitor_type='push', name='GitOps Deploy — Status', interval=600, push_token=monitor_bridge_gitops_status_push_token) }}
  ```

### 5. Secrets — `ansible/vars/secrets.yml`

- **Add** `monitor_bridge_gitops_alive_push_token` and `monitor_bridge_gitops_status_push_token`
  — each **exactly 32 alphanumeric chars** (`openssl rand -hex 16`); AutoKuma silently
  refuses other formats.
- **Retire** `gitops_deploy_kuma_push_token` (no longer referenced).

## Failure semantics (what a red dot means)

| Monitor | Red when | Cannot be tripped by |
|---|---|---|
| **GitOps Deploy — Alive** | deployer stalled (`last_run` > 90 min) → bridge pushes `down`; **or** monitor-bridge/host itself dead → Kuma's 10-min window trips (and *all* monitors go red, so it's globally obvious) | slow deploy, dirty tree, broad-defer, idle, manual deploy |
| **GitOps Deploy — Status** | a rollback wrote `hold_sha` and it's unresolved; re-asserted every 5 min, self-heals on revert | inactivity, maintenance, reboots |

## Migration

AutoKuma reconciles to the declared label set. Removing the `gitops-deploy` label and adding
`gitops-deploy-alive` / `gitops-deploy-status` should **delete** the old monitor (live id 198)
and create the two new ones automatically on the next `monitor-bridge` deploy — no Kuma UI
clicking. **Verify** AutoKuma's delete-on-removal behavior during rollout; if it leaves an
orphan, delete monitor 198 manually once.

## Testing

- Unit-test `gitops_alive` (fresh / stale / boundary) and `gitops_status` (no hold / hold →
  sha8 message) in `ansible/roles/containers/monitor-bridge/files/test_check.py`, the same way
  `backup_age_hours` and `n8n_failures` are tested (pure, no I/O).
- Run: `uv run pytest ansible/roles/containers/monitor-bridge/files`.
- Smoke: `docker exec monitor-bridge python /app/check.py --once` after deploy; confirm both
  new monitors receive `up`.
- `check.py` edits are picked up only on container recreate — already wired via
  `common_config_changed: {{ monitor_bridge_check is changed }}`.

## Risks & open items

1. **`gitops-deploy.service` start-timeout.** It's `Type=oneshot` with no `TimeoutStartSec`,
   yet a real deploy + health-gate can run minutes. **Verify** systemd isn't killing long
   deploys at the default start-timeout (a killed deploy = half-deploy + no `last_run` write).
   If real, add `TimeoutStartSec=0` to the unit. *Flagged; fix only if confirmed.*
2. **Bind-mount source must pre-exist.** `/var/lib/gitops-deploy` is created by the
   `gitops_deploy` role (initial_setup, daniel-server). If `monitor-bridge` deploys first,
   Docker auto-creates the mount source **root-owned**, breaking the deployer's `0750 ubuntu`
   expectation. Ensure gitops_deploy runs before monitor-bridge (normal order), or have the
   monitor-bridge role assert the dir exists with the right owner.
3. **Read access.** monitor-bridge runs as `1000:1000` (=`ubuntu`), the dir owner, so it can
   read `0750`/`0644` markers. Confirmed.

## Rollout order

1. Add the two 32-char tokens to `secrets.yml`; `sops updatekeys` not needed (same recipients).
2. Deploy `gitops_deploy` (writes `last_run`, drops Kuma push) — `initial_setup.yml`
   `--tags gitops_deploy`, or run the deployer once so `last_run` exists.
3. Deploy `monitor-bridge` (`--tags monitor-bridge`); verify the old monitor is gone and both
   new ones go green; smoke-test `--once`.
