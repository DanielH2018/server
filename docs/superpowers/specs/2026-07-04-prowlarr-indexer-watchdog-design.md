# Prowlarr sustained-indexer watchdog

**Date:** 2026-07-04
**Status:** approved (design), pre-implementation
**Origin:** operator request — kill *arr indexer-flap notification noise without falling back to
"only alert when ALL indexers are down." Follows the 2026-07-04 dedup change that made Prowlarr the
single in-app indexer-health reporter (`includeHealthWarnings=false` on Sonarr/Radarr; see the
`discord-and-healthchecks-topology` memory).

## Problem

Prowlarr's in-app health notification is binary. `includeHealthWarnings=true` → it fires the instant
*any* indexer is disabled due to failures, so a transient upstream timeout (EZTV's site slow for
~15 min, then self-recovers) pages just like a real outage. `includeHealthWarnings=false` → the only
indexer signal left is the red **error**, which Servarr only raises when *every* indexer is
unavailable. There is no native duration/grace knob, so there is no middle ground: you get jitter, or
you get nothing until total blackout.

Observed: over the full Prowlarr log retention, public trackers flap routinely (1337x 28×, TheRARBG
9×, TPB 7×, YTS 5×, EZTV 2×), almost always self-clearing inside Prowlarr's escalating backoff
(~5–15 min). Each flap currently pages.

## Goal

Alert only on **sustained per-indexer** outages (an individual indexer down ≥ 30 min), suppress
sub-30-min flaps entirely, and keep an **instant** signal for the genuine all-indexers-down
emergency — reusing the existing `monitor-bridge` → Uptime Kuma push pattern. No new component, no
new network attachment (monitor-bridge already reaches `prowlarr:9696` over `media`).

## Architecture (chosen)

`monitor-bridge` polls Prowlarr's API each cycle, computes how long each currently-failing indexer
has been failing from Prowlarr's own `initialFailure` timestamp, and pushes up/down to a Kuma push
monitor — exactly like its other 26 checks (direct precedent: `check_arr_queue`, which already hits
the *arr APIs over `media`).

**Age-based, not consecutive-cycle streak.** Chosen because the 30-min window is long relative to how
often monitor-bridge is recreated (any config deploy can recreate it): a streak counter would reset
on redeploy and restart the ~30-min clock mid-outage, whereas `initialFailure` is authoritative from
Prowlarr and survives a bridge restart. It also gives exact wall-clock "down for N min" semantics.

```
Prowlarr (indexer failing → escalating backoff, listed in /api/v1/indexerstatus)
monitor-bridge  check_prowlarr_indexers()  (every INTERVAL=300s)
   ├─ GET http://prowlarr:9696/api/v1/indexerstatus   (X-Api-Key)  → [{indexerId, initialFailure, disabledTill, ...}]
   ├─ GET http://prowlarr:9696/api/v1/indexer         (X-Api-Key)  → id→name map
   ├─ down_list = [(name, mins) for s in status if (now - s.initialFailure) >= MIN_DOWN_MIN]
   └─ push status=up|down&msg=… to Kuma monitor "Prowlarr Indexers"
Uptime Kuma push monitor (interval 600s, retries 0)
   └─ also DOWN if monitor-bridge itself dies (no push) — the existing dead-man's-switch
```

### Coverage after this change

| event                          | who pages                     | latency  |
|--------------------------------|-------------------------------|----------|
| indexer flaps < 30 min         | nobody                        | —        |
| one indexer down ≥ 30 min      | monitor-bridge (names it)     | ~30 min  |
| all indexers down              | Prowlarr red error (in-app)   | instant  |

The instant all-down path is retained by keeping Prowlarr's `onHealthIssue=true`; the bridge owns the
per-indexer sustained signal it can't express.

## Components

