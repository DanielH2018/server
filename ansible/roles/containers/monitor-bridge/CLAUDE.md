# monitor-bridge — the host-state REMNANT (the metric/API checks live in the cluster twin)

> **SPLIT at the Phase F drain (2026-08-14).** Every metric/API check moved to
> `roles/k8s/monitor-bridge` — the SAME `files/check.py`, staged from this role, split by
> env: the twin sets `CHECKS_SKIP`, this remnant sets `CHECKS_ONLY` with exactly the
> checks that read THIS host's state files — three since the 2026-08-14 host flips:
> gitops_alive, gitops_status, disk_prune (their producers are host crons here; each
> retires with its producer). pi_peers and renovate_alive DISSOLVED at those flips —
> their successors (the k8s/pi-peer-backup CronJob; renovate-notify's ExecStartPost on
> daniel-box) push the same Kuma monitors directly. check.py refuses a filter naming an
> unknown check or a gated check without its gate, and
> `test_checks_and_compose_push_env_agree` asserts the two deployments' env partitions
> the token set. Most of the per-check documentation below predates the split — checks
> it describes outside the remnant's three run (and keep their env) in the twin, except
> the two dissolved ones, whose sections are history.

A tiny sidecar that turns host-cron state files into Uptime Kuma **push** monitors, so
threshold problems actually page. See repo-root `CLAUDE.md`. (The kopia backup checks
retired with kopia on 2026-08-10 — the backup plane is Longhorn;
`backup-consolidation-longhorn.md`.)

## At a glance
- **Image:** `python:3.14-alpine` (stdlib only — no build, no extra deps)
- **Host:** daniel-server · **No web UI**, no Authelia
- **Networks:** `monitoring` (deploy-ordering artifact only; Prometheus itself retired
  2026-08-12) + `apps` + `media` — the kopia net retired with kopia; monitor-bridge reaches the
  cluster prometheus via PROMETHEUS_URL (`https://prometheus-k8s.local.<domain>`). Kuma, n8n and
  the *arrs are all reached over the LAN (`-k8s` names) since their cluster moves, so the
  remaining memberships are a Phase E diet candidate.
- **Depends on:** prometheus (`meta/deps.yml`)
- **Config in:** `ansible/inventory/host_vars/daniel-server.yml` → `containers_list`

