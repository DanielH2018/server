# monitor-bridge — metric & backup alerting → Uptime Kuma

A tiny sidecar that turns Prometheus metrics and Kopia backup state into Uptime Kuma
**push** monitors, so threshold/backup problems actually page. See repo-root `CLAUDE.md`.

## At a glance
- **Image:** `python:3.14-alpine` (stdlib only — no build, no extra deps)
- **Host:** daniel-server · **No web UI**, no Authelia
- **Networks:** `monitoring` (reach `prometheus:9090`, `uptime-kuma:3001`) + `kopia`
  (reach `kopia:51515`) + `apps` (reach the n8n public API at `n8n:5678`) + `media`
  (since 2026-07-02: reach `sonarr:8989`/`radarr:7878` for `check_arr_queue`, same
  precedent as homepage). Joins the `kopia` net as trusted infra — like Traefik — so Kopia
  stays off `monitoring` and apps still can't reach the unauthenticated `kopia:51515`.
- **Depends on:** prometheus, uptime-kuma, kopia (`meta/deps.yml`)
- **Config in:** `ansible/inventory/host_vars/daniel-server.yml` → `containers_list`

## Notable
- `files/check.py` is a **static** Python loop (config via env vars, no Jinja). Every
  `INTERVAL` (300 s) it runs **twenty-six checks** and pushes `status=up|down&msg=…` to one Kuma push
  monitor each:
  - **Prometheus Reachable** (a trivial `vector(1)` instant query — the root-cause GATE for the
    prom-dependent checks. Evaluated FIRST each cycle: when Prometheus is unreachable, the nine
    prom-dependent checks (disk/cert/memory/restarts/oom/cpu/targets/traefik5xx/b2_trend) are
    **suppressed** — pushed `up` with a "skipped — Prometheus unreachable" msg so their push-monitor
    heartbeats stay alive — and only THIS monitor pages. Without the gate one Prometheus outage
    fired all nine at once: one root cause, a nine-monitor alert storm. A single scrape target down
    (Prometheus up, one exporter gone) still surfaces separately on Scrape Targets. The
    `PROM_DEPENDENT` set is guarded by a test against the live `CHECKS` so it can't drift.)
  - **Backup Freshness** (Kopia `/api/v1/sources` last-snapshot age + errorCount)
  - **Root Disk** (`node_filesystem_*` for `/`, `/boot` **and `/boot/efi`** — old kernels
    filling /boot quietly breaks upgrades, and a full ESP breaks firmware/bootloader
    updates the same way; server-only, the Pi's disk lives in the Pi Pressure check)
  - **TLS Cert Expiry** (`traefik_tls_certs_not_after`)
  - **Memory** (host `node_memory_*` pressure only)
  - **Container Restarts** (`changes(container_start_time_seconds[15m]) > RESTART_MAX`)
  - **Container OOM** (`increase(container_oom_events_total[1h]) by (name)` — names the
    offender; supersedes the old host-aggregate OOM that lived in the Memory check)
  - **CPU Throttling** (throttled/total CFS *periods* `> CPU_THROTTLE_PCT` **and** throttled
    *seconds*/s `> CPU_MIN_THROTTLED_CORES`, by name — catches a container pinned at its
    `deploy.resources` cpu cap, which throttles silently without OOM/restart/5xx. The cores
    floor (same volume-floor idea as Traefik's `TRAEFIK_MIN_RPS`) is essential: the period
    ratio alone runs 30–90% for tiny low-limit sidecars that briefly burst over their slice
    while losing negligible absolute CPU — a perpetual false `down`, which Kuma renders as
    "No heartbeat in the time window" since only `up` pushes satisfy a push monitor's
    watchdog. Unlimited containers give 0/0→NaN and are ignored. On top of both gates,
    `CPU_CONSECUTIVE` (3) adds hysteresis: only the 3rd consecutive breaching cycle
    (~15 min) pushes `down`; shorter bursts push `up` with a "throttling streak n/3"
    msg naming the offender, and a clean cycle resets the streak — so one-cycle blips
    (flaresolverr solving a challenge, homepage hugging the cores floor) never page.)
  - **Scrape Targets** (`up == 0` — names the down job)
  - **Traefik 5xx** (5xx ratio over 5m **per service**, naming each offender, gated by a
    per-service `TRAEFIK_MIN_RPS` volume floor — per-service so the alert points at the
    erroring backend and a broken low-traffic service can't hide diluted in the aggregate)
  - **n8n Prod Workflows** (n8n public API: failed executions of *active* workflows within
    `N8N_FAIL_WINDOW`, naming each one. "Prod" = active. Empty `N8N_API_KEY` = disabled
    (stays up); an unreachable API surfaces as `down`. Reached at `n8n:5678` over `apps`,
    bypassing Authelia via the `X-N8N-API-KEY` header. Caps the workflow page at 250 and the
    error-execution page at 100 — ample for a homelab window.)
  - **Arr Queue Warnings** (sonarr's + radarr's own `/api/v3/queue`: `down` on any item with
    `trackedDownloadStatus == "warning"`, `trackedDownloadState == "importBlocked"`, or
    `importPending` carrying `statusMessages` — naming the release title + app. Added after the
    2026-07-01 incident: an indexer served a poisoned fake-episode `.exe`; sonarr blocked the
    import itself and flagged the queue item `warning` ("Caution: Found executable file with
    extension: '.exe'"), but nothing paged, so the release sat seeding for a full day before a
    manual review caught it. `SONARR_API_KEY`/`RADARR_API_KEY` are independent — an empty one
    skips that app, both empty disables the whole check (stays up), like `N8N_API_KEY`. An
    unreachable *arr API is NOT given grace/hysteresis — it surfaces as `down` immediately via
    the same `_evaluate` path as `check_n8n`/`check_scrutiny` (no shared root cause here to
    gate, unlike the Prometheus/exporter checks). Pure `queue_warnings()` is unit-tested.)
  - **GitOps Deploy — Alive** (reads `/gitops-state/last_run`, a bind-mounted host timestamp the
    `gitops_deploy` deployer rewrites each non-crashing tick; `down` once it's older than
    `GITOPS_MAX_AGE_MIN` — i.e. the deployer stalled / host down. The deployer no longer pushes
    to Kuma itself — see [[the gitops_deploy CLAUDE.md]])
  - **GitOps Deploy — Status** (reads `/gitops-state/hold_sha`; `down` while a rolled-back commit
    is held pending the operator reverting the offending PR — self-heals when the hold clears)
  - **Backup Restore Drill** (reads `/restore-drill/state.json`, written monthly by the kopia
    role's `kopia-restore-drill.sh` host cron — `down` on a failed drill, >35 d staleness, or a
    missing/corrupt state file. Same state-file pattern as the GitOps monitors.)
  - **Backup Verify** (reads `/verify/state.json`, written weekly by the kopia role's
    `kopia-verify.sh` host cron — `down` on a FAILED `kopia snapshot verify` (detected
    bit-rot / an unreadable blob), >10 d staleness, or a missing/corrupt state file. The
    verify tier of the three-tier backup assurance: it proves stored blobs are READABLE
    across ALL snapshots, where the restore drill proves ONE service's tree restores. The
    script captures the verify's own exit code — the old `... | logger` cron made cron see
    logger's always-zero exit and silently swallowed a non-zero verify.)
  - **B2 Storage Usage** (reads `/b2-usage/state.json`, written daily by the kopia role's
    `kopia-b2-usage.sh` host cron with the bucket's **billable** bytes — `rclone size
    --b2-versions`, which counts hidden versions the way B2 bills them, NOT `kopia blob
    stats`. The repo lives on B2's 10 GB free tier; `down` above `B2_USAGE_MAX_PCT` (85%)
    of `B2_CAP_GB`, on probe failure, >2.5 d staleness, or missing state — runway to
    prune/upgrade before a full bucket silently kills the nightly snapshots.)
  - **B2 Usage Trend** (`predict_linear(kopia_b2_billable_bytes[7d], 7d)` — the runway warning the
    absolute-85% **B2 Storage Usage** monitor can't give. The daily `b2-usage.sh` cron already
    exports billable bytes as the `kopia_b2_billable_bytes` Prometheus gauge (node-exporter
    textfile); this fits a linear trend over `B2_TREND_WINDOW` and goes `down` when the bucket is on
    track to cross the cap within `B2_TREND_HORIZON_D` days (default 7) — catching the recurring
    fast-growth incidents (hidden-version / LiveSync churn) while there's still headroom to act.
    Flat/shrinking usage → up; a missing gauge (cron not exporting / textfile collector broken) →
    down (fail-stale), distinct from B2 Storage Usage's state.json staleness. **Prom-dependent** —
    suppressed under the Prometheus Reachable gate. Pure `b2_trend()` is unit-tested. node-exporter
    serves the last textfile value every scrape, so predict_linear has a dense series even between
    the cron's daily writes — a stalled cron reads flat here (its staleness is B2 Storage Usage's job).)
  - **SMART Data Freshness** (scrutiny web API `/api/summary` over `monitoring`: every
    non-archived device must have a `collector_date` within 26 h — the collector is
    cron-as-PID1 with no usable healthcheck, so a silently-dead collector only shows up as
    aging SMART data. Also `down` when scrutiny lists no devices at all.)
  - **Pi Pressure** (the Pi's glances API `/api/4/load` + `/api/4/mem` + `/api/4/fs` over
    the LAN: `down` when load5/core > `PI_LOAD_MAX`, mem `available` < `PI_MEM_MIN_MB`, or
    any filesystem device > `PI_DISK_MAX_PCT` — glances' fs list is its *container* view
    (bind-mount paths), so entries are deduped by `device_name`, which carries the host SD
    card's usage percent. A filling SD card is the classic slow Pi death the server-only
    Root Disk check can't see. The 512MB
    Zero 2 W dies by swap-thrash — 2026-06-11 fwupd episodes ran load5/core >1.7 with
    healthcheck-timeout storms no other monitor saw. Polls glances rather than adding a
    Pi node-exporter: zero Pi-side RAM cost, and a second node-exporter would have broken
    the instance-blind `node_*` queries in the Memory/Root Disk checks. Empty
    `PI_GLANCES_URL` = disabled (stays up); the static Kuma HTTP monitor
    `daniel-pi-glances` covers glances itself being down.)
  - **Home Assistant Automations** (HA's REST API `/api/states/input_datetime.ha_heartbeat` over
    `apps`, Bearer `HA_TOKEN`: an HA `time_pattern:/1min` automation stamps that helper with `now()`,
    so its `last_changed` is fresh ONLY while HA's automation *scheduler* is executing. `down` once
    it's older than `HA_HEARTBEAT_MAX_AGE` (300 s) — a wedged-but-running HA (HTTP `:8123` up,
    scheduler stuck) that the container healthcheck can't see. **Consecutive-cycle hysteresis
    (`HA_CONSECUTIVE`=2, same idiom as `CPU_CONSECUTIVE`):** a planned redeploy takes the API
    unreachable for ~120 s and then leaves the scheduler a beat behind, so a single cycle reads
    unreachable OR stale — only the 2nd straight down cycle pages; the first pushes `up` with a
    "down streak n/N" msg, and one fresh read resets the streak. The unreachable-API error is
    caught inside the check (not left to `run_once`) so it rides the SAME grace as staleness — both
    are the deploy, not a wedge; a genuinely wedged/auth-broken HA stays bad across cycles and still
    pages. Empty `HA_URL`/`HA_TOKEN` = disabled (stays up). Pure `ha_heartbeat_fresh()` + the
    streak wrapper are unit-tested.
    Spec: `docs/superpowers/specs/2026-06-19-ha-automation-heartbeat-watchdog-design.md`.)
  - **Renovate Notifier — Alive** (reads `/renovate-state/last_run`, a bind-mounted host
    timestamp the `renovate_notify` daily timer rewrites each clean run; `down` once it's
    older than `RENOVATE_MAX_AGE_MIN` (2160 = 36 h, one missed daily run + slack) — i.e. the
    notifier stalled / host down. Same state-file dead-man's-switch pattern as the GitOps
    monitors. Spec: `docs/superpowers/specs/2026-06-19-renovate-manual-action-notifier-design.md`.)
  - **Loki Log Ingestion** (instant LogQL `sum(count_over_time({job=~".+"}[30m]))` against
    `loki:3100` over `monitoring`: `down` at zero lines — a silently-dead promtail→Loki pipeline
    (docker-proxy break, positions-file corruption, relabel regression) that Loki's `/ready` Kuma
    probe stays green through. Counts ALL file-tailed streams (`authlog`+`syslog`+`traefik`), not one:
    if promtail dies they ALL fall silent together, while no single low-volume file going quiet can
    trip it. **Second arm (added after the docker stream blind-spot review): also counts
    `{container=~".+"}` (`LOKI_DOCKER_STREAM`) — the docker_sd stream carries a `container`
    label, no `job`, so it's outside `{job=~".+"}`; a docker_sd-specific break (docker-proxy
    down, the docker relabel regressing) would silence every container log while the file-tail
    union keeps flowing. `down` if EITHER arm is silent. Promtail also now stamps the docker
    stream `job: docker` so it's filterable + falls under the union too.** **Originally `{job="syslog"}` over 10m — false-paged 2026-06-23 because this debloated
    host routinely idles >15m between syslog writes (a 15m35s gap was observed), so a normal quiet
    spell read as a dead pipeline; the broadened selector + 30m window is the fix.** Stream/window
    tunable via `LOKI_STREAM`/`LOKI_WINDOW`. Pure `loki_ingestion_fresh()` + `loki_count()` are
    unit-tested. A freshness watchdog in the same idiom as the SMART/restore-drill checks.)
  - **Discord Delivery** (GET-verifies **all three** Discord notification webhooks: Kuma's own
    `monitor_discord_webhook_url` — the one Kuma POSTs every alert to — CrowdSec's
    `crowdsec_discord_webhook_url`, which CrowdSec POSTs ban alerts to *directly* (not via Kuma), and
    the `gitops_deploy_discord_webhook`, which delivers the gitops-deploy rollback alert AND every
    `renovate_notify` digest (its Renovate Notifier — Alive marker greens even when the POST fails —
    no Kuma backstop). The latter two have NO Kuma backstop of their own. `down` if ANY is invalid,
    naming which; each empty URL is skipped. A
    rotated/revoked/deleted webhook makes those alerts silently fail to deliver while every monitor
    stays GREEN in the Kuma UI; this is the alert chain's delivery hop that NO other monitor — not
    even the off-box UptimeRobot host dead-man — exercises. A webhook GET returns Discord's metadata (200) when valid
    and 404 once gone, and never posts a message (no channel spam) — unlike a test POST. The only
    check that reaches the PUBLIC internet, so `DISCORD_CONSECUTIVE` (2) adds the same streak
    hysteresis as the HA heartbeat: a single transient non-200/network blip pushes `up` with a
    "down streak n/N" msg and only the 2nd straight failure pages. Empty `DISCORD_WEBHOOK_URL` =
    disabled (stays up), like `N8N_API_KEY`. Pure `discord_webhook_ok()` + the streak wrapper are
    unit-tested. NOTE: it verifies the webhook is DELIVERABLE (catches a rotated/revoked URL); it
    does NOT assert Kuma still has the notification *attached* to each monitor — AutoKuma re-applies
    that on every deploy via the `kuma()` macro's `notification_name_list`.)
  - **Recyclarr Sync** (instant LogQL counts of supercronic's `job succeeded` / `job failed` lines
    for `{container="recyclarr"}` over `RECYCLARR_WINDOW` (26 h = one `@daily` run + slack), reached
    at `loki:3100`: recyclarr runs `recyclarr sync` under supercronic and `/cron.sh` ends with the
    sync, so its exit code propagates — supercronic logs `job succeeded` on exit 0, `job failed` on
    non-zero. `down` on any `job failed` (a sync errored) OR zero `job succeeded` (the scheduler
    stalled / every run failing). The container healthcheck only watches supercronic, so an ERRORING
    sync was previously invisible — the silent 2026-06-10 v8-major breakage that failed every nightly
    sync with the healthcheck staying green. Pure `recyclarr_sync_ok()` is unit-tested; selector/window
    tunable via `RECYCLARR_LOKI_SELECTOR`/`RECYCLARR_WINDOW`. Same Loki-query path as Loki Log Ingestion.)
- The restart/OOM/cpu/target/5xx checks use `prom_vector()` (keeps series labels) so the alert
  names *which* container / target / route is failing; the others use `prom_scalar()`.
- Explicit `down` = fast, descriptive alert; the push monitor's heartbeat interval (600 s,
  2× the loop) is the backstop for "the bridge itself died". Same dead-man's-switch idea as
  `cloudflare-ddns` — see [[its CLAUDE.md]] and the `kuma(..., monitor_type='push')` macro.
- **All push monitors set `max_retries=0`** (2026-06-12): with retries, Kuma parks a pushed
  `down` in PENDING and the 60s watchdog — which only `up` pushes satisfy — crosses
  maxretries first, so every visible DOWN event read "No heartbeat in the time window"
  instead of the check's named-offender msg. Zero retries means the bridge's own push flips
  the state and the descriptive msg lands in the event + Discord notification. Trade-off:
  a dead bridge pages after one missed 600s window (acceptable — that's the dead-man's
  switch doing its job).
- **Container healthcheck (2026-06-10):** check.py touches `/tmp/heartbeat` (tmpfs) after
  every cycle; the compose healthcheck goes unhealthy when the mtime exceeds ~3×INTERVAL,
  so autoheal restarts a *hung* loop (death alone already exits the container). Kuma push
  silence remains the alerting path; the healthcheck adds auto-recovery.
- Push tokens (`monitor_bridge_{kopia,disk,cert,mem,restarts,oom,cpu,targets,traefik,prometheus,n8n,arr_queue,gitops_alive,gitops_status,scrutiny,pi,b2,b2_trend,ha,renovate_alive,loki,verify,maintenance,discord,recyclarr}_push_token` + `kopia_restore_drill_push_token`)
  live in `secrets.yml`; we set them and Kuma honors client-supplied tokens. They're passed
  both as env (what the script pushes to) and as `push_token=` in the AutoKuma label.
- The **Home Assistant Automations** check additionally needs `monitor_bridge_ha_token` — an HA
  **Long-Lived Access Token** (operator-minted in HA → Profile → Security; can't be templated), NOT
  a Kuma push token. tier `assisted` (rotate = revoke + reissue in HA). Empty `HA_TOKEN` disables it.
- The two GitOps monitors read host state via a **read-only bind-mount**
  `/var/lib/gitops-deploy:/gitops-state:ro` (written by the `gitops_deploy` host role) — no
  Prometheus/Kopia/n8n source. That dir must exist owned by the deploy user before deploy; the
  `gitops_deploy` role creates it, so deploy `gitops_deploy` before `monitor-bridge` (else Docker
  auto-creates the mount source root-owned and the non-root container can't read it).
- Similarly, the **Renovate Notifier — Alive** monitor bind-mounts
  `/var/lib/renovate-notify:/renovate-state:ro` (written by the `renovate_notify` daily timer).
  Deploy `renovate_notify` before `monitor-bridge` for the same reason — so the dir is created
  and owned by the deploy user, not root.
- Thresholds are env-tunable in the compose template (`BACKUP_MAX_AGE_H`, `DISK_MAX_PCT`,
  `CERT_MIN_DAYS`, `MEM_MAX_PCT`, `RESTART_WINDOW`/`RESTART_MAX`, `OOM_WINDOW`,
  `CPU_WINDOW`/`CPU_THROTTLE_PCT`/`CPU_MIN_THROTTLED_CORES`/`CPU_CONSECUTIVE`, `TRAEFIK_5XX_PCT`/`TRAEFIK_MIN_RPS`,
  `N8N_FAIL_WINDOW`/`N8N_FAIL_MAX`; n8n connection config: `N8N_URL`/`N8N_API_KEY`; arr queue
  connection config: `SONARR_URL`/`SONARR_API_KEY`/`RADARR_URL`/`RADARR_API_KEY`; GitOps
  liveness: `GITOPS_MAX_AGE_MIN`/`GITOPS_STATE_DIR`; Pi pressure:
  `PI_GLANCES_URL`/`PI_LOAD_MAX`/`PI_MEM_MIN_MB`/`PI_DISK_MAX_PCT`; HA heartbeat:
  `HA_URL`/`HA_TOKEN`/`HA_HEARTBEAT_MAX_AGE`/`HA_CONSECUTIVE`; B2 trend:
  `B2_TREND_METRIC`/`B2_TREND_WINDOW`/`B2_TREND_HORIZON_D`). A failed
  query/unreachable source makes that monitor `down` with an explanatory msg — a broken
  exporter is surfaced, not silently green.

## Operator prerequisites
1. Add the twenty-six push tokens to `secrets.yml` (`sops ansible/vars/secrets.yml`). **They must
   be exactly 32 alphanumeric chars** (Kuma rejects others, e.g. `openssl rand -hex 16`);
   AutoKuma silently refuses to create the monitor otherwise (`Invalid push_token`).
2. For the n8n monitor: add `n8n_api_key` to `secrets.yml`. Mint it in the n8n UI
   (**Settings → n8n API**), scoped to read **Workflow** + **Execution** permissions.
3. For the Arr Queue Warnings monitor: `sonarr_api_key`/`radarr_api_key` already exist in
   `secrets.yml` (recyclarr/janitorr/homepage reference them too — get the plaintext from
   `docker exec sonarr cat /config/config.xml` / `docker exec radarr cat /config/config.xml`
   if you need to re-derive them). monitor-bridge joined the `media` network for this on
   2026-07-02 (its `containers_list` entry in `ansible/inventory/host_vars/daniel-server.yml`);
   if `media` is ever dropped from that entry, the check pages `down` every cycle
   (unresolvable host) rather than failing silent.
4. Notifications attach **automatically** — the `kuma()` macro tags every monitor with
   `notification_name_list=["{{ kuma_notification_id }}"]`, linking it to the AutoKuma-managed
   Discord notification defined on the `uptime-kuma` container. No per-monitor UI clicking.

## Editing & testing
- Compose: `templates/docker-compose.yml.j2` · Logic: `files/check.py`
- Unit tests (parsing + every check's decision logic):
  `uv run pytest ansible/roles/containers/monitor-bridge/files`.
  Also run automatically by the `pytest` prek hook (`prek run pytest --all-files`).
- Smoke test one pass: `docker exec monitor-bridge python /app/check.py --once`
- Deploy: `uv run ansible-playbook ansible/deploy.yml --tags "monitor-bridge"`