### 1. monitor-bridge (`roles/containers/monitor-bridge`)
- **`files/check.py`**: new pure decision function + a `check_prowlarr_indexers()` wrapper (stdlib
  `urllib` only, matching `check_arr_queue`'s authenticated-GET pattern with an `X-Api-Key` header).
  - Pure, unit-tested: `indexers_down(status, names, now, min_down_min)` → `list[(name, minutes)]`
    for every entry where `now − initialFailure ≥ min_down_min`. `initialFailure` parsed as ISO/UTC
    and compared to `datetime.now(timezone.utc)` (same TZ-safe idiom as `ha_heartbeat_fresh`). A
    null/absent `initialFailure` → treat as just-started (0 min, never qualifies) rather than
    crashing.
  - Wrapper returns `(ok, msg)`:
    - reachable, no indexer ≥ threshold → `(True, "N indexers OK")` (or "no failing indexers")
    - one+ sustained → `(False, "EZTV down 31m; 1337x down 47m")` (names each + duration)
    - unreachable / HTTP error → `(False, "<reason>")` **immediately, no grace** — matches the
      `check_arr_queue`/`check_n8n`/`check_scrutiny` precedent (no shared root cause to gate here).
    - empty `PROWLARR_API_KEY` ⇒ disabled, returns `up` (same "empty = disabled, stays up" convention
      as `N8N_API_KEY`/`PI_GLANCES_URL`).
  - Add `("prowlarr_indexers", _env("KUMA_PUSH_PROWLARR_INDEXERS", ""), check_prowlarr_indexers)` to
    `CHECKS`. **Not** added to `PROM_DEPENDENT` (not Prometheus-backed) — the `PROM_DEPENDENT`
    guard-test against live `CHECKS` still passes.
- **`templates/docker-compose.yml.j2`**: new env — `PROWLARR_URL=http://prowlarr:9696`,
  `PROWLARR_API_KEY={{ prowlarr_api_key }}`, `PROWLARR_INDEXER_MIN_DOWN_MIN=30`,
  `KUMA_PUSH_PROWLARR_INDEXERS={{ monitor_bridge_prowlarr_indexers_push_token }}`; new label via the
  shared macro `kuma('monitor-bridge-prowlarr-indexers', monitor_type='push', name='Prowlarr
  Indexers', interval=600, max_retries=0,
  push_token=monitor_bridge_prowlarr_indexers_push_token)`.
  **Network note:** monitor-bridge is already on `media` (joined 2026-07-02 for `check_arr_queue`), so
  it reaches `prowlarr:9696` today — no network change anywhere.
- **`files/test_check.py`**: unit-test `indexers_down` — below threshold → empty, at/above → named,
  null `initialFailure` → empty, multi-indexer ordering/naming, empty status → up; plus the
  disabled-key (empty `PROWLARR_API_KEY`) → up(disabled) path. Mock the HTTP layer the same way the
  existing check tests do.

### 2. Prowlarr in-app notification (app DB, via API — not templated)
- Set `includeHealthWarnings=false` on Prowlarr's `Discord (health)` connection (notification id 1),
  **keep `onHealthIssue=true`** so the all-indexers-down red error still pages instantly. Applied via
  live API `PUT` (round-trip the full body, flip one flag) — same method used for Sonarr/Radarr on
  2026-07-04. Not git-tracked; SOPS holds the durable webhook copy.

### 3. Secrets (`ansible/vars/secrets.yml` + rotation registry)
- `prowlarr_api_key` — **to add**. Prowlarr's API key (from `docker exec prowlarr cat
  /config/config.xml`, `<ApiKey>`). tier `assisted` (rotate = regenerate in Prowlarr → Settings →
  General, then re-sync). Referenced only by monitor-bridge today.
- `monitor_bridge_prowlarr_indexers_push_token` — **to add**. Kuma push token, **exactly 32
  alphanumeric chars** (`openssl rand -hex 16`) or AutoKuma silently refuses the monitor. tier `auto`
  (like the other `monitor_bridge_*_push_token`s).
- Add both via the `/add-secret` flow → `secret_rotation.py sync` → commit.

## Failure modes / edge cases
- **Prowlarr restart / deploy:** the API is briefly unreachable → this check pushes `down` for that
  cycle (no grace, per the Arr Queue precedent). This is the one spot that can false-page on a
  Prowlarr redeploy. **Deferred mitigation:** if observed noisy, add a 2-cycle consecutive grace to
  the *unreachable* branch only (the `HA_CONSECUTIVE`/`DISCORD_CONSECUTIVE` idiom), leaving the
  age-based indexer-down logic untouched. Not built now (YAGNI until seen).
- **monitor-bridge dies:** no push → Kuma's 600s heartbeat trips the monitor DOWN (existing backstop).
- **Indexer recovers before 30 min:** drops out of `indexerstatus` → never qualifies → no page.
- **Indexer recovers after having paged:** next cycle `down_list` empties → push `up` (auto-clear).
- **Clock skew:** none material (monitor-bridge + Prowlarr both on daniel-server, NTP); UTC-aware
  compare, and `initialFailure` is Prowlarr's own clock (same host).
- **`initialFailure` semantics:** it is set when an indexer *first* fails and cleared on recovery, so
  it does not creep forward on repeated failures within one outage — age reflects the true outage
  start. (Verified against Servarr's IndexerStatus model at build time.)

## Testing & rollout
- `uv run pytest ansible/roles/containers/monitor-bridge/files` (new `indexers_down` tests green) —
  TDD: write the tests first.
- Smoke: `docker exec monitor-bridge python /app/check.py --once` after deploy.
- Deploy: `uv run ansible-playbook ansible/deploy.yml --tags "monitor-bridge"` (Prowlarr already up).
- Verify: AutoKuma creates "Prowlarr Indexers" (UP). Flip-test by temporarily setting
  `PROWLARR_INDEXER_MIN_DOWN_MIN` to `0` (or pointing an indexer at a dead URL) and confirming a
  named DOWN, then restore.
- Apply the Prowlarr `includeHealthWarnings=false` PUT and confirm persisted (GET the notification).

## Out of scope (YAGNI)
- No Prometheus exportarr + `for:` alert rule — there is no Alertmanager here (alerting is
  Kuma-push), so that path would need a bridge into Kuma anyway. More scaffolding, same outcome.
- No per-indexer mute/allowlist — the 30-min gate already removes the noise; add later only if a
  specific indexer proves chronically slow-but-usable.
- No consecutive-cycle grace on the unreachable branch **yet** (see Failure modes — deferred).
- No change to Sonarr/Radarr (already `includeHealthWarnings=false` as of 2026-07-04).
