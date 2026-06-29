# HA automation-engine heartbeat watchdog (review item 4.1)

**Date:** 2026-06-19
**Status:** approved (design), pre-implementation
**Origin:** item 4.1 of the 2026-06-19 Home Assistant review (since pruned; see git history)

## Problem

Home Assistant is monitored only as a *container* (the AutoKuma `home-assistant` docker monitor +
the compose healthcheck, which curls `:8123`). A **wedged-but-running** HA — automation scheduler
stuck, recorder locked, runaway template — still serves HTTP `:8123`, so it looks "up" while every
automation (and therefore every alert HA itself emits) is silently dead. Nothing external notices,
because the alerter is the thing that's broken.

## Goal

Detect a wedged automation engine within ~5 minutes via a signal that lives **outside** HA, reusing
the existing `monitor-bridge` → Uptime Kuma push pattern. No new always-on push logic inside HA, no
new network attachment for the internet-facing HA container.

## Architecture (chosen)

`monitor-bridge` polls HA's API for the freshness of a heartbeat that only stays fresh while HA's
**automation scheduler** is executing, and pushes up/down to a Kuma push monitor — exactly like its
other 16 checks. Chosen over HA-pushes-directly because it keeps "push to Kuma" in the one component
built for it, needs no HA→Kuma network path, and is unit-testable.

```
HA automation (time_pattern /1min)
   └─ input_datetime.ha_heartbeat = now()        # proves the scheduler ran
monitor-bridge  check_ha_heartbeat()  (every INTERVAL=300s)
   └─ GET http://home-assistant:8123/api/states/input_datetime.ha_heartbeat  (Bearer token, over `apps`)
   └─ ok = (now_utc − last_changed) < HA_HEARTBEAT_MAX_AGE (300s)
   └─ push status=up|down&msg=… to Kuma monitor "Home Assistant Automations"
Uptime Kuma push monitor (interval 600s, retries 0)
   └─ also DOWN if monitor-bridge itself dies (no push) — the existing dead-man's-switch
```

### Why this signal is correct
An `input_datetime` updated by a `time_pattern` automation is fresh **iff the automation scheduler
executed** in the last minute. It is strictly stronger than the container healthcheck (HTTP up) and
than reading some integration-polled sensor (which proves polling, not automation execution). A real
wedge → no update → stale → `down`. An HA restart blips it briefly; the 300s age threshold and the
600s Kuma interval both ride that out (the same tolerance every other bridge check uses).

## Components

### 1. Home Assistant (`roles/containers/home-assistant`)
- **`configuration.yaml.j2`**: add an `input_datetime:` helper `ha_heartbeat` (`has_date: true`,
  `has_time: true`). Add `input_datetime.ha_heartbeat` to the existing `recorder: exclude: entities:`
  list — it changes every minute and has zero history value (avoids recorder churn).
- **`files/automations.yaml`**: new automation `ha_heartbeat` — `trigger: time_pattern minutes:"/1"`,
  `mode: single`, `action: input_datetime.set_datetime` with `timestamp: "{{ now().timestamp() }}"`
  on `input_datetime.ha_heartbeat`. No conditions (it must tick unconditionally). Homelab-wide
  (no `bedroom_` prefix); does NOT route through `bedroom_notify`.

### 2. monitor-bridge (`roles/containers/monitor-bridge`)
- **`files/check.py`**: new `check_ha_heartbeat()` (stdlib `urllib` only, matching `check_n8n`'s
  authenticated-GET pattern with a `Authorization: Bearer <token>` header). Returns `(ok, msg)`:
  - reachable + entity present + `last_changed` within `HA_HEARTBEAT_MAX_AGE` → `(True, "fresh (Ns ago)")`
  - stale / entity missing / 401 / unreachable / empty token-disabled → `(False, "<reason>")`,
    EXCEPT empty `HA_URL` or empty `HA_TOKEN` ⇒ disabled, returns `up` (same "empty = disabled, stays
    up" convention as `N8N_API_KEY`/`PI_GLANCES_URL`).
  - Timezone-safe: parse `last_changed` (ISO, UTC) and compare to `datetime.now(timezone.utc)`.
  - Add `("ha_heartbeat", _env("KUMA_PUSH_HA", ""), check_ha_heartbeat)` to `CHECKS`.
- **`templates/docker-compose.yml.j2`**: new env — `HA_URL=http://home-assistant:8123`,
  `HA_TOKEN={{ monitor_bridge_ha_token }}`, `HA_HEARTBEAT_MAX_AGE=300`,
  `KUMA_PUSH_HA={{ monitor_bridge_ha_push_token }}`; new label
  `kuma('monitor-bridge-ha', monitor_type='push', name='Home Assistant Automations', interval=600,
  max_retries=0, push_token=monitor_bridge_ha_push_token)`.
  **Network note:** monitor-bridge is already on `apps`, so it reaches `home-assistant:8123` today —
  no network change anywhere.
- **`files/test_check.py`** (or the existing test module): unit-test `check_ha_heartbeat` decision
  logic — fresh→up, stale→down, missing-entity→down, HTTP-error→down, empty-token→up(disabled) —
  mocking the HTTP layer the same way the existing check tests do.

### 3. Secrets
- `monitor_bridge_ha_token` — **already added** (commit `198773f`, tier `assisted`): the HA
  Long-Lived Access Token (operator-minted; rotate = revoke + reissue in HA UI).
- `monitor_bridge_ha_push_token` — **to add**: the Kuma push token. **Exactly 32 alphanumeric
  chars** (`openssl rand -hex 16`) or AutoKuma silently refuses the monitor. tier `auto` (like the
  other `monitor_bridge_*_push_token`s). Add via `sops set` → `secret_rotation.py sync`.

## Failure modes / edge cases
- **HA restart / deploy:** entity briefly stale; 300s threshold + the automation re-ticking within
  60s of boot absorb it (input_datetime restores its last value via `restore_state` meanwhile).
- **monitor-bridge dies:** no push → Kuma's 600s heartbeat trips the monitor DOWN (existing backstop).
- **HA token expires/revoked:** GET 401 → `down` with "auth failed" — correctly surfaces, doesn't
  silently green.
- **Clock skew:** none material (both containers on daniel-server, same TZ, NTP); UTC-aware compare.

## Testing & rollout
- `uv run pytest ansible/roles/containers/monitor-bridge/files` (new test green) — TDD: write the
  test first.
- Smoke: `docker exec monitor-bridge python /app/check.py --once` after deploy.
- Deploy order: `home-assistant` first (so the heartbeat entity exists), then `monitor-bridge`.
- Verify: AutoKuma creates "Home Assistant Automations" (UP); flip-test by pausing the HA automation
  briefly and confirming it goes DOWN with a descriptive msg, then back UP.

## Out of scope (YAGNI)
- No Prometheus exporter on HA (the API-poll path is simpler and needs no scrape/token-in-scrape).
- No second signal (e.g. recorder-write liveness) — one explicit scheduler heartbeat is enough.
- No notification wiring — inherited automatically from the AutoKuma Discord notification.
