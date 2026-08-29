# monitor-bridge — every threshold check, pushed to Uptime Kuma (k8s)

> **THE bridge since the Docker uninstall (2026-08-14).** Born as the daniel-server
> sidecar, split at the Phase F drain, whole again in-cluster: this role's
> `files/check.py` runs every check. The gitops pair reads daniel-box's own deployer
> via a hostPath (the pod is pinned there); disk_prune retired with the Docker daemon;
> pi_peers and renovate_alive dissolved into direct pushers at the host flips
> (k8s/pi-peer-backup CronJob; renovate-notify's ExecStartPost). check.py still
> refuses a CHECKS_ONLY/CHECKS_SKIP filter naming an unknown check or a gated check
> without its gate, and `test_checks_and_env_secret_push_tokens_agree` asserts the
> env-secret carries exactly the token set the code reads. Much of the per-check
> documentation below predates the moves — Docker-era plumbing details (compose, bind
> mounts, networks) are history (`roles/containers/archive/monitor-bridge/`).
>
> **`files/check.py`'s `CHECKS` list is the authority on which checks exist.** This file is
> prose and nothing tests it: until 2026-08-16 the three retired above were still written up
> here in the present tense, as live checks with unit-tested pure functions, four weeks after
> the functions were deleted. If a bullet below disagrees with `CHECKS`, `CHECKS` is right.

A tiny sidecar that turns host-cron state files into Uptime Kuma **push** monitors, so
threshold problems actually page. See repo-root `CLAUDE.md`. (The kopia backup checks
retired with kopia on 2026-08-10 — the backup plane is Longhorn;
`backup-consolidation-longhorn.md`.)

## At a glance
- **Image:** `python:3.14-alpine` (stdlib only — no build, no extra deps)
- **Host:** daniel-box — pinned by `nodeSelector` · **No web UI**, no Authelia
- **Reaches:** prometheus, Kuma, n8n, the *arrs, scrutiny, HA and Loki over their in-cluster
  Service names (see `templates/env-secret.yaml.j2`) — no VIP, no Traefik, no gate in a
  probe's path. The LAN routes it used during the migration are gone, suffix and all.
- **Depends on:** prometheus (`meta/deps.yml`)
- **Config in:** `ansible/inventory/host_vars/daniel-box.yml` → `containers_list`

## Notable
- `files/check.py` is a **static** Python loop (config via env vars, no Jinja). Every
  `INTERVAL` (300 s) it runs every entry in `CHECKS` plus the four reachability
  gates (prometheus, loki_reachable, b2_reachable, cluster_prometheus) and pushes
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
  - **Host coverage floor** — not a monitor of its own, but a second arm inside **Root Disk** and
    **Memory** (`_host_origin_shortfall`, added 2026-08-23; documented here 2026-08-23b review
    M15, having previously existed only as code comments). Both checks group by `origin`, and
    node-exporter is a DaemonSet on both nodes, so a vector covering fewer than
    `HOST_ORIGINS_MIN` (2) distinct hosts has **lost** a host rather than measured a healthy
    estate — at which point the surviving node's numbers would be reported as the estate's.
    Live on 2026-08-23: a one-directional UFW rule left daniel-box's node-exporter unreachable
    for 5.4h, and both checks pushed OK off daniel-server alone while daniel-box's host memory
    and `/boot` went unwatched behind two green tiles.
    **Why not lean on Scrape Targets:** that keys on `up`, and node-exporter's normal failure is
    PER-COLLECTOR — a filesystem or meminfo collector can fail with `up == 1`, leaving Scrape
    Targets green while the host silently drops out of these two checks. Same shape as
    `check_ups`'s partial-absence arm: never monitor the survivor silently.
    `HOST_ORIGINS_CONSECUTIVE` gives it hysteresis, for the reason `UPS_CONSECUTIVE` exists —
    the weekly Sunday reboot takes a node's exporter away against a 1m scrape and a 5m loop.
    Reported AFTER each check's own breach scan: a reporting host that is genuinely out of
    memory outranks a complaint about the absent one.
    `HOST_ORIGINS_MIN` is rendered in `templates/env-secret.yaml.j2` so a planned single-node
    maintenance window can lower it and put it back; **at 1 the arm is inert**, which is the
    original failure, so treat it as a temporary setting rather than a fix for a noisy tile.
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
    public trackers from the offender list — set to `The Pirate Bay,1337x` — the first after its
    apibay.org backend 503'd/timed-out for hours and flapped this monitor up/down on 2026-07-05,
    the second for the same chronic flapping (the remaining indexers
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
  - **WG Pi Peer Backup** — RETIRED from this container at the host flips (2026-08-14). The pull
    became the `pi-peer-backup` k8s CronJob, which pushes its Kuma monitor directly, so there is
    no `/pi-peers/state.json` on this host and no `pi_peers()` check here. The monitor and the
    gap it watches are unchanged: the rsync uses no `--delete`, so a silently-failing pull leaves
    the last-good copy in place while the Pi's un-rebuildable WireGuard peer keys go stale.
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
  - **Disk Autoprune** — RETIRED at the Docker uninstall (2026-08-14), with no successor. The
    cron pruned daniel-server's Docker daemon, which no longer exists; containerd's own image GC
    owns that concern now (`files/check.py:167-170`). Root Disk's threshold pager is what is
    left on that axis — alerting without remediation, deliberately.
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
    moved to Longhorn (`docs/archive/k3s-migration/backup-consolidation-longhorn.md`) — which left
    `B2_DEPENDENT` empty for five days. **It is not empty now:** `check_b2_storage` (B2 Storage
    Usage) was added 2026-08-15 and is gated here, so a transaction-cap incident does not page
    twice for one root cause. That check queries B2 live rather than reading a cron's state file,
    which is what made the five kopia-era checks report a stale success in the first place. This check reports B2's own
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
  - **R2 Free Tier Headroom** (Cloudflare's GraphQL Analytics API, `r2StorageAdaptiveGroups` +
    `r2OperationsAdaptiveGroups` in ONE POST: month-to-date storage bytes, Class A and Class B
    operation counts as a percentage of the free tier (10 GB / 1M / 10M), `down` past
    `R2_USAGE_MAX_PCT` (80) on any arm. **Cloudflare offers no spending cap or usage limit on R2,
    on any plan** — the Usage-Based Billing notification that would do this natively needs a Pro
    plan and this account is Free — so a watched threshold is the only boundary that exists. It is
    a headroom guard, not a bill-shock alarm: overage is $0.015/GB-month, $4.50/M Class A, $0.36/M
    Class B with egress free, so the realistic worst case is under a dollar. What it actually
    catches is a **runaway client** while the fix is still a config edit — the shape of the
    2026-08-13 Longhorn retry storm, which drove ~2.5k B2 Class B/day against a hard cap.
    A fourth arm counts **outstanding incomplete multipart uploads** (`R2_UPLOADS_MAX`, 25): they
    bill as stored bytes and do NOT appear in an object listing, which is the quiet way a 10 GB
    budget fills. The durable fix for those is a bucket lifecycle rule (below); this arm is the
    backstop for that rule being absent, deleted, or not working.
    Operation classes are NOT in the API response — Cloudflare returns raw `actionType` names — so
    the Class A / Class B mapping lives in `check.py` from the pricing page. An actionType in
    neither published list counts toward **Class A** (the tighter, more expensive arm) and is named
    in the message: over-counting reports headroom we do not have, which is the safe direction, and
    the name explains why the numbers moved when Cloudflare adds an operation.
    **SUCCESSES are cached for `R2_PROBE_INTERVAL_S` (1800 s); a failure is NOT** — the inverse of
    `b2_reachable`'s cache-both, deliberately: B2's cached failure protects a spend cap that
    retrying would deepen, whereas GraphQL analytics calls are free and count against no R2 budget.
    So this follows `EMAIL_PROBE_INTERVAL_S`'s cache-successes-only idiom and rides out a transient
    Cloudflare blip through `STARTUP_GRACE` instead of through a stale verdict.
    A 200 carrying a populated `errors` array — how an under-scoped token arrives — is raised, not
    parsed: unchecked it reads as a zero-usage bucket, a monitor green because it is blind.
    **The free tier is per-ACCOUNT; this query filters by `bucketName`.** Identical while there is
    one bucket, and silently under-reporting the day there is a second — at which point drop the
    `bucketName` filter rather than raising the thresholds.
    Empty `CF_ACCOUNT_ID`/`CF_ANALYTICS_TOKEN`/`R2_BUCKET` = disabled (stays up). Pure
    `r2_month_start()`/`r2_classify_operations()`/`r2_usage_verdict()` are unit-tested.)
  - **SMART Data / Health** (scrutiny web API `/api/summary` over `monitoring`: every
    non-archived device must have a `collector_date` within 26 h **AND a passing `device_status`**
    (0 = SMART self-assessment + Scrutiny's attribute thresholds both OK; non-zero decodes to
    "SMART self-assessment FAILED" / "attribute threshold breached"). Freshness catches a
    silently-dead collector (cron-as-PID1, no usable healthcheck → only shows as aging data); the
    status check catches a drive that goes SMART-FAILED / breaches a threshold while STILL reporting
    fresh data — nothing else alerts on that (Scrutiny writes to InfluxDB not Prometheus, and its
    own Shoutrrr notifier is unconfigured, so this bridge check is the only drive-failure alert
    path). Also `down` when scrutiny lists no devices at all. `SCRUTINY_TEMP_MAX` (°C, default
    0 = off) adds an optional early-warning temperature ceiling on top.
    **A third arm watches NVMe endurance** (added 2026-08-22): `percentage_used` against
    `SCRUTINY_WEAR_MAX` (default 80). Scrutiny ships that attribute with `thresh=100`, so its own
    evaluation cannot fold a breach into `device_status` until the drive's rated write endurance is
    fully spent — the wear curve offers months of warning where `device_status` offers days.
    It is the one arm NOT served by `/api/summary`, whose `smart` block carries `collector_date`,
    `temp` and `power_on_hours` but no wear attributes: it costs one
    `/api/device/<wwn>/details` fetch per non-archived device per
    cycle (~19 KB each, only `smart_results[0]` read), taken after freshness passes so a dead
    collector costs no per-device calls. A device that reports no `percentage_used` is **unwatched,
    not healthy** — the message names it, and says INERT when no device reports the field at all.
    Missing must not page either: `percentage_used` is NVMe-only, so a SATA disk added later
    legitimately has none. Pure `scrutiny_freshness()`, `scrutiny_health()`,
    `scrutiny_device_wear()` and `scrutiny_wear_verdict()` are unit-tested; those tests mock the
    payload, so they prove the verdict logic and nothing about the endpoint path.)
  - **Host Temperature** (board and CPU sensors from node-exporter's hwmon collector, added
    2026-08-28: `node_hwmon_temp_celsius`, with drives EXCLUDED — `HWMON_TEMP_EXCLUDE_CHIP`
    drops the `nvme_` chips because SMART Data / Health above already owns them, and reading
    them here would double-page one condition. Nothing alerted on host temperature before this:
    `check_cpu_throttle` sees CFS throttling, which is a cgroup limit rather than heat, and the
    Grafana **Hardware Temperature Monitor** panel plots these series but a panel pages nobody.
    Every sensor gets a limit from one of **two exhaustive arms** — `HWMON_TEMP_RATIO` (0.90) of
    its own declared max where that max is plausible, else the flat `HWMON_TEMP_FALLBACK_C`
    (85 °C). **The fallback is not a nicety.** Measured live 2026-08-28, only 7 of 21 scraped
    sensors declare a usable max, and both daniel-pi sensors declare none — so a
    declared-max-only check would be silent on two thirds of the estate, daniel-pi included.
    **The declared max is sanity-bounded, not trusted:** three NVMe sensors declare 65261.85
    (a 0xFFFF sentinel for "no max"), and a ratio of that is unreachable, so those sensors
    would read green through a fire. A max outside
    (`HWMON_TEMP_MIN_PLAUSIBLE_C`, `HWMON_TEMP_MAX_PLAUSIBLE_C`] is treated as UNDECLARED.
    An EMPTY sensor vector is `down`, not `up` — zero readings means EVERY collector went blind,
    and "nothing is too hot" from no data is a lie. `HWMON_TEMP_CONSECUTIVE` (3) rides out a
    transcode spike.
    **A PARTIAL blindness is a separate arm** (`HWMON_TEMP_ORIGINS_MIN`, added 2026-08-29 for
    review M-9): the empty-vector branch fires only when ALL hosts go quiet, so until this arm
    existed one host's hwmon collector could die while the other two answered "all below limit"
    for the estate. It reuses `_host_origin_shortfall`, the same helper Root Disk and Memory
    use, but passes its OWN floor — **3, not the shared `HOST_ORIGINS_MIN` of 2**, because all
    three hosts declare non-excluded sensors (measured 2026-08-29: 9 / 5 / 2), so a floor of 2
    is met by any two of them. Origins are counted over the series that survive
    `HWMON_TEMP_EXCLUDE_CHIP`, through the same predicate `hwmon_temp_limits` uses — a host
    whose only sensors are nvme is a host this check does not cover, and counting it would
    satisfy the floor with a host nothing watches.
    `HWMON_TEMP_ORIGINS_CONSECUTIVE` (5) is **longer than `HOST_ORIGINS_CONSECUTIVE`** (3) on
    purpose: the third host is the Pi, and over the 7d to 2026-08-29 its hwmon series went
    absent for about 20 minutes (6 of 1054 samples at a 5m step, all daniel-pi), which the
    shared 15-minute grace would have paged on. The two hysteresis mechanisms are never
    compounded — `down_streak` is the thermal-spike grace and applies only to the hot-sensor
    path, so a missing host pages on its own 5th cycle rather than the 15th.
    Adding the arm is also why `host_temp` joined `EXPORTER_DEPENDENT["node"]`: a dead
    node-exporter now trips the floor, and without the entry one root cause would page twice. Pure `hwmon_temp_limits()` / `hwmon_temp_verdict()` are unit-tested in
    `test_host_temp.py`, each rule as an accept/reject pair; the coverage test is the load-bearing
    one, since this check's failure mode is silence rather than a wrong threshold.)
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
    healthcheck-timeout storms no other monitor saw.
    **This check still owns Pi disk and memory, but the reason it polls glances has changed.**
    It read "rather than adding a Pi node-exporter: zero Pi-side RAM cost, and a second
    node-exporter would have broken the instance-blind `node_*` queries in the Memory/Root Disk
    checks." The Pi HAS a node-exporter since 2026-08-24 — the collision that argument named is
    real, and it was fixed rather than avoided: `HOST_METRIC_ORIGIN_EXCLUDE` keeps daniel-pi out
    of both those queries (`host_metric_sel`), so they stay two-host checks and this one stays
    the single source of truth for Pi pressure. What the exporter adds is the half glances never
    could — retained time series, so Pi memory and SD usage can be graphed and trended instead of
    only tripping a threshold. The RAM cost is no longer zero: node-exporter measures ~20 MiB on
    a 456 MB box, which is why its collector set is trimmed in the role's compose template.
    **If you add a third node-exporter host, decide explicitly whether it belongs in the
    estate-wide Memory/Root Disk checks or in a check of its own** — that choice is what this
    bullet exists to force. Empty
    `PI_GLANCES_URL` = disabled (stays up); the static Kuma HTTP monitor
    `daniel-pi-glances` covers glances itself being down.
    **Published-port arm** (`with_pi_ports`, folded here rather than given its own monitor for
    the push-token reason recorded at `with_ha_ban`): after a Pi reboot a container can come
    back attached to no Docker network while still reporting `Up (healthy)`, because its
    healthcheck curls loopback inside its own netns. The observable harm is that its published
    port stops listening, and only a recreate restores it — autoheal's restart loop re-enters
    the same empty sandbox and structurally cannot.
    The arm TCP-connects to each expected port and fetches glances' `/api/4/containers` **only
    when something is already dead**, to say why: up with no `->` mapping is detached and needs
    a recreate; up *with* a mapping is a bind or firewall fault; not up is an ordinary down.
    **Do not invert that order.** Measured 2026-08-27 against the live Pi, `/api/4/load`,
    `/mem` and `/fs` answer in 0.03-0.06s each while `/api/4/containers` took 4.43s and then
    timed out at the 10s `HTTP_TIMEOUT` on the very next call — polling it every cycle would
    leave the arm failing open most of the time, inert behind a green monitor, which is the
    exact failure mode it exists to catch. A failed attribution fetch downgrades the diagnosis
    to "cause unknown" and never the verdict; the port is still reported dead.
    `PI_PUBLISHED_PORTS` renders `name:port` pairs from daniel-pi's `containers_list` (every
    entry with a `port`), so `docker-proxy`, `autoheal` and `docker-proxy-lifecycle` — which
    publish nothing forever — fall out by construction rather than by an exclusion list.
    `udp_port` is excluded: there is no TCP-connect equivalent for UDP. `PI_PORTS_CONSECUTIVE`
    (2) rides out the seconds of closed ports a Pi deploy's container recreate causes.
    **This arm adds no reachability coverage — it adds attribution, and that is the whole
    case for it.** Measured 2026-08-27: Kuma HTTP-monitors glances, dozzle and wg-easy, and
    `promtail`/`node-exporter` are Prometheus scrape targets (`job=promtail-pi`, `job=node-pi`)
    that `check_targets_down` already covers. So every publisher was already watched. What
    nothing said was *why* a port went quiet, and on 2026-08-08 that cost a manual sweep
    across four monitors which then missed dozzle entirely. Turning "service X is down" into
    "these containers are up with no published ports — recreate, a restart cannot fix it" is
    the value. Do not re-justify this arm as filling a monitoring gap; it does not.)
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
    **A second arm watches HA's own ip_ban** (added 2026-08-23): a `count_over_time` LogQL query
    for `Banned IP` lines over `HA_BAN_WINDOW` (1h), keyed on `container="home-assistant"` (see the no-`app`-label trap below), `down` on any hit. HA's ban middleware runs on
    every request and keys on the peer address, so an unauthenticated burst from inside the cluster
    bans an INFRASTRUCTURE ip — on 2026-08-23 five ad-hoc `curl` calls banned `10.42.0.1`, the
    node's pod-network gateway, and HA 403'd the kubelet probes arriving from it into a crash loop
    that paged k3s Workload Health. The probes now exec curl to `127.0.0.1` and cannot be banned,
    which fixes the crash loop and makes a ban SILENT — HA keeps serving while whatever shares that
    source IP stays locked out. This arm is the visibility half. Folded into this monitor rather
    than given its own for the same reason as the extended-resource arm: a new Kuma monitor needs a
    new push token in SOPS, and a ban is an HA fault. A ban wins the message and keeps the
    heartbeat's text after it. It also **skips `down_streak`** — that exists to ride out a
    transient, and a ban either happened in the window or did not, so a second cycle's confirmation
    adds nothing.
    **It watches the ban EVENT, not the ban STATE.** `Banned IP` is logged once, at ban time, so
    the arm pages for `HA_BAN_WINDOW` and then SELF-CLEARS while the entry is still in
    `/config/ip_bans.yaml`. A ban older than the window — or one reloaded from that file by an HA
    restart, which logs nothing — is invisible. **A green `ha_heartbeat` does not mean "no IP is
    banned"**; it means "no ban was issued in the last `HA_BAN_WINDOW`". That is the only signal
    available: HA does not log its ongoing 403s to a banned peer, and this pod cannot read HA's PVC.
    The durable artifact is the **Discord notification** Kuma fires on the down transition, not the
    monitor's colour — when one fires, read `/config/ip_bans.yaml` by hand rather than waiting for
    the monitor to clear.
    **`ha_heartbeat` is deliberately NOT in `LOKI_DEPENDENT`**: membership there suppresses the
    WHOLE check during a Loki outage, which would blind the real heartbeat. The ban arm instead
    fails open on a Loki error and keeps the heartbeat's own verdict. Pure `ha_ban_verdict()` is
    unit-tested.
    Spec: `docs/superpowers/specs/2026-06-19-ha-automation-heartbeat-watchdog-design.md`.)
  - **k3s Speedtest** (speedtest-tracker's `/api/v1/results`, newest row only, Bearer
    `speedtest_api_token` from the mounted credentials Secret. Three arms in this order —
    status, then age, then the download floor. The order is load-bearing: `download_bits` is
    null on a failed row, so a floor comparison ahead of the status arm compares None.
    **The floor is 100 Mbps because results are bimodal, not because 100 is a target.** Which
    Ookla server a run draws decides the mode: over 2026-08-14..24, 20 runs on server 41671
    had a median of 910 Mbps and a worst of 119, while 17 runs on six other servers had a
    median of 12.8 and a best of 42.8. Nothing landed between. The speedtest role now pins
    41671, and this floor is what makes that pin degrading visible — 1775 was clean for 54
    runs and then was not.
    The age arm (`SPEEDTEST_MAX_AGE_H`, 8h against a 6h schedule) is the one that notices the
    scheduler dying, which has no other symptom: the pod keeps serving its UI and passing both
    probes while writing no new rows.
    **No hysteresis on the verdict, only on the fetch.** The app produces a row every 6h and
    this loop runs every 5 min, so a consecutive-cycle streak would re-read one row up to 72
    times — delaying the page and proving nothing. The fetch rides `SPEEDTEST_CONSECUTIVE`
    because an app restart under a deploy is a real transient; `speedtest` is also in
    `STARTUP_GRACE`. Same split as `check_ha_heartbeat`.
    Reaching the app needs `netpol-baseline/templates/networkpolicy-speedtest.yaml.j2` — the
    baseline admits traefik, prometheus and two cni0 /32s, none of which is this pod.)
  - **Renovate Notifier — Alive** — RETIRED from this container at the host flips (2026-08-14).
    The notifier pushes its own Kuma monitor from an `ExecStartPost` now, so there is no
    `/renovate-state/last_run` bind mount and no `renovate_alive()` check here. The monitor and
    its dead-man semantics are unchanged.
    Spec: `docs/superpowers/specs/2026-06-19-renovate-manual-action-notifier-design.md`.
  - **Loki Reachable** (a fixed `/loki/api/v1/labels` probe — the root-cause GATE for the
    Loki-querying checks, the peer of Prometheus Reachable. Evaluated each cycle: when Loki is
    unreachable the `LOKI_DEPENDENT` check (loki_ingestion) is
    **suppressed** — pushed `up` with a "skipped — Loki unreachable" msg — and only THIS monitor
    pages. It was two until janitorr's watchdog moved to the cluster (2026-08-08); one Loki outage
    firing both at once is why the gate exists. Loki being UP but promtail not
    shipping is a different signal Loki Log Ingestion still surfaces. `LOKI_DEPENDENT` is guarded by
    a test against the live `CHECKS` so it can't drift.)
  - **Cluster Prometheus Reachable** (a `vector(1)` probe against the **k3s cluster's** Prometheus
    over its in-cluster Service DNS name — a SECOND instance on daniel-box, not the one `PROM_URL`
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
    invisible to the reachability gate above, which is why both exist.
    **A second arm covers DaemonSets** (added 2026-08-13):
    `kube_daemonset_status_number_unavailable`, with its own `K8S_MIN_DAEMONSETS` floor (9) and
    the same fail-closed-on-absent-series logic — a Deployment-shaped census cannot see promtail,
    node-exporter or the otel collector, which run one pod per node and are exactly the workloads
    a node problem takes out first. A third arm reports crash-looping restarts —
    `increase(...[K8S_RESTART_WINDOW]) > K8S_RESTART_MAX` (1h / 3), **and** a restart inside
    `K8S_RESTART_RECENT_WINDOW` (30m). The recency clause is what lets a RECOVERED pod leave the
    arm: `increase` is a pure lookback, so without it the restarts that already happened hold the
    monitor DOWN for the rest of the hour — zigbee2mqtt recovered at 09:47 on 2026-08-23 and the
    arm still read `restarts in window: 9`. The 30m floor is the worst observed inter-restart
    SPACING, not the 5-min backoff cap: the homepage incident above spaced 31 restarts ~15-19 min
    apart, and a window inside that spacing goes up in the gaps and flaps, which at
    `max_retries: 0` is a notification per transition.
    **A fourth arm watches extended resources** (added 2026-08-20, PR #281):
    every name in `K8S_EXTENDED_RESOURCES` (comma-separated, default `devic.es/dri`) must still be
    advertised at non-zero quantity by at least one node, read from
    `kube_node_status_allocatable`. This is the blast radius of a wedged device plugin, not the
    plugin's own liveness: `dri-device-plugin` has no readinessProbe, and a container without one
    is Ready the instant it starts, so a plugin whose gRPC registration hangs keeps a Running,
    Ready, fully-available DaemonSet while kubelet deregisters `devic.es/dri` — invisible to the
    DaemonSet arm above. **The obvious socket-stat probe is worse than nothing** (rationale
    recorded in commit `1b2aa497`): the registration socket file persists through the wedge, so
    the probe reads green through the fault, and kubelet clears that directory on restart, so the
    same probe restart-loops a healthy plugin. `ksm_resource_label()` sanitizes the configured
    Kubernetes name into the label kube-state-metrics actually emits (`devic.es/dri` →
    `devic_es_dri`) — querying the unsanitised name matches no series, which this arm would read
    as a deregistered resource; that is the 2026-08-20 false page in **Traps** below. The arm
    needs kube-state-metrics' `nodes` collector: with no `kube_node_status_allocatable` series at
    all it reports **INERT** and names what it is not watching, rather than passing silently.
    Folded into this monitor rather than given its own, because a new Kuma monitor needs a new
    push token in SOPS and this arm answers the DaemonSet arm's question. A resource fault wins
    the message and keeps the workload arm's text after it. Pure
    `k8s_workloads_verdict()` and `extended_resource_verdict()` are unit-tested;
    `CLUSTER_DEPENDENT` is guarded against the live
    `CHECKS` and asserted disjoint from the other three skip sets.)
  - **Loki Log Ingestion** (three-arm LogQL freshness against the cluster `loki-homelab` via
    its in-cluster Service, `down`
    if ANY arm is silent — a silently-dead promtail→Loki pipeline (docker-proxy break,
    positions-file corruption, relabel regression) that Loki's `/ready` Kuma probe stays green
    through. **Arm 1 — file-tail union** `sum(count_over_time({job=~"authlog|syslog"}[3h]))`:
    (`check.py`'s in-code default also lists `traefik`, but `LOKI_STREAM` in
    `templates/env-secret.yaml.j2` overrides it and does not — the deployed selector is the two
    named here, so traefik's freshness is NOT covered by this arm.)
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
    promtail death fast. **Arm 3 — daniel-pi** `sum(count_over_time({job="pi"}[3h]))`
    (`LOKI_PI_STREAM`, added 2026-08-25 review M-11): arms 1 and 2 only count CLUSTER streams, so
    the Pi's own promtail could die with every cluster stream still flowing and both arms green —
    the Pi's logs simply stop arriving and nothing said so. Runs on the TOLERANT window
    (`LOKI_FILETAIL_WINDOW`, same as arm 1), for a stronger reason than arm 1's: the Pi is a
    Zero 2 W running five LAN-only containers, so its log volume is genuinely low and bursty, not
    just quiet overnight. Selectors/windows tunable via
    `LOKI_STREAM`/`LOKI_FILETAIL_WINDOW`/`LOKI_DOCKER_STREAM`/`LOKI_WINDOW`/`LOKI_PI_STREAM`. Pure
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
- **Liveness probe (2026-06-10, k8s since the migration):** check.py touches `/tmp/heartbeat`
  (tmpfs) after every cycle; the `livenessProbe` in `templates/deployment.yaml.j2` fails when the
  mtime exceeds ~3×INTERVAL, so **the kubelet** restarts a *hung* loop (death alone already exits
  the container). Kuma push silence remains the alerting path; the probe adds auto-recovery.
  (This was a Compose healthcheck restarted by autoheal until the k8s migration. autoheal now
  runs only on daniel-pi, so looking for its log line here finds nothing.)
- Push tokens — 29 of them, and **`templates/env-secret.yaml.j2`'s `KUMA_PUSH_*` keys are the
  list**; no test reads this prose, so treat the names below as a reading aid that can go stale
  rather than as the source of truth. (It had: it carried eight tokens retired at the
  2026-08-14 host flips and was missing six added since.) As of 2026-08-16:
  `monitor_bridge_{arr_queue,b2_reachable,b2_storage,cert,cluster_prometheus,cluster_targets,cpu,discord,disk,gitops_alive,gitops_status,ha,k8s_workloads,loki,loki_reachable,mem,n8n,oom,pi,prometheus,promtail_dropped,prowlarr_indexers,r2_usage,restarts,scrutiny,targets,traefik,traefik_latency,ups}_push_token`
  live in `secrets.yml`; we set them and Kuma honors client-supplied tokens. They're passed
  both as env (what the script pushes to) and as `push_token=` in the AutoKuma label.
- The **Home Assistant Automations** check additionally needs `monitor_bridge_ha_token` — an HA
  **Long-Lived Access Token** (operator-minted in HA → Profile → Security; can't be templated), NOT
  a Kuma push token. tier `assisted` (rotate = revoke + reissue in HA). It's **file-mounted**
  (`HA_TOKEN_FILE=/run/secrets/ha_token`, rendered 0600 by the role, read via `check.py`'s
  `_env_file`) — an unscoped full-access token must NOT sit inline in the container Env the
  docker-proxy exposes to monitoring-net neighbors (2026-07-15 review H2). An empty token file
  disables the check (falls back to the `HA_TOKEN` env, also empty = disabled).
- The **B2 Reachable** check reuses that same file-mount pattern for its B2 credential — the
  existing `kopia_b2_application_key`, which is LONGHORN's B2 key despite the name (ADR-0014),
  rendered 0600 to `./b2_probe_application_key` and read via
  `B2_PROBE_APPLICATION_KEY_FILE`. No new secret is minted for it; the probe-specific env name
  means pointing it at a scoped read-only key later is an inventory edit rather than a code change.
  The paired `B2_PROBE_KEY_ID` stays inline, since an id can't authenticate on its own.
- The two GitOps monitors read host state via a **read-only bind-mount**
  `/var/lib/gitops-deploy:/gitops-state:ro` (written by the `gitops_deploy` host role) — no
  Prometheus/n8n source. That dir must exist owned by the deploy user before deploy; the
  `gitops_deploy` role creates it, so deploy `gitops_deploy` before `monitor-bridge` (else Docker
  auto-creates the mount source root-owned and the non-root container can't read it).
  (The **Renovate Notifier — Alive** and **WG Pi Peer Backup** monitors had the same
  deploy-ordering requirement, for `/renovate-state` and `/pi-peers`. Both dissolved into direct
  pushers at the 2026-08-14 host flips, so neither mount nor either ordering constraint exists
  now.)
- The **Cloudflare IP Drift** monitor's
  `/var/lib/cloudflare-ip-drift:/cloudflare-drift:ro` mount (written weekly by the `traefik` role's
  `cloudflare-ip-drift.sh`, seeded once on deploy) is created sys_user-owned by that role, which
  deploys first (everything depends on it), so the ordering is naturally satisfied.
  The **CrowdSec AppSec** monitor's `/var/lib/crowdsec-appsec:/crowdsec-appsec:ro` mount (written
  every 15 min by the same role's `appsec-verify.sh`, seeded once on deploy) follows the same
  ordering — but that dir is **root-owned** (the verify cron runs as root via `docker exec`, like
  `docker-user-verify.sh`), and the script `chmod 0644`s its state file for the non-root reader.
- (The **Disk Autoprune** monitor bind-mounted `/var/lib/autofix-disk-prune:/autofix-disk:ro`
  and required `autofix-bridge` to deploy first. Retired with the Docker daemon on 2026-08-14 —
  no mount, no ordering constraint.) The **Fake Remux Scan** monitor's `/var/lib/autofix-fake-remux:/fake-remux:ro` mount (written daily by the
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
  `PI_GLANCES_URL`/`PI_LOAD_MAX`/`PI_MEM_MIN_MB`/`PI_DISK_MAX_PCT`/`PI_PUBLISHED_PORTS`/`PI_PORT_TIMEOUT`/`PI_PORTS_CONSECUTIVE`; HA heartbeat:
  `HA_URL`/`HA_TOKEN`/`HA_HEARTBEAT_MAX_AGE`/`HA_CONSECUTIVE`; speedtest:
  `SPEEDTEST_URL`/`SPEEDTEST_TOKEN`/`SPEEDTEST_DOWNLOAD_MIN_MBPS`/`SPEEDTEST_MAX_AGE_H`/`SPEEDTEST_CONSECUTIVE`;
  host-coverage floor:
  `HOST_ORIGINS_MIN`/`HOST_ORIGINS_CONSECUTIVE`, and the thermal check's own pair
  `HWMON_TEMP_ORIGINS_MIN`/`HWMON_TEMP_ORIGINS_CONSECUTIVE`). A failed
  query/unreachable source makes that monitor `down` with an explanatory msg — a broken
  exporter is surfaced, not silently green.

## Operator prerequisites
1. Add a push token to `secrets.yml` (`sops ansible/vars/secrets.yml`) for every `KUMA_PUSH_*`
   entry in `templates/env-secret.yaml.j2` — `test_every_push_token_env_is_wired_to_a_monitor`
   asserts that template's keys match the AutoKuma monitors, so the template is the list. (It
   does not read this file; the token names quoted above are prose and have drifted before.)
   **They must
   be exactly 32 alphanumeric chars** (Kuma rejects others, e.g. `openssl rand -hex 16`);
   AutoKuma silently refuses to create the monitor otherwise (`Invalid push_token`).
2. For the n8n monitor: add `n8n_api_key` to `secrets.yml`. Mint it in the n8n UI
   (**Settings → n8n API**), scoped to read **Workflow** + **Execution** permissions.
3. For the Arr Queue Warnings monitor: `sonarr_api_key`/`radarr_api_key` already exist in
   `secrets.yml` (configarr/janitorr/homepage reference them too — get the plaintext from
   `sudo k3s kubectl -n homelab exec deploy/sonarr -- cat /config/config.xml` (likewise radarr)
   if you need to re-derive them — both are k8s pods now, and neither cluster node has had
   Docker since 2026-08-14, so the `docker exec` this line used to give has no target).
   monitor-bridge joined the `media` network for this on
   2026-07-02 (its `containers_list` entry in `ansible/inventory/host_vars/daniel-box.yml`);
   if `media` is ever dropped from that entry, the check pages `down` every cycle
   (unresolvable host) rather than failing silent.
4. For the R2 Free Tier Headroom monitor: add `cloudflare_analytics_token` to `secrets.yml`. Mint
   it at **Cloudflare dashboard → My Profile → API Tokens → Create Token → Custom token**, with
   exactly one permission: **Account → Account Analytics → Read**, scoped to this account. It is
   file-mounted (`CF_ANALYTICS_TOKEN_FILE=/etc/bridge-credentials/cf_analytics_token`) for the same
   H2 reason as `ha_token`. tier `assisted` (rotate = revoke + reissue in the dashboard). The
   check also reads the existing `r2_account_id` and `r2_bucket`. Run
   `uv run python scripts/secrets_mgmt/secret_rotation.py sync` after adding both, or the prek registry hook
   fails. Then smoke-test the query for real —
   `sudo k3s kubectl -n homelab exec deploy/monitor-bridge -- python /app/check.py --once` — the
   unit tests mock the payload, so this is the first thing that proves Cloudflare accepts the
   query and that the token is scoped correctly.

   **Do NOT give this token write or R2 permissions.** A token that could revoke R2 access would
   let the bridge hard-stop the bucket at the threshold, but that means parking a strictly more
   privileged standing credential in the cluster to protect against sub-dollar overage, and its
   firing would break the backup path it is guarding. Deliberate trade: this monitor pages, and a
   human decides. If a hard stop is ever wanted, the manual procedure is **R2 → Manage R2 API
   Tokens → revoke the key** — and the way back is re-minting it and updating `r2_access_key_id` /
   `r2_secret_access_key`, so treat it as a break-glass step, not a routine one.

   **One-time bucket setting, not codified here:** set an `AbortIncompleteMultipartUpload`
   lifecycle rule (7 days) on the bucket —
   `npx wrangler r2 bucket lifecycle add <bucket> --name abort-mpu --abort-multipart-days 7`, or
   dashboard → R2 → the bucket → Settings → Object Lifecycle Rules. It needs the S3 API or
   Wrangler, neither of which the stdlib-only bridge has, and hand-rolling a SigV4 signer that
   could not be tested against the live bucket from here would be worse than a documented step.
   The monitor's uploads arm is what notices if this is missing.
5. Notifications attach **automatically** — the `kuma()` macro tags every monitor with
   `notification_name_list=["{{ kuma_notification_id }}"]`, linking it to the AutoKuma-managed
   Discord notification defined on the `uptime-kuma` container. No per-monitor UI clicking.

## Module layout — and the one rule that governs it

`files/` holds six runtime modules. `check.py` is the entrypoint the Deployment runs and still
owns the I/O, the env-derived config and the `CHECKS` registry; the others hold pure logic that
takes its inputs as arguments.

| module | holds |
|---|---|
| `check.py` | config constants, HTTP/PromQL fetching, every `check_*`, `CHECKS`, the run loop |
| `bridge_common.py` | `_env`, `sanitize` — the two helpers shared verbatim with autofix-bridge's `autofix.py`, staged into that role's ConfigMap too (see its CLAUDE.md) |
| `bridge_parsing.py` | duration/timestamp parsing, `endpoint_label`, `describe_fetch_failure` |
| `verdicts_cluster.py` | `k8s_workloads_verdict`, `extended_resource_verdict`, `ksm_resource_label`, `targets_verdict` |
| `verdicts_host.py` | `ups_health`, the `scrutiny_*` family, `pi_pressure` |
| `verdicts_service.py` | n8n streaks, `queue_warnings`, `indexers_down`, `gitops_alive`, the HA/Loki/Discord verdicts |

`gitops_status` is the one verdict that stays in `check.py`, because it reads
`GITOPS_BEHIND_MAX_S`; `gitops_alive` takes its threshold as an argument and moved. Its private
helper `_parse_behind` sits beside `gitops_status` rather than with the other verdicts, so the
only caller and the helper stay together.

**A function may move out of `check.py` only if it is never monkeypatched AND reads no patched
module-level name.** The test suite patches 208 times, always against the `check` module object.
A function reads globals from the module it is DEFINED in, so moving a patched function elsewhere
leaves the test patching a name nothing reads — the test then passes against unpatched production
code instead of failing. That is a silent loss of coverage, not a visible break, which is why the
rule is written here rather than left to be rediscovered.

`bridge_common.py` answers to the same rule twice over: it's imported by `check.py` like the
other split modules, AND it's imported by autofix-bridge's `autofix.py`, whose own suite
(`test_autofix.py`) patches `push` and `_request` directly. `_env`/`sanitize` are the only two
helpers that clear both bars — see `bridge_common.py`'s own header for the full account of what
was considered and rejected (`log`, `push`, `touch_heartbeat`, the urllib wrapper, each file's
`main()`).

Two consequences that look like arbitrary omissions and are not:

- **Config constants stay in `check.py`.** Beyond the patching rule, two tests call
  `importlib.reload(check)` to re-derive `PROM_ORIGIN` from the environment. `reload` does not
  re-execute an imported module's body, so a constant moved to a config module would leave those
  tests asserting a stale value.
- **The B2/R2 verdicts stay too.** They read their threshold constants as `None` fallbacks, so
  they are constant-coupled; splitting the storage domain would mean moving config with it.

**Adding a module means adding it to `monitor_bridge_modules`** in `defaults/main.yml` — the
ConfigMap ships exactly that list, and a module missing from it kills the pod at import with
`ModuleNotFoundError` on its next roll, on the one workload that cannot page about its own
failure. pytest cannot catch that: it imports from `files/` on disk and never reads the list.
`ansible/tests/test_monitor_bridge_modules.py` is what does.

## Editing & testing
- Manifests: `templates/deployment.yaml.j2`, `templates/env-secret.yaml.j2` · Logic:
  `files/check.py` plus the modules beside it (see *Module layout* above)
- Unit tests (parsing + every check's decision logic):
  `uv run pytest ansible/roles/k8s/monitor-bridge/files`.
  Also run automatically by the `pytest` prek hook (`prek run pytest --all-files`).
- Smoke test one pass:
  `sudo k3s kubectl -n homelab exec deploy/monitor-bridge -- python /app/check.py --once`
  (the readonly SA plain `kubectl` uses holds no exec verb)
- Deploy: `uv run ansible-playbook ansible/deploy.yml --tags "monitor-bridge"`

## Traps

### kube-state-metrics sanitizes resource names into labels
`kubectl describe node` prints `devic.es/dri`. kube-state-metrics emits
`kube_node_status_allocatable{resource="devic_es_dri"}` — every character outside
`[a-zA-Z0-9_]` becomes `_`. A query written with the Kubernetes name matches no series, on a
cluster where both nodes advertise the resource at capacity 4.

A check designed to fail closed on an absent series cannot tell "the resource is
deregistered" from "I asked the wrong question". Both return an empty vector. The 2026-08-20
extended-resource arm went DOWN every 5 minutes from 18:05 to 18:29 UTC with
`extended resource(s) advertised by no node: devic.es/dri — the device plugin is Running but
its resource is deregistered`, while `kubectl get nodes` showed `"devic.es/dri":"4"` on both
nodes. Fail-closed is right and is not the bug: it buys a blind check that pages instead of
going green, and it costs a typo that pages identically to a real fault.

Sanitize the configured name at query time and keep the operator-facing name the one
`kubectl` prints, so config matches the world and the query matches KSM. Name both forms in
the fault message — that is what makes the next mismatch diagnosable from the alert alone.
Before trusting a new metric-backed check, run its exact query against live Prometheus and
confirm it returns rows; the unit tests mock the payload, so they prove the verdict logic and
nothing about the selector. Guarded by `ksm_resource_label` in `files/check.py` and its test;
landed in PR #286 the same day PR #281 introduced it.

### Promtail's k8s streams have no `app` label
`kubectl` selects pods with `-l app=home-assistant`, so a LogQL selector written from that habit
reads naturally and matches nothing. Promtail's k8s stream carries
`container` / `pod` / `job` / `machine` / `namespace` / `service_name` / `stream` / `filename` —
no `app`. `LOKI_DOCKER_STREAM` already used `container=~".+"`; the ip_ban arm added 2026-08-23
did not follow it.

`HA_BAN_SELECTOR` shipped as `{namespace="homelab",app="home-assistant"}`, matched no stream, and
pushed `no ip_ban events in 1h` — through a window that provably contained
`Banned IP 10.42.0.1 for too many login attempts`. The arm fails open by design, so a wrong
question and a clean bill of health are the same output. Same shape as the
kube-state-metrics label trap above, and the same lesson: **a fail-closed check pages on a typo,
a fail-open check goes green on one.**

Unit tests could not catch it — they mock the payload, so they prove the verdict logic and
nothing about the selector. What caught it was running the selector against live Loki over a
window containing a KNOWN event, which is the only check that distinguishes "nothing happened"
from "nothing matched". Do that before trusting any new log- or metric-backed arm; a green first
cycle is not evidence. `LOKI_STREAM_LABELS` +
`test_loki_selectors_use_real_stream_labels` in `test_check_service.py` now pin the vocabulary
for all three Loki selectors.

### The bracketed log timestamps are Central time, not UTC
Log lines like `[2026-08-16T07:26:57] DOWN b2_reachable ...` carry the container's local
America/Chicago wall clock from the `TZ` env, while `kubectl logs --timestamps` prepends the
true UTC ingestion time. Verified 2026-08-16: bracketed `07:26:57` paired with kubectl's
`12:26:57Z`. Reading the brackets as UTC shifted a B2 cap-breach 5h early and pointed the
investigation at the wrong window — a "03:09 breach" that was really 08:09 UTC, minutes after
the 07:30 weekly reboot.

When building a timeline from monitor-bridge, or from any homelab container that sets
`TZ=America/Chicago`, pass `--timestamps` to `kubectl logs` and trust the prefix over the
app's own stamp. Cross-check one line against `date -u` before anchoring an incident timeline.