## Notable
- `files/check.py` is a **static** Python loop (config via env vars, no Jinja). Every
  `INTERVAL` (300 s) it runs **30 monitors** (26 in `CHECKS` plus the four reachability
  gates: prometheus, loki_reachable, b2_reachable, cluster_prometheus) and pushes
  `status=up|down&msg=…` to one Kuma push monitor each:
  - **Prometheus Reachable** (a trivial `vector(1)` instant query — the root-cause GATE for the
    prom-dependent checks. Evaluated FIRST each cycle: when Prometheus is unreachable, the ten
    prom-dependent checks (disk/cert/memory/restarts/oom/cpu/targets/traefik5xx/ups/
    promtail_dropped) are
    **suppressed** — pushed `up` with a "skipped — Prometheus unreachable" msg so their push-monitor
    heartbeats stay alive — and only THIS monitor pages. Without the gate one Prometheus outage
    fires all ten at once: one root cause, a ten-monitor alert storm. A single scrape target down
    (Prometheus up, one exporter gone) still surfaces separately on Scrape Targets. The
    `PROM_DEPENDENT` set is guarded by a test against the live `CHECKS` so it can't drift.)
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
  - **Traefik Latency** (share of requests slower than a histogram BUCKET BOUNDARY, per service,
    behind the same `TRAEFIK_MIN_RPS` floor — the gap the 5xx check can't close, since a degraded
    backend still answers 200. **Not `histogram_quantile`**: Traefik's default buckets are
    0.1/0.3/1.2/5.0/+Inf, so a quantile landing between 1.2s and 5.0s is interpolated across an
    empty 3.8s-wide gap, and the old 3s threshold sat inside it. Every firing was that arithmetic —
    homepage@docker read 4.058s on 2026-08-06 where the Traefik access log for the same window
    showed a real p95 of 1.576s. Bucket counts are exact, so ">`TRAEFIK_SLOW_PCT` (5%) of requests
    over `TRAEFIK_SLOW_BUCKET` (5.0s)" IS "p95 over 5.0s" without interpolation. Keep
    `TRAEFIK_SLOW_BUCKET` on a boundary Traefik actually emits: an `le=` matching no series is
    reported as a config fault rather than read as 0 requests under the boundary, which would page
    every service at once.)
  - **n8n Prod Workflows** (n8n public API: per-*active*-workflow **consecutive-failure
    streak**. n8n doesn't save successful executions (`EXECUTIONS_DATA_SAVE_ON_SUCCESS=none`, to
    bound `database.sqlite` + its B2 backup churn — 2026-07-03), so "consecutive" can't be read
    from one snapshot: `check.py` accumulates the streak ACROSS cycles, deduped by execution id
    so a single lingering failure isn't recounted, and resets a workflow's streak once its latest
    error ages past `N8N_FAIL_WINDOW` (recovered/idle). `down` when any workflow fails
    `N8N_CONSECUTIVE_MAX` (3) times in a row, OR when `N8N_SYSTEMIC_MAX` (2)+ workflows are each
    failing `N8N_SYSTEMIC_STREAK` (2)+ times — the n8n-wide catch that pages promptly as ONE
    alert instead of waiting for each to hit the consecutive threshold (and instead of a
    per-workflow flood). "Prod" = active. Empty `N8N_API_KEY` = disabled (stays up); an
    unreachable API surfaces as `down`. Reached at `n8n:5678` over `apps`, bypassing Authelia via
    the `X-N8N-API-KEY` header. Streak state is module-global (resets on a bridge restart, ridden
    out by the STARTUP_GRACE hysteresis). Pure `n8n_update_streaks()`/`n8n_verdict()` are
    unit-tested.)
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
  - **GitOps Deploy — Status** (reads `/gitops-state/hold_sha`, `/gitops-state/diverged_sha`
    **and `/gitops-state/behind_since`**;
    `down` while a rolled-back commit is held pending the operator reverting the offending PR —
    self-heals when the hold clears — OR while local and origin have **diverged** so the deployer
    can't fast-forward and silently noops forever while origin's new commits never deploy (both other
    GitOps signals stay green; the deployer records the diverged SHA each tick, 2026-07-15 review L3)
    — OR while the host has simply sat **behind origin** longer than `GITOPS_BEHIND_MAX_MIN` (360 =
    6 h). That last arm is the general case the other two are instances of, and it is the one that
    caught nothing: a deferred **broad** change never fast-forwards, so the host parks on an old tree
    with `last_run` still ticking and `is_diverged` false (origin is a strict descendant). daniel-server
    ran a 12-commit-old tree for hours that way on 2026-08-02, every GitOps signal green, until its
    un-deployed Pi-hole DNS records were noticed by hand. Age-gated, not presence-gated: a routine push
    is behind for one tick and the deployer's dirty-tree path is behind for a whole edit session by
    design, so only sustained behind-ness is a fault. hold/diverged are reported ahead of it — they
    name the cause where "behind" names the symptom. Pure `gitops_status()`/`_parse_behind()` are
    unit-tested; an unparseable marker reads as not-behind rather than paging forever on garbage.)
  - **WG Pi Peer Backup** (reads `/pi-peers/state.json`, written daily by the **wg-easy** role's
    daniel-server `wg-easy-pull-pi-peers.sh` host cron — `down` on a FAILED pull (Pi unreachable /
    SSH-sudo break / file-count floor tripped), >2.5 d staleness, or a missing/corrupt state file.
    The pull rsyncs the Pi's un-rebuildable WireGuard peer keys into backup scope; it uses **no
    `--delete`**, so a silently-failing pull leaves the last-good copy in place while the peers go
    stale. This is the dedicated watchdog for that gap (added 2026-07-05 — it was the one backup
    cron with no monitor). Same state-file idiom as DOCKER-USER Origin Lock / Cloudflare IP Drift;
    pure `pi_peers()` is unit-tested.)
  - **CrowdSec Home Allowlist** — RETIRED from this container at slice-6 B2 (2026-08-09). `cscli
    allowlists` is LAPI-machine-only, so the updater cron followed the LAPI into the cluster
    (`roles/k8s/crowdsec`) and pushes the Kuma monitor directly from daniel-box; there is no
    state file on this host to read, so the check, its `HOME_ALLOWLIST_*` env, its bind mount and
    its tests are gone. The monitor itself still exists — its AutoKuma label moved to the
    `uptime-kuma` role.
  - **Public origin lock & AppSec verifiers** — RETIRED at E7 (2026-08-13) with the docker-edge
    public 80/443 origin. The Cloudflare-only-origin (`docker-user-verify.sh` cron) and Cloudflare-IP-
    drift checks guarded the legacy Traefik@docker; the CrowdSec AppSec verifier has re-homed to
    daniel-box as a root cron pushing the same "CrowdSec AppSec" Kuma monitor directly from
    `roles/k8s/crowdsec/templates/crowdsec-appsec-verify.sh.j2`.
  - **Disk Autoprune** (reads `/autofix-disk/state.json`, written hourly by the **autofix-bridge**
    role's disk-prune host cron — `down` on a FAILED prune command (docker image/builder/container
    prune erroring), >3 h staleness (cron broken / never ran), or a missing/corrupt state file. The
    cron conservatively reclaims dangling images/build cache/stopped containers (never `-a`, never
    volumes) when `/` used% crosses a threshold, keeping Root Disk from ever needing a manual prune
    as image churn grows. A disk still full of real data after a clean prune is Root Disk's alert,
    not this one — single-purpose monitors, no double-paging. Same state-file idiom as
    WG Pi Peer Backup; pure `disk_prune()` is unit-tested.)
  - **Fake Remux Scan / Fake Remux Replace** — no longer bridge checks. The detector +
    reconciler crons moved to daniel-box with the media stack (2026-08-08, slice 4 B7c;
    `roles/setup/fake_remux`), and their Kuma pushes go directly from that host via
    `state_push.py` — same tokens, so the monitors and their history survived the move. The
    label declarations live on the uptime-kuma compose now. Nothing in `check.py` references
    fake-remux anymore.
  - **B2 Reachable** (authenticates against B2's native API (`b2_authorize_account`, Basic auth
    with `B2_PROBE_KEY_ID` + the file-mounted `B2_PROBE_APPLICATION_KEY_FILE`) — Longhorn's own
    B2-backed backups need this probe. Added after the 2026-08-02 transaction-cap incident
    (`docs/b2-transaction-cap-monitoring-gaps.md`): B2 caps **transactions** separately from
    storage bytes, and it used to gate five kopia-era state-file checks (Backup Verify, Backup
    Content Verify, Backup Maintenance, B2 Storage Usage, B2 Usage Trend) that read green for nine
    and a half hours during that incident because they reported their cron's LAST SUCCESSFUL RUN
    rather than current B2 health. Those checks were removed 2026-08-10 — kopia is retired, backup
    moved to Longhorn (`docs/k3s-migration/backup-consolidation-longhorn.md`) — leaving
    `B2_DEPENDENT` empty; kept as infrastructure for any future check that reads B2-backed state
    via a cron/state-file rather than querying B2 live. This check reports B2's own
    `transaction_cap_exceeded` error text directly. **Throttled**, unlike the other two gates: the
    probe runs at most once per `B2_PROBE_INTERVAL_S` (1800 s = 48 calls/day) and **both** outcomes
    are cached, because the fault being detected is a transaction cap and an every-cycle probe
    (288/day) — or `email_backstop`'s cache-successes-only idiom, which retries on failure — would
    spend the budget it's watching. The cached verdict is still pushed every cycle so the push
    monitor's heartbeat stays alive. Empty credentials = disabled (stays up). `B2_DEPENDENT` is
    guarded by tests against the live `CHECKS` and against `STARTUP_GRACE` so it can't drift.
    **Assumption, stated in the code because it can't be tested without a live breach:** that
    `b2_authorize_account` is itself subject to the cap — Backblaze's endpoint docs list
    403/`transaction_cap_exceeded` among its errors. If a future breach leaves this monitor green,
    point `B2_PROBE_URL` at a Class C call instead; it's a URL swap by design.)
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
    unreachable the `LOKI_DEPENDENT` check (loki_ingestion) is
    **suppressed** — pushed `up` with a "skipped — Loki unreachable" msg — and only THIS monitor
    pages. It was two until janitorr's watchdog moved to the cluster (2026-08-08); one Loki outage
    firing both at once is why the gate exists. Loki being UP but promtail not
    shipping is a different signal Loki Log Ingestion still surfaces. `LOKI_DEPENDENT` is guarded by
    a test against the live `CHECKS` so it can't drift.)
  - **Cluster Prometheus Reachable** (a `vector(1)` probe against the **k3s cluster's** Prometheus
    at `prometheus-k8s.local.<domain>` — a SECOND instance on daniel-box, not the one `PROM_URL`
    points at. Its own gate rather than an arm of `PROM_DEPENDENT`, because they are two instances
    on two hosts reached by two paths: the Docker Prometheus being up says nothing about whether
    the cluster one is, and a gate that isn't watching a check's real source reports confidence it
    doesn't have. Gates `CLUSTER_DEPENDENT`. Empty `CLUSTER_PROMETHEUS_URL` = disabled.)
  - **k3s Workload Health** (`kube_deployment_status_replicas_unavailable` from kube-state-metrics
    via the cluster Prometheus — the only monitor the seven routeless k8s workloads have, and the
    reason the metric exists at all: `registry`, both `cloudflare-ddns` copies, karakeep's
    `chrome`/`meilisearch`/`time-tagger`, and `n8n-runners`, which executes every workflow's code.
    Three expose only a ClusterIP (unreachable from daniel-server), four expose **no Service at
    all**, and none has an ingress route — so their health is a Kubernetes API property, not an
    HTTP one, and nothing here can probe them directly. **Fails closed on an absent series, and
    this is the whole point:** `unavailable > 0` returns an empty vector both when every workload
    is healthy AND when there are no series at all, so the check `count()`s the series FIRST and
    reports `UNKNOWN, not OK` when the count is missing or below `K8S_MIN_WORKLOADS` (5). Reading
    the healthy meaning onto both is how a monitor goes green while blind — the shape of the B2
    transaction cap (2026-08-02) and the gitops-behind defer (2026-08-07). The floor also covers a
    partially-loaded kube-state-metrics: its ClusterRole is deliberately scoped, so dropping `apps`
    would take every deployment series away while the pod stays up and Ready. That fault is
    invisible to the reachability gate above, which is why both exist. Pure
    `k8s_workloads_verdict()` is unit-tested; `CLUSTER_DEPENDENT` is guarded against the live
    `CHECKS` and asserted disjoint from the other three skip sets.)
  - **Loki Log Ingestion** (two-arm LogQL freshness against the cluster `loki-homelab` via
    its ClientIP-gated -k8s route since Phase D.2, `down`
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
  - **Promtail Dropped Entries** (`sum(increase(promtail_dropped_entries_total[1h]))`
    from Prometheus, which already scrapes `promtail:9080` — `down` above `PROMTAIL_DROPPED_MAX` (1000)
    entries dropped in the window. Where **Loki Log Ingestion** catches TOTAL silence, this surfaces
    PARTIAL loss: entries promtail gave up shipping across ALL drop reasons (`ingester_error`,
    `rate_limited`, `stream_limited`, `line_too_long` — the selector dropped its `ingester_error`-only
    filter on 2026-07-15, review M2, so Loki's own configured limits no longer reject logs unseen).
    The threshold keeps a transient Loki restart's handful
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
  - *(**Configarr Sync** moved out on 2026-08-08, slice 4 B7a. The nightly guide sync is a k8s
    CronJob on daniel-box now, and the `/configarr/state.json` this bridge read lived beside it on
    this host. The k8s/configarr role's `configarr-health.sh` cron reads the last Job through the
    read-only kubeconfig and pushes the SAME monitor with the same token, so the monitor and its
    history are unchanged — the AutoKuma label moved to the uptime-kuma role, alongside the other
    two cluster-side push monitors. The exit-code + output verdict still runs `configarr_status.py`
    verbatim; only where it runs changed.)*
  - *(**Janitorr Errors** moved out on 2026-08-08, slice 4 B7b, with the workload. It read the
    error count from Loki and the uptime from `container_start_time_seconds{name="janitorr"}`;
    cluster pod logs never reach Loki and that cAdvisor series is this host's Docker container, so
    both signals died at the port. The k8s/janitorr role's `janitorr-health.sh` cron reads the pod
    through the read-only kubeconfig and pushes the SAME monitor with the same token — the 12 h
    window, the 600 s startup grace and the uptime-minus-grace cap on the counting slice are all
    preserved, so the verdict does not change with the host. The AutoKuma label moved to the
    uptime-kuma role.)*

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
- **Startup/redeploy grace for the reach-out checks (`STARTUP_GRACE`, 2026-07-12):** the five
  checks that poll a live app dependency with **no reachability gate and no per-check hysteresis**
  — **n8n Prod Workflows** (n8n), **Arr Queue Warnings**
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
- Push tokens (`monitor_bridge_{disk,cert,mem,restarts,oom,cpu,targets,traefik,prometheus,n8n,arr_queue,prowlarr_indexers,gitops_alive,gitops_status,scrutiny,ups,pi,pi_peers,docker_user,cloudflare_drift,appsec,b2_reachable,ha,renovate_alive,loki,loki_reachable,promtail_dropped,disk_prune,fake_remux,fake_remux_replace,discord}_push_token`)
  live in `secrets.yml`; we set them and Kuma honors client-supplied tokens. They're passed
  both as env (what the script pushes to) and as `push_token=` in the AutoKuma label.
- The **Home Assistant Automations** check additionally needs `monitor_bridge_ha_token` — an HA
  **Long-Lived Access Token** (operator-minted in HA → Profile → Security; can't be templated), NOT
  a Kuma push token. tier `assisted` (rotate = revoke + reissue in HA). It's **file-mounted**
  (`HA_TOKEN_FILE=/run/secrets/ha_token`, rendered 0600 by the role, read via `check.py`'s
  `_env_file`) — an unscoped full-access token must NOT sit inline in the container Env the
  docker-proxy exposes to monitoring-net neighbors (2026-07-15 review H2). An empty token file
  disables the check (falls back to the `HA_TOKEN` env, also empty = disabled).
- The **B2 Reachable** check reuses that same file-mount pattern for its B2 credential — Kopia's
  existing application key, rendered 0600 to `./b2_probe_application_key` and read via
  `B2_PROBE_APPLICATION_KEY_FILE`. No new secret is minted for it; the probe-specific env name
  means pointing it at a scoped read-only key later is an inventory edit rather than a code change.
  The paired `B2_PROBE_KEY_ID` stays inline, since an id can't authenticate on its own.
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
  before `monitor-bridge` on a fresh host.
- The **Cloudflare IP Drift** monitor's
  `/var/lib/cloudflare-ip-drift:/cloudflare-drift:ro` mount (written weekly by the `traefik` role's
  `cloudflare-ip-drift.sh`, seeded once on deploy) is created sys_user-owned by that role, which
  deploys first (everything depends on it), so the ordering is naturally satisfied.
  The **CrowdSec AppSec** monitor's `/var/lib/crowdsec-appsec:/crowdsec-appsec:ro` mount (written
  every 15 min by the same role's `appsec-verify.sh`, seeded once on deploy) follows the same
  ordering — but that dir is **root-owned** (the verify cron runs as root via `docker exec`, like
  `docker-user-verify.sh`), and the script `chmod 0644`s its state file for the non-root reader.
- The **Disk Autoprune** monitor bind-mounts `/var/lib/autofix-disk-prune:/autofix-disk:ro`
  (written by the `autofix-bridge` role's hourly disk-prune cron). That role creates the dir
  sys_user-owned, so on a fresh host deploy `autofix-bridge` before `monitor-bridge` (else Docker
  auto-creates the mount source root-owned and the non-root container can't read it). The **Fake
  Remux Scan** monitor's `/var/lib/autofix-fake-remux:/fake-remux:ro` mount (written daily by the
  same role's `fake_remux_scan.py` cron, seeded once on deploy) is created the same way with the
  same ordering. The **Fake Remux Replace** monitor reuses that same mount — its
  `fake_remux_replace.py` cron writes `replace_state.json` into the same directory.
- Thresholds are env-tunable in the compose template (`GRACE_CYCLES` (startup/redeploy grace),
  `DISK_MAX_PCT`,
  `CERT_MIN_DAYS`, `MEM_MAX_PCT`, `RESTART_WINDOW`/`RESTART_MAX`, `OOM_WINDOW`,
  `CPU_WINDOW`/`CPU_THROTTLE_PCT`/`CPU_MIN_THROTTLED_CORES`/`CPU_CONSECUTIVE`, `TRAEFIK_5XX_PCT`/`TRAEFIK_MIN_RPS`/`TRAEFIK_SLOW_BUCKET`/`TRAEFIK_SLOW_PCT`,
  `N8N_FAIL_WINDOW`/`N8N_CONSECUTIVE_MAX`/`N8N_SYSTEMIC_STREAK`/`N8N_SYSTEMIC_MAX`; n8n connection
  config: `N8N_URL`/`N8N_API_KEY`; arr queue
  connection config: `SONARR_URL`/`SONARR_API_KEY`/`RADARR_URL`/`RADARR_API_KEY`; GitOps
  liveness: `GITOPS_MAX_AGE_MIN`/`GITOPS_STATE_DIR`; Pi pressure:
  `PI_GLANCES_URL`/`PI_LOAD_MAX`/`PI_MEM_MIN_MB`/`PI_DISK_MAX_PCT`; HA heartbeat:
  `HA_URL`/`HA_TOKEN`/`HA_HEARTBEAT_MAX_AGE`/`HA_CONSECUTIVE`). A failed
  query/unreachable source makes that monitor `down` with an explanatory msg — a broken
  exporter is surfaced, not silently green.

## Operator prerequisites
1. Add the 34 push tokens to `secrets.yml` (`sops ansible/vars/secrets.yml`). **They must
   be exactly 32 alphanumeric chars** (Kuma rejects others, e.g. `openssl rand -hex 16`);
   AutoKuma silently refuses to create the monitor otherwise (`Invalid push_token`).
2. For the n8n monitor: add `n8n_api_key` to `secrets.yml`. Mint it in the n8n UI
   (**Settings → n8n API**), scoped to read **Workflow** + **Execution** permissions.
3. For the Arr Queue Warnings monitor: `sonarr_api_key`/`radarr_api_key` already exist in
   `secrets.yml` (configarr/janitorr/homepage reference them too — get the plaintext from
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
