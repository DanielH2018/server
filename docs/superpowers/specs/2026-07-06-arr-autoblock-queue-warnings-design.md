# Arr queue auto-blocklist actor (`arr-autoblock`)

**Date:** 2026-07-06
**Status:** approved (design), pre-implementation
**Origin:** operator request — automate a response to the **Arr Queue Warnings** monitor by
blocklisting stuck/poisoned releases through the *arr API instead of hand-clearing them. Follows the
2026-07-01 incident (an indexer served a poisoned fake-episode `.exe`; Sonarr blocked the import but
only flagged the queue item `warning`, so it sat seeding for a day) that created the read-only
`check_arr_queue` monitor in the first place.

## Problem

`monitor-bridge`'s **Arr Queue Warnings** check (`check.py:765` `check_arr_queue`, pure
`queue_warnings()` at `check.py:732`) *detects and pages* on stuck queue items, but the remediation is
still fully manual: open Sonarr/Radarr, find the flagged item, blocklist + remove it, and kick a new
search. For the common failure classes — a blocked import of a poisoned/fake release, a terminally
errored grab — that manual loop is mechanical and slow (the motivating incident sat a full day).

`monitor-bridge` cannot do the remediation itself: it is **deliberately read-only** (it detects and
pushes to Uptime Kuma, never mutates) and joins trusted infra networks including `kopia`. Giving the
monitor destructive power over the media stack would break that invariant and trust boundary.

## Goal

A least-privilege **writer** sidecar that independently polls the same `/api/v3/queue` endpoints,
auto-blocklists the narrow, clearly-bad classes of stuck items (with a grace period and a blast-radius
cap), triggers a replacement search, and reports every action to Discord — while `monitor-bridge`
continues to be the read-only human-visible signal. Ships **dry-run first**.

## Non-goals

- Not replacing the **Arr Queue Warnings** monitor — it stays as the read-only pager and the
  broader signal (it still surfaces the transient/ambiguous classes this actor deliberately leaves
  alone).
- Not touching Prowlarr indexer health, download-client health, or import-path config — only the
  per-item queue remediation.
- No web UI, no Traefik router, no Authelia.

## Architecture (chosen)

A new container `arr-autoblock`, the mutating twin of read-only `monitor-bridge`, mirroring its shape
(`python:3.14-alpine`, stdlib only, a loop over env-configured settings, a **pure** decision function
that is unit-tested, an Uptime Kuma push monitor for its own liveness). The two run as independent
siblings both reading the *arr queue:

```text
monitor-bridge (read-only) ── detects hard-bad items ─→ Kuma "Arr Queue Warnings"  (pages operator)
arr-autoblock  (writer)    ── polls queue every 300s ─→ blocklist+remove ─→ re-search
     • networks: media (reach sonarr:8989 / radarr:7878) + monitoring (push uptime-kuma:3001, egress)
     • secrets: sonarr_api_key, radarr_api_key (existing), arr_autoblock_push_token (new)
     • image: python:3.14-alpine, stdlib only
```

**Independent polling, not event-driven off the Kuma monitor.** Wiring "act when the Kuma monitor
goes DOWN" would need a webhook receiver and couples the actor to Kuma's alert state; independent
polling is the repo idiom (`monitor-bridge` itself polls) and keeps the actor working even if Kuma is
down. The Kuma monitor remains the human signal; the actor is a parallel reader.

## Auto-block predicate

An item is a candidate when it is **hard-bad** OR **malware-signature** (decision D1, accepted):

```text
candidate if:
    (trackedDownloadStatus == "error" AND NOT client-communication error)
 OR trackedDownloadState  in ("importBlocked", "importFailed")
 OR (trackedDownloadStatus == "warning" AND any statusMessage matches DANGEROUS_MSG_PATTERNS)
```

- **Hard-bad** (`error` / `importBlocked` / `importFailed`): terminal states the *arr will not
  self-resolve.
- **Malware-signature**: `warning` is a coarse enum that lumps the poisoned-`.exe` case in with
  benign transients, so `warning` alone is **not** a trigger. The real signal is in the item's
  `statusMessages[].messages[]`. `DANGEROUS_MSG_PATTERNS` (case-insensitive substring, env-tunable
  via `DANGEROUS_MSG_PATTERNS`) starts as:
  - `executable file with extension`  ← the 2026-07-01 incident, verbatim
  - `potentially dangerous`
  - `sample`
  This is what makes the actor actually cover the incident that motivated the monitor. A `warning`
  with no dangerous message is **left alone** (monitor-bridge still pages it for a human).

