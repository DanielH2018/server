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
  `INTERVAL` (300 s) it runs **thirty-eight checks** and pushes `status=up|down&msg=…` to one Kuma push
  monitor each:
  - **Prometheus Reachable** (a trivial `vector(1)` instant query — the root-cause GATE for the
    prom-dependent checks. Evaluated FIRST each cycle: when Prometheus is unreachable, the nine
    prom-dependent checks (disk/cert/memory/restarts/oom/cpu/targets/traefik5xx/b2_trend) are
    **suppressed** — pushed `up` with a "skipped — Prometheus unreachable" msg so their push-monitor
    heartbeats stay alive — and only THIS monitor pages. Without the gate one Prometheus outage
    fired all nine at once: one root cause, a nine-monitor alert storm. A single scrape target down
    (Prometheus up, one exporter gone) still surfaces separately on Scrape Targets. The
    `PROM_DEPENDENT` set is guarded by a test against the live `CHECKS` so it can't drift.)
  - **Backup Freshness** (Kopia `/api/v1/sources` last-snapshot age + errorCount, **plus a
    snapshot-shrink guard**: once age/errorCount are clean it pulls the source's snapshot HISTORY
    (`/api/v1/snapshots`) and `down`s if the latest snapshot's total files or bytes dropped >
    `BACKUP_SIZE_DROP_PCT` (20%) below the trailing-snapshot MEDIAN. A kopiaignore over-match or a
    vanished bind mount silently drops a service from the config-only backup while the snapshot still
    completes fresh + 0-error — the recurring bare-`data/` incident class (kopiaignore.j2) that nothing
    else catches: the B2 monitors track billable-bytes GROWTH, a shrink reads greener, and the monthly
    restore drill exercises only one rotated service. Uses `summary.{files,size}` — the whole-tree
    totals — NOT `stats.fileCount`, which counts only newly-hashed non-cached files and swings by
    design. The median absorbs the routine one-off drops from exclusion tuning, so only a sharp
    anomalous drop pages. Stateless (sourced from Kopia's own history). Pure `backup_size_regression()`
    is unit-tested; `BACKUP_SIZE_DROP_PCT`/`BACKUP_SIZE_MIN_HISTORY` tune it. **Plus a per-service
    presence guard** (once size is clean): fetches the top-level directory listing of the latest +
    trailing snapshots (`/api/v1/objects/<rootID>`, `entries[].summ.files`) and `down`s if a service
    dir present with >0 files in ALL of the prior `BACKUP_SIZE_MIN_HISTORY` snapshots vanished (or
    dropped to 0 files) from the latest — the residual the aggregate size floor can't reach once a
    service is small (portainer is ~0.05% of the tree, far under 20%), and portainer is NOT in the
    restore-drill rotation. A newly-added service isn't in the priors (no false page on an add), and
    an intentional removal stops being "expected" after one cycle. Pure `backup_presence_regression()`
    is unit-tested.)
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
  - **Prowlarr Indexers** (Prowlarr's `/api/v1/indexerstatus` + `/api/v1/indexer` over `media`,
    `X-Api-Key`: `down` only when an indexer has been failing ≥ `PROWLARR_INDEXER_MIN_DOWN_MIN`
    (1 week = 10080 min — only a genuinely long outage pages; short flaps are noise) measured from
    Prowlarr's own `initialFailure` — the age-based, per-indexer SUSTAINED
    signal Prowlarr's binary in-app health notification can't express (it's warnings-on-every-flap
    or all-indexers-down-only). Suppresses the transient tracker flaps that self-clear inside
    Prowlarr's ~5-15 min backoff. Age-based (not consecutive-cycle) so it survives a bridge
    redeploy mid-outage. Empty `PROWLARR_API_KEY` = disabled (stays up); a null/unparseable
    `initialFailure` is skipped, an unreachable Prowlarr surfaces as `down` via `_evaluate` (the
    `check_arr_queue` convention — no grace). Pairs with Prowlarr set to
    `includeHealthWarnings=false` (keeps `onHealthIssue` = the instant all-down red backstop).
    `PROWLARR_INDEXER_IGNORE` (comma-separated names, case-insensitive) drops chronically-flaky
    public trackers from the offender list — set to `The Pirate Bay` after its apibay.org backend
    503'd/timed-out for hours and flapped this monitor up/down on 2026-07-05 (the other 7 indexers
    cover the same searches; the all-down onHealthIssue is still the backstop). Pure
    `indexers_down()` is unit-tested. Spec: `docs/superpowers/specs/2026-07-04-prowlarr-indexer-watchdog-design.md`.)
  - **GitOps Deploy — Alive** (reads `/gitops-state/last_run`, a bind-mounted host timestamp the
    `gitops_deploy` deployer rewrites each non-crashing tick; `down` once it's older than
    `GITOPS_MAX_AGE_MIN` — i.e. the deployer stalled / host down. The deployer no longer pushes
    to Kuma itself — see [[the gitops_deploy CLAUDE.md]])
  - **GitOps Deploy — Status** (reads `/gitops-state/hold_sha`; `down` while a rolled-back commit
    is held pending the operator reverting the offending PR — self-heals when the hold clears)
  - **Backup Restore Drill** (reads `/restore-drill/state.json`, written monthly by the kopia
    role's `kopia-restore-drill.sh` host cron — `down` on a failed drill, >35 d staleness, or a
    missing/corrupt state file. Same state-file pattern as the GitOps monitors.)
  - **WG Pi Peer Backup** (reads `/pi-peers/state.json`, written daily by the **wg-easy** role's
    daniel-server `wg-easy-pull-pi-peers.sh` host cron — `down` on a FAILED pull (Pi unreachable /
    SSH-sudo break / file-count floor tripped), >2.5 d staleness, or a missing/corrupt state file.
    The pull rsyncs the Pi's un-rebuildable WireGuard peer keys into Kopia scope; it uses **no
    `--delete`**, so a silently-failing pull leaves the last-good copy in place and the nightly
    snapshot still succeeds — **Backup Freshness stays green while the peers go stale**. This is the
    dedicated watchdog for that gap (added 2026-07-05 — it was the one backup cron with no monitor).
    Same state-file idiom as Backup Verify / Restore Drill; pure `pi_peers()` is unit-tested.)
  - **CrowdSec Home Allowlist** (reads `/home-allowlist/state.json`, written **every 5 min** by the
    **traefik** role's `crowdsec-update-home-allowlist.sh` host cron — `down` on a FAILED run (ipify
    unreachable / malformed IP / cscli error) or >30 min staleness (cron broken / never ran), a
    missing/corrupt state file included. That cron keeps the operator's current home public IP in
    CrowdSec's `home-ips` allowlist so browsing the public path from home doesn't trip the WAF; it
    writes state on EVERY run incl. the common IP-unchanged fast path, so a healthy no-op keeps the
    monitor green and only a real failure/stall pages. It was the last self-`logger`ing cron with no
    watchdog — the twin of the WG Pi Peer Backup gap (2026-07-05). Same state-file idiom; pure
    `home_allowlist()` is unit-tested. `HOME_ALLOWLIST_MAX_AGE_MIN` tunes the staleness window.)
  - **DOCKER-USER Origin Lock** (reads `/docker-user/state.json`, written **every 15 min** by the
    **traefik** role's `docker-user-verify.sh` root cron — `down` on a failed live-chain assert or
    >45 min staleness (3 missed runs), a missing/corrupt state file included. The Cloudflare-only-
    origin lock is applied by two systemd units (`docker-user-seed.service` before Docker on boot +
    `docker-user-rules.service` re-asserting after a docker restart) that write NO state, so this is
    the only signal it's ACTUALLY applied: the cron re-reads the live `iptables DOCKER-USER` chain and
    asserts the terminal DROP for :80/:443 plus a RETURN allow are present. A chain flushed after boot
    (docker network reload, manual `iptables -F`, a Docker upgrade seeding a preempting RETURN) leaves
    80/443 reachable direct — a Cloudflare/CrowdSec bypass — invisibly otherwise. Same state-file idiom;
    pure `docker_user()` is unit-tested. `DOCKER_USER_MAX_AGE_MIN` tunes the staleness window.)
  - **Cloudflare IP Drift** (reads `/cloudflare-drift/state.json`, written **weekly** by the
    **traefik** role's `cloudflare-ip-drift.sh` cron — `down` on drift, a failed fetch, or >10 d
    staleness (one missed weekly run), a missing/corrupt state file included. The hardcoded
    `cloudflare_ips` allowlist (`group_vars/all.yml`) gates BOTH Traefik's `forwardedHeaders.trustedIPs`
    AND the DOCKER-USER origin-lock DROP (`docker-user-rules.sh`), so if Cloudflare adds a range a
    client arriving on it is silently DROPped at the edge firewall with NO log trail (reads like a
    random regional outage). The cron diffs the baked-in list against Cloudflare's published
    `ips-v4`/`ips-v6` and alerts on any mismatch — it never auto-applies a fetched list to a firewall.
    Cloudflare changes these ~once per several years, so it's a low-frequency safety net. Same
    state-file idiom; pure `cloudflare_drift()` is unit-tested. `CLOUDFLARE_DRIFT_MAX_AGE_D` tunes it.)
  - **CrowdSec AppSec** (reads `/crowdsec-appsec/state.json`, written **every 15 min** by the
    **traefik** role's `appsec-verify.sh` root cron — `down` on a failed live assert or >45 min
    staleness (3 missed 15-min runs), a missing/corrupt state file included. The CrowdSec AppSec
    inline L7 WAF (2026-07-14) fails **OPEN** (`crowdsecAppsecUnreachableBlock: false`), so if its
    engine stops loading the appsec config/rulesets — a bad `cscli collections upgrade`, a hub rename
    dropping `appsec-virtual-patching`/`appsec-generic-rules`, a broken `appsec-default` config — the
    edge silently degrades to ban-list-only while the crowdsec container stays up + `cscli lapi status`
    healthy, which Scrape Targets can't catch (it only sees a TOTAL crowdsec death). The cron asserts
    the live agent has ≥1 enabled `cscli appsec-configs` **and** ≥1 inband `cscli appsec-rules` loaded
    — both traffic-independent (the `cs_appsec_*` request counters read zero on a quiet homelab and
    would false-page). It's the one fail-open edge control that lacked the repo's state-file→monitor
    idiom. Same idiom as `docker_user`; pure `appsec()` is unit-tested. `APPSEC_MAX_AGE_MIN` tunes it.)
  - **Backup Verify** (reads `/verify/state.json`, written weekly by the kopia role's
    `kopia-verify.sh` host cron — `down` on a FAILED `kopia snapshot verify` (detected
    bit-rot / an unreadable blob), >10 d staleness, or a missing/corrupt state file. The
    verify tier of the three-tier backup assurance: it proves stored blobs are READABLE
    across ALL snapshots, where the restore drill proves ONE service's tree restores. The
    script captures the verify's own exit code — the old `... | logger` cron made cron see
    logger's always-zero exit and silently swallowed a non-zero verify.)
  - **Backup Content Verify** (reads `/content-verify/state.json`, written **quarterly** (1st of
    Mar/Jun/Sep/Dec) by the kopia role's `content-verify.sh` host cron — `down` on a FAILED
    `kopia snapshot verify --verify-files-percent=25`, >100 d staleness (the ~92 d max inter-quarter
    gap + margin; a missed quarter pages), or a missing/corrupt state file. The DEEP tier below the
    weekly 1% Backup Verify: re-reading a quarter of every snapshot's content catches a mis-purge /
    blob-accounting bug the 1% sample keeps missing (belt-and-suspenders — B2 already read-verifies
    its own SHA1). Seeded on first deploy so it stays green until the first quarterly run. Same
    state-file idiom; pure `content_verify()` is unit-tested. `CONTENT_VERIFY_MAX_AGE_D` tunes it.)
  - **Disk Autoprune** (reads `/autofix-disk/state.json`, written hourly by the **autofix-bridge**
    role's disk-prune host cron — `down` on a FAILED prune command (docker image/builder/container
    prune erroring), >3 h staleness (cron broken / never ran), or a missing/corrupt state file. The
    cron conservatively reclaims dangling images/build cache/stopped containers (never `-a`, never
    volumes) when `/` used% crosses a threshold, keeping Root Disk from ever needing a manual prune
    as image churn grows. A disk still full of real data after a clean prune is Root Disk's alert,
    not this one — single-purpose monitors, no double-paging. Same state-file idiom as Backup
    Verify / WG Pi Peer Backup; pure `disk_prune()` is unit-tested.)
  - **Backup Maintenance** (reads `/maintenance/state.json`, written daily by the kopia role's
    `kopia-maintenance` host cron from `kopia maintenance info --json` — `down` on a
    disabled/overdue/failed full-maintenance cycle, >2.5 d staleness, or a missing/corrupt state
    file. Full maintenance GCs expired blobs from B2, so a stall is the upstream CAUSE that the
    B2 Storage Usage / B2 Usage Trend checks only catch weeks later as a downstream symptom (and
    B2 headroom is thin). Same state-file idiom as Backup Verify / B2 Storage Usage; pure
    `maintenance()` + `check_maintenance()` are unit-tested.)
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
  - **SMART Data / Health** (scrutiny web API `/api/summary` over `monitoring`: every
    non-archived device must have a `collector_date` within 26 h **AND a passing `device_status`**
    (0 = SMART self-assessment + Scrutiny's attribute thresholds both OK; non-zero decodes to
    "SMART self-assessment FAILED" / "attribute threshold breached"). Freshness catches a
    silently-dead collector (cron-as-PID1, no usable healthcheck → only shows as aging data); the
    status check catches a drive that goes SMART-FAILED / breaches a threshold while STILL reporting
    fresh data — nothing else alerts on that (Scrutiny writes to InfluxDB not Prometheus, and its
    own Shoutrrr notifier is unconfigured, so this bridge check is the only drive-failure alert
    path). Also `down` when scrutiny lists no devices at all. `SCRUTINY_TEMP_MAX` (°C, default
    0 = off) adds an optional early-warning temperature ceiling on top. Pure `scrutiny_freshness()`
    + `scrutiny_health()` are unit-tested.)
  - **UPS Battery Health** (the APC UPS's charge % + estimated runtime + the replace-battery
    self-test verdict, via HA's Prometheus-scraped sensors over `monitoring` — the UPS is on
    NUT/peanut and HA's prometheus integration exports it). `down` on a low battery RUNWAY: charge <
    `UPS_CHARGE_MIN_PCT` (50, a deep discharge while on battery) OR estimated runtime <
    `UPS_RUNTIME_MIN_S` (300 s — an aged battery whose full-charge runway has decayed, OR a discharge
    nearing shutdown) OR the UPS's own **replace-battery** verdict (`UPS_REPLACE_QUERY`, an HA
    template `binary_sensor.apc_ups_replace_battery` over the NUT `RB` flag — the earliest signal, it
    can trip while charge/runtime still read fine; before this the RB verdict reached NEITHER channel,
    2026-07-14 review). Two defer paths avoid double-paging a source outage another monitor owns:
    ALL arms absent → HA's whole scrape is down (Scrape Targets' page); **both NUT numeric arms
    (charge, runtime) absent while the replace arm is still present** → the NUT server/integration
    dropped (HA drops the unavailable numeric sensors, but the replace-battery template FLOORS to 0 so
    it stays present — it CANNOT reach the all-absent branch), which the `nut` container healthcheck
    owns. A **partial** absence that is neither (a single numeric arm gone, or replace gone while the
    numerics report) is a specific entity rename → pages through the streak rather than silently
    monitoring the survivor. The only
    pre-existing UPS alert is an HA automation → **mobile** push (a separate channel from this
    Kuma→Discord brain) and nothing trended the battery, so a slowly-degrading battery was invisible
    until an outage collapsed it — this is the health/runway signal + the Discord escalation path.
    **Prom-dependent** (queries HA's scrape). `UPS_CONSECUTIVE` (2, like
    `HA_CONSECUTIVE`) rides out a one-cycle dip from a transient load spike (or an HA-restart blip
    that briefly drops one arm). Queries are env-driven
    (`UPS_CHARGE_QUERY`/`UPS_RUNTIME_QUERY`/`UPS_REPLACE_QUERY`, all empty = disabled) so a UPS/entity
    rename needs no code edit. Pure `ups_health()` is unit-tested.)
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
  - **Loki Reachable** (a fixed `/loki/api/v1/labels` probe — the root-cause GATE for the
    Loki-querying checks, the peer of Prometheus Reachable. Evaluated each cycle: when Loki is
    unreachable the three `LOKI_DEPENDENT` checks (loki_ingestion/recyclarr/janitorr) are
    **suppressed** — pushed `up` with a "skipped — Loki unreachable" msg — and only THIS monitor
    pages. Without it one Loki outage fired all three at once. Loki being UP but promtail not
    shipping is a different signal Loki Log Ingestion still surfaces. `LOKI_DEPENDENT` is guarded by
    a test against the live `CHECKS` so it can't drift.)
  - **Loki Log Ingestion** (two-arm LogQL freshness against `loki:3100` over `monitoring`, `down`
    if EITHER arm is silent — a silently-dead promtail→Loki pipeline (docker-proxy break,
    positions-file corruption, relabel regression) that Loki's `/ready` Kuma probe stays green
    through. **Arm 1 — file-tail union** `sum(count_over_time({job=~"authlog|syslog|traefik"}[3h]))`:
    counts the file-tailed streams — not one, so if promtail dies they ALL fall silent together
    while syslog's routine volume keeps a quiet night alive (no single low-volume file trips it) —
    over a TOLERANT window. It deliberately EXCLUDES the docker_sd stream: promtail stamps that
    stream `job: docker` (so a bare `{job=~".+"}` would swallow it), and it dwarfs the file-tail
    streams (~all 44 containers' stdout), so including it let a healthy container stream mask a
    total file-tail outage — arm 1 could then only reach zero if promtail was *totally* dead, which
    arm 2 already catches (the 2026-07-07 blind-spot review re-scoped it to file-tail-only). The
    window is wider than arm 2's because file-tail volume is low and dips overnight (a lone
    `{job="syslog"}` over 10m false-paged 2026-06-23 — a 15m35s idle gap was observed). **Arm 2 —
    docker stream** `sum(count_over_time({container=~".+"}[30m]))` (`LOKI_DOCKER_STREAM`): the
    docker_sd stream carries a `container` label, no `job`, so it's exactly the one arm 1 excludes;
    a docker_sd-specific break (docker-proxy down, the docker relabel regressing) silences every
    container log while the file-tail streams keep flowing, and a tight window catches a total
    promtail death fast. Selectors/windows tunable via
    `LOKI_STREAM`/`LOKI_FILETAIL_WINDOW`/`LOKI_DOCKER_STREAM`/`LOKI_WINDOW`. Pure
    `loki_ingestion_fresh()` + `loki_count()` are unit-tested. A freshness watchdog in the same
    idiom as the SMART/restore-drill checks.)
  - **Promtail Dropped Entries** (`sum(increase(promtail_dropped_entries_total{reason="ingester_error"}[1h]))`
    from Prometheus, which already scrapes `promtail:9080` — `down` above `PROMTAIL_DROPPED_MAX` (1000)
    entries dropped in the window. Where **Loki Log Ingestion** catches TOTAL silence, this surfaces
    PARTIAL loss: entries promtail gave up shipping when Loki rejected them (`ingester_error` = ingester
    unavailable / rate-limited / out-of-order). The threshold keeps a transient Loki restart's handful
    of drops from paging; `increase()` handles counter resets; no series → 0 → up (a dead promtail
    scrape is Scrape Targets' page). **Prom-dependent** — suppressed under the Prometheus gate. Pure
    `promtail_dropped()` is unit-tested; `PROMTAIL_DROPPED_WINDOW`/`PROMTAIL_DROPPED_MAX` tune it.)
  - **Discord Delivery** (GET-verifies **all five** Discord notification webhooks: Kuma's own
    `monitor_discord_webhook_url` — the one Kuma POSTs every alert to — CrowdSec's
    `crowdsec_discord_webhook_url`, which CrowdSec POSTs ban alerts to *directly* (not via Kuma),
    the `gitops_deploy_discord_webhook`, which delivers the gitops-deploy rollback alert AND every
    `renovate_notify` digest (its Renovate Notifier — Alive marker greens even when the POST fails —
    no Kuma backstop), and `arr_discord_webhook_url`, which Sonarr/Radarr/Prowlarr POST their own
    onHealthIssue alerts to via in-app Discord Connect (config lives in the app DBs, not templated —
    the Arr Queue check covers stuck downloads, NOT indexer/download-client health), and
    `healthchecks_discord_webhook_url`, the healthchecks.io app's own check-down/up webhook (a
    "webhook" channel in hc.sqlite, not templated — a redundant secondary to its SMTP path). The
    latter four have NO Kuma backstop of their own. `down` if ANY is invalid,
    naming which; each empty URL is skipped. A
    rotated/revoked/deleted webhook makes those alerts silently fail to deliver while every monitor
    stays GREEN in the Kuma UI; this is the alert chain's delivery hop that NO other monitor — not
    even the off-box UptimeRobot host dead-man — exercises. A webhook GET returns Discord's metadata (200) when valid
    and 404 once gone, and never posts a message (no channel spam) — unlike a test POST. **It also
    probes the alert-EMAIL 2nd channel** (`email_backstop`): the Gmail SMTP notification attached
    (only) to THIS monitor as the escape hatch when the Discord webhook is dead — a throttled SMTP
    login with the same creds Kuma uses (`SMTP_USER`/`SMTP_PASSWORD`), so a silently-revoked
    app-password flips this monitor down and still pages via the working Discord channel. Throttled to
    `EMAIL_PROBE_INTERVAL_S` (6h — Gmail flags frequent AUTHs): a success is cached, a failure
    re-probes every cycle. Empty `SMTP_PASSWORD` = that probe disabled. Both webhooks + SMTP reach the
    PUBLIC internet, so `DISCORD_CONSECUTIVE` (2) adds the same streak
    hysteresis as the HA heartbeat: a single transient non-200/network blip pushes `up` with a
    "down streak n/N" msg and only the 2nd straight failure pages. Empty `DISCORD_WEBHOOK_URL` =
    disabled (stays up), like `N8N_API_KEY`. Pure `discord_webhook_ok()`, `email_backstop()`'s
    throttle + the streak wrapper are unit-tested. NOTE: it verifies the webhook is DELIVERABLE
    (catches a rotated/revoked URL); it does NOT assert Kuma still has the notification *attached* to
    each monitor — AutoKuma re-applies that on every deploy via the `kuma()` macro's `notification_name_list`.)
  - **Recyclarr Sync** (instant LogQL counts of supercronic's `job succeeded` / `job failed` lines
    for `{container="recyclarr"}` over `RECYCLARR_WINDOW` (26 h = one `@daily` run + slack), reached
    at `loki:3100`: recyclarr runs `recyclarr sync` under supercronic and `/cron.sh` ends with the
    sync, so its exit code propagates — supercronic logs `job succeeded` on exit 0, `job failed` on
    non-zero. `down` on any `job failed` (a sync errored) OR zero `job succeeded` (the scheduler
    stalled / every run failing). The container healthcheck only watches supercronic, so an ERRORING
    sync was previously invisible — the silent 2026-06-10 v8-major breakage that failed every nightly
    sync with the healthcheck staying green. Pure `recyclarr_sync_ok()` is unit-tested; selector/window
    tunable via `RECYCLARR_LOKI_SELECTOR`/`RECYCLARR_WINDOW`. Same Loki-query path as Loki Log Ingestion.)
  - **Janitorr Errors** (counts janitorr scheduled-task ERROR lines in Loki over the post-startup
    window — janitorr's healthcheck only proves the JVM is alive, so an internal cleanup error
    (failed delete, bad config, a bug) logs ERROR and is otherwise invisible, and **janitorr deletes
    real media**. The one benign, recurring ERROR is the documented post-boot race — an `@Scheduled`
    cleanup fires before jellyfin/sonarr/radarr finish loading → `FeignException` 503, self-heals
    next cycle (janitorr's CLAUDE.md). That ERROR line is generic ("Unexpected error occurred in
    scheduled task"), identical to a real failure, with the exception type on a separate Loki line —
    so it can't be filtered by content. We discriminate by **TIME** via the container's Prometheus
    uptime (`time() - max(container_start_time_seconds{name="janitorr"})`): within
    `JANITORR_STARTUP_GRACE_S` (600 s) of startup we don't count, and past it we count only over the
    post-startup slice (`min(JANITORR_WINDOW=12h, uptime − grace)`) so the boot race can never be
    in-window. Absent uptime metric (janitorr stopped) → up (Container Restarts/Scrape Targets owns
    that). **Prom-dependent** (uptime, in `PROM_DEPENDENT`) AND **Loki-dependent** (count, in
    `LOKI_DEPENDENT`) — suppressed under either gate. Pure `janitorr_errors_ok()` is unit-tested.)
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
- **Startup/redeploy grace for the reach-out checks (`STARTUP_GRACE`, 2026-07-12):** the six
  checks that poll a live app dependency with **no reachability gate and no per-check hysteresis**
  — **Backup Freshness** (kopia), **n8n Prod Workflows** (n8n), **Arr Queue Warnings**
  (sonarr/radarr), **Prowlarr Indexers** (prowlarr) and **SMART Data / Health** (scrutiny) (both
  added 2026-07-14), **Pi Pressure** (the Pi glances) — get a consecutive-down grace applied in
  `run_once` (peer mechanism to `PROM_DEPENDENT`/`LOKI_DEPENDENT`, but a *hysteresis* not a
  *suppression*). Cause: the bridge's first cycle after the **weekly Sunday 07:30 host reboot**
  runs before those heavy apps finish starting, so each un-graced `max_retries=0` monitor flipped
  DOWN on that one transient cycle (`<name> check error: Connection refused` / n8n `HTTP 404` while
  its API routes warmed up) and paged, then recovered next cycle — a weekly DOWN/UP flap. Which
  subset actually paged varied week to week (a startup race: some weeks the DOWN push itself failed
  because uptime-kuma wasn't ready yet). `apply_startup_grace()` holds each `up` for the first
  `GRACE_CYCLES`-1 (default 2−1 = 1) consecutive down cycles — the same "down streak n/N" idiom as
  `check_ha_heartbeat`'s `HA_CONSECUTIVE` — so only the `GRACE_CYCLES`'th straight down pages a
  genuinely-dead dependency (~one extra INTERVAL later), and one `ok` resets the streak. The set is
  **disjoint from every run_once skip set** (so a graced check reaches the eval path each cycle and
  its streak advances) — both invariants guarded by a test against `CHECKS`. `GRACE_CYCLES` is
  env-tunable. Pure `apply_startup_grace()` is unit-tested.
- **Container healthcheck (2026-06-10):** check.py touches `/tmp/heartbeat` (tmpfs) after
  every cycle; the compose healthcheck goes unhealthy when the mtime exceeds ~3×INTERVAL,
  so autoheal restarts a *hung* loop (death alone already exits the container). Kuma push
  silence remains the alerting path; the healthcheck adds auto-recovery.
- Push tokens (`monitor_bridge_{kopia,disk,cert,mem,restarts,oom,cpu,targets,traefik,prometheus,n8n,arr_queue,prowlarr_indexers,gitops_alive,gitops_status,scrutiny,ups,pi,pi_peers,home_allowlist,docker_user,cloudflare_drift,appsec,b2,b2_trend,ha,renovate_alive,loki,loki_reachable,promtail_dropped,verify,content_verify,disk_prune,maintenance,discord,recyclarr,janitorr}_push_token` + `kopia_restore_drill_push_token`)
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
- Likewise, the **WG Pi Peer Backup** monitor bind-mounts `/var/lib/wg-easy-pi-peers:/pi-peers:ro`
  (written by the `wg-easy` role's daniel-server pull cron). The `wg-easy` role creates that dir
  sys_user-owned (and its file task re-chowns it if Docker got there first), so deploy `wg-easy`
  before `monitor-bridge` on a fresh host. Kopia's own state dirs (`/var/lib/kopia-*`) are created
  by the kopia role, which `monitor-bridge` already depends on.
- The **CrowdSec Home Allowlist** monitor bind-mounts `/var/lib/crowdsec-home-allowlist:/home-allowlist:ro`
  (written by the `traefik` role's every-5-min `crowdsec-update-home-allowlist.sh` cron). The `traefik`
  role creates that dir sys_user-owned, and traefik deploys first (everything depends on it), so the
  ordering is naturally satisfied. The **Cloudflare IP Drift** monitor's
  `/var/lib/cloudflare-ip-drift:/cloudflare-drift:ro` mount (written weekly by the same role's
  `cloudflare-ip-drift.sh`, seeded once on deploy) is created the same way with the same ordering.
  The **CrowdSec AppSec** monitor's `/var/lib/crowdsec-appsec:/crowdsec-appsec:ro` mount (written
  every 15 min by the same role's `appsec-verify.sh`, seeded once on deploy) follows the same
  ordering — but that dir is **root-owned** (the verify cron runs as root via `docker exec`, like
  `docker-user-verify.sh`), and the script `chmod 0644`s its state file for the non-root reader.
- The **Disk Autoprune** monitor bind-mounts `/var/lib/autofix-disk-prune:/autofix-disk:ro`
  (written by the `autofix-bridge` role's hourly disk-prune cron). That role creates the dir
  sys_user-owned, so on a fresh host deploy `autofix-bridge` before `monitor-bridge` (else Docker
  auto-creates the mount source root-owned and the non-root container can't read it).
- Thresholds are env-tunable in the compose template (`GRACE_CYCLES` (startup/redeploy grace),
  `BACKUP_MAX_AGE_H`, `DISK_MAX_PCT`,
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
1. Add the thirty-eight push tokens to `secrets.yml` (`sops ansible/vars/secrets.yml`). **They must
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