- **Client-communication-error exclusion** (added 2026-07-06, before the live flip): a *bare*
  `trackedDownloadStatus == "error"` is **excluded** when a `statusMessage` or the queue record's
  top-level `errorMessage` matches `CLIENT_ERROR_PATTERNS` (case-insensitive substring, env-tunable;
  default `unable to communicate` / `not responding` / `failed to connect` / `connection refused` /
  `download client is unavailable`). This stops a transient qBittorrent/VPN outage — which flips
  legitimate in-progress downloads to `error` — from wrongly blocklisting them. The exclusion applies
  **only** to the bare-`error` bucket: `importBlocked`/`importFailed` (the download completed, so a
  client outage can't produce them) and malware-signature items are still candidates. `stalled`/
  no-seeders is deliberately **not** excluded — a dead-seeded release is a legitimate
  blocklist-and-re-search case.

Everything the predicate does not match — transient `warning`, plain `importPending` with messages —
stays **notify-only** via the existing monitor. Failing to match fails **safe** (no action).

## Action logic (per cycle, `INTERVAL` = 300 s)

1. `GET` Sonarr + Radarr `/api/v3/queue?includeUnknown{Series,Movie}Items=true&pageSize=250` with
   `X-Api-Key` (same URLs/keys/network as `check_arr_queue`).
2. Compute candidates via the predicate above.
3. **Grace (consecutive-cycle):** in-process `dict {downloadId: count}` (fall back to the queue
   record `id` when `downloadId` is absent). Each cycle, increment still-candidate items and drop the
   rest (a candidate that clears resets). An item is **eligible** at `count >= GRACE_CYCLES`
   (default **3** ≈ 15 min — same hysteresis idiom as `CPU_CONSECUTIVE`/`HA_CONSECUTIVE`). In-process
   state means a redeploy resets the counter — the **safe** direction (waits longer, never acts
   early). Documented, consistent with monitor-bridge's streak checks.
4. **Blast-radius valve:** if the eligible count in a cycle exceeds `MAX_ACTIONS_PER_CYCLE`
   (default **5**), act on **none**; push `down` + Discord "N items eligible — holding, investigate".
   A mass-flag is almost always a systemic cause (disk full breaking every import, an indexer that
   poisoned a whole batch) where auto-nuking everything is wrong. The operator investigates.
5. For each eligible item, unless `DRY_RUN`:
   1. `DELETE /api/v3/queue/{id}?removeFromClient=true&blocklist=true` — removes from the download
      client **and** blocklists the release so it is never re-grabbed.
   2. Re-search at **series/movie granularity** (decision D3, accepted):
      - Sonarr: `POST /api/v3/command {name: "SeriesSearch", seriesId: <id>}`
      - Radarr: `POST /api/v3/command {name: "MoviesSearch", movieIds: [<id>]}`

      Series/movie-level is robust for **season packs** (a queue record's `episodeId` may not
      represent every episode in a stuck pack); the *arr only grabs genuine gaps, so this does not
      produce spurious downloads. The blocklist guarantees the replacement search cannot re-pick the
      release just killed.

## Reporting & health

- **Discord action log** — direct POST to the existing `arr_discord_webhook_url` (the same channel
  the *arr already post their own health alerts to) for each action:
  - dry-run: `WOULD blocklist [Sonarr] <title> — importBlocked (3/3)`
  - live: `Blocklisted + re-searched [Radarr] <title> — status=error`
  - blast-valve: `N queue items eligible — holding (max 5/cycle), investigate`

  Titles and reasons pass through `sanitize()` (release titles are attacker-influenced). The POST
  **must set a `User-Agent` header** — direct urllib Discord POSTs 403 silently (Cloudflare 1010)
  without one (known repo gotcha; see `renovate_notify`/`gitops_deploy`). The **Discord Delivery**
  monitor already GET-verifies `arr_discord_webhook_url`, so a rotated webhook is caught.
- **Kuma liveness** — its own push monitor **"Arr Auto-Block"** via the `kuma()` macro: `up` on a
  clean cycle, `down` on an unreachable *arr API or a failed `DELETE`/`command` (fail-loud, the
  `check_arr_queue`/`check_n8n` convention). This monitors the actor's **health**, distinct from the
  Discord action feed. `max_retries=0` like every bridge push monitor.

## Error handling

- Unreachable *arr / non-2xx on `GET`, `DELETE`, or `command` → push `down` with the error and take
  **no** further action that cycle (never leave a partial-nuke). Next cycle retries from a clean read.
- `DRY_RUN=true` is the **initial default**: log + Discord-report intended actions, mutate nothing.
  Flip to `false` after an observation window.
- Empty `SONARR_API_KEY`/`RADARR_API_KEY` independently skip that app; both empty → disabled (stays
  `up`). Same convention as `check_arr_queue`.

## Components (`ansible/roles/containers/arr-autoblock/`)

- `files/autoblock.py` — the loop plus a **pure** `eligible(candidates, streaks, grace, max_actions)`
  → `(to_act, held)` decision function (no I/O), the unit-tested core, and `is_candidate(item,
  patterns)` / `dangerous(messages, patterns)` helpers.
- `files/test_autoblock.py` — pytest suite, added to `pyproject.toml` `[tool.pytest.ini_options]`
  `testpaths`.
- `templates/docker-compose.yml.j2` — shared macros: `kuma()` (push monitor "Arr Auto-Block"),
  `healthcheck()` (touch `/tmp/heartbeat` like monitor-bridge → autoheal a hung loop),
  `resources()`, `service_networks()`/`external_networks()` for `[media, monitoring]`. No Traefik,
  no Authelia.
- `tasks/main.yml` — render dir + copy `files/`, deploy via the `common`/`docker_deploy` include.
- `meta/deps.yml` — depends on `sonarr`, `radarr`, `uptime-kuma`.

## Infra wiring / new secrets

- `arr_autoblock_push_token` — 32 alphanumeric chars — added to `secrets.yml` (via `/add-secret` or
  `sops`), then `uv run python scripts/secret_rotation.py sync`. Passed both as env (what the script
  pushes to) and as `push_token=` in the `kuma()` label.
- Reuses existing `sonarr_api_key`, `radarr_api_key`, `arr_discord_webhook_url`.
- `containers_list` entry in `ansible/inventory/host_vars/daniel-server.yml`: `name: arr-autoblock`,
  **no** `port`, `use_authelia: false`, `networks: [media, monitoring]`. Deploy tags derive from the
  name automatically.
- Deploy ordering: `sonarr`/`radarr`/`uptime-kuma` before `arr-autoblock` (deps.yml).

## Env / thresholds (compose template)

`INTERVAL=300`, `GRACE_CYCLES=3`, `MAX_ACTIONS_PER_CYCLE=5`, `DRY_RUN=true` (initial),
`DANGEROUS_MSG_PATTERNS=executable file with extension,potentially dangerous,sample`,
`SONARR_URL`/`SONARR_API_KEY`/`RADARR_URL`/`RADARR_API_KEY`, `ARR_DISCORD_WEBHOOK_URL`,
`KUMA_PUSH_ARR_AUTOBLOCK` + `UPTIME_KUMA_URL`.

## Testing

Unit tests for the pure core (`uv run pytest ansible/roles/containers/arr-autoblock/files`):

- `is_candidate`: hard-bad states match; plain `warning` does **not**; `warning` + a dangerous
  `statusMessage` **does**; `warning` + benign message does not; `importPending` + messages does not.
- `dangerous`: matches each seed pattern case-insensitively; ignores unrelated messages.
- `eligible`: not eligible until `count >= GRACE_CYCLES`; a candidate that clears resets its streak;
  when eligible count > `MAX_ACTIONS_PER_CYCLE`, `to_act` is empty and `held` carries all of them.
- Dry-run: `eligible()` still returns the intended `to_act` (dry-run only gates the I/O in the loop,
  not the decision), so the actionable set is reported without mutation.

Smoke test one pass after deploy: `docker exec arr-autoblock python /app/autoblock.py --once`.

## Rollout

1. Deploy with `DRY_RUN=true`. Watch the Discord `WOULD blocklist …` reports and the "Arr Auto-Block"
   Kuma monitor for a week.
2. Confirm it only targets genuine bad releases (no false positives against transient warnings).
3. Flip `DRY_RUN=false` (template edit + redeploy) to enable real blocklist + re-search.

## Accepted decisions

- **D1 = yes** — fold the malware-signature trigger into the predicate so `warning`-status poisoned
  releases (the 2026-07-01 class) are covered; generic warnings stay notify-only.
- **D2 = defaults** — `GRACE_CYCLES=3`, `MAX_ACTIONS_PER_CYCLE=5`, `INTERVAL=300`.
- **D3 = series/movie-level** re-search (`SeriesSearch` by `seriesId`, `MoviesSearch` by `[movieId]`)
  — robust for season packs; the *arr only grabs genuine gaps.
