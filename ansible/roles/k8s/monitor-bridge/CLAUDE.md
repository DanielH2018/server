# monitor-bridge — every threshold check, pushed to Uptime Kuma (k8s)

A stdlib-only Python loop (`files/cli.py`) that runs every registered check each `INTERVAL`
(300 s) and pushes `status=up|down&msg=…` to one Uptime Kuma **push** monitor per check, so a
threshold breach pages. It is the alert pipeline: it has no readiness probe and is denylisted
from auto-deploy, because a broken deploy cannot page about being broken.

**`files/registry.py`'s `build_checks()` is the authority on which checks exist.** This file
is prose; if a bullet below disagrees with the registry, the registry is right. The
measurements, incidents and retirements behind every number here are in
`docs/monitor-bridge-checks.md` — read that when you need to know *why* a threshold is what it
is, and this file when you need to know what the rule is.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's defaults, templates, tasks or containers_list entry, or the k3s role's Longhorn tier lists. -->
- **Deploy tag:** `--tags "monitor-bridge"`
- **Image:** `python` (`monitor_bridge_k8s_image`)
- **Route:** none (no `templates/ingressroute.yaml.j2`)
- **Claims:** none (no PVC)
- **Auto-deploy:** denylisted (`k8s_autodeploy: false`) — observability — this IS the alert
  pipeline; a broken deploy cannot page about being broken. ALSO no readinessProbe (probe-less)
  — two independent reasons
<!-- /generated_from -->

- **Stdlib only** (no build, no extra deps) · **No web UI** · **Depends on:** prometheus
- **Host:** daniel-box — pinned by `nodeSelector`, because the gitops checks read the
  deployer's state directory over a hostPath.
- **Reaches:** every source over its in-cluster Service name (`templates/env-secret.yaml.j2`)
  — no VIP, no Traefik, no gate in a probe's path. speedtest needs its own NetworkPolicy
  (`netpol-baseline`'s `networkpolicy-speedtest.yaml.j2`).

## Gates and hysteresis

Four reachability gates run first each cycle; each suppresses its dependents so one outage
pages once. A suppressed check pushes `up` with a `skipped — <source> unreachable` message so
its heartbeat stays alive. The sets live in `files/gates.py`, each pinned to the registry.

- **Prometheus Reachable** (`vector(1)`): when Prometheus is unreachable, every
  prom-dependent check (disk/cert/memory/restarts/oom/cpu/targets/traefik5xx/traefik_404/ups/
  host_temp/shipper_dropped/longhorn_volumes/snapshot_headroom/kubelet_plugin_readonly/pi_pressure) is
  **suppressed**. `tests/test_claude_md_prom_dependent_enumeration.py` pins that list to
  `PROM_DEPENDENT`; edit the set and the sentence together.
- **Loki Reachable** (`/loki/api/v1/labels`): gates `LOKI_DEPENDENT` — `loki_ingestion`,
  `swallowed_verdicts`, `kuma_notify_failures`. `ha_heartbeat` is deliberately NOT a member:
  its ban arm fails open on a Loki error so the heartbeat verdict survives.
- **B2 Reachable** (`b2_authorize_account`): gates `B2_DEPENDENT` (`b2_storage`). One probe
  per `B2_PROBE_INTERVAL_S` (1800 s) with a BILLED outcome cached, because the fault it
  detects is a transaction cap; a failure that never reached B2 (DNS, connect, timeout) takes
  the short `B2_TRANSPORT_RETRY_S` TTL so a transient cannot pin the tile DOWN for 30 minutes.
  A real cap denial arrives as `HTTPError`, never `RuntimeError` — a test faking it the other
  way proves the opposite of what it claims.
- **Cluster Prometheus Reachable**: gates `CLUSTER_DEPENDENT` (`k8s_workloads`,
  `cluster_targets`, `pvc_fullness`). Both Prometheus URLs render to the same cluster Service,
  so `run_once` reuses the first gate's verdict here; the split survives so a second
  Prometheus can be reintroduced, with membership following the URL a check reads. Do not
  read the pair as independent coverage.
- **`EXPORTER_DEPENDENT`** maps a node-exporter scrape job to the checks a dead exporter would
  otherwise page twice: `node` → disk, memory, host_temp; `node-pi` → host_temp, pi_pressure.
  `pvc_fullness` gets NO entry keyed on the kubelet job on purpose — its claim-count floor
  exists to page on that partial outage. A new node-exporter scrape job fails
  `test_every_node_exporter_job_is_mapped_in_exporter_dependent` until it is mapped.
- **`STARTUP_GRACE`** (`n8n`, `bazarr`, `prowlarr_indexers`, `scrutiny`, `r2_usage`,
  `speedtest`) holds a reach-out check with no gate and no streak of its own `up` for the
  first `GRACE_CYCLES`-1 consecutive down cycles, so the weekly Sunday reboot's first cycle
  does not page. The set is disjoint from every skip set, and a test holds both invariants.
- **Consecutive-cycle streaks** (`bridge/streaks.py`, every `*_CONSECUTIVE` in cycles of
  `INTERVAL`) are the per-check hysteresis. A held cycle pushes `up` with a `down streak n/N`
  note, because a monitor that is up while a fault accumulates has to say so. A streak delays
  a page; it never suppresses one.

## Checks

One bullet per registry entry, in registry order: source, rule, gate or streak, and the
switch that disables it. An empty credential or URL disables a check (it stays `up`); an
unreachable source pages through `_evaluate` unless a streak is named.

- **Root Disk** (`disk`): `node_filesystem_*` for `/`, `/boot` and `/boot/efi` on the two
  nodes, over `DISK_MAX_PCT` (the Pi is excluded by `HOST_METRIC_ORIGIN_EXCLUDE`; Pi Pressure
  owns it). Carries the **host coverage floor**: fewer than `HOST_ORIGINS_MIN` (2) origins
  means a lost host, not a healthy estate, and pages after `HOST_ORIGINS_CONSECUTIVE`. At 1
  the floor is inert — lower it for a planned single-node window and put it back.
- **TLS Cert Expiry** (`cert`): `traefik_tls_certs_not_after` under `CERT_MIN_DAYS`.
- **Memory** (`memory`): host `node_memory_*` over `MEM_MAX_PCT`, with the same coverage
  floor as Root Disk, plus the **Claude Code cgroups** arm (`with_claude_cgroups`): any
  increase in a `CLAUDE_CGROUP_EVENTS` counter (`max|oom|oom_kill|oom_group_kill`) pages at
  once, PSI `full` stall over `CLAUDE_CGROUP_STALL_MAX_PCT` pages, and a cgroup named in
  `CLAUDE_CGROUPS` that reports nothing pages. `high` is not alerted on — MemoryHigh
  throttling is the cap working. Empty `CLAUDE_CGROUPS` disables the arm.
- **Container Restarts** (`restarts`): `changes(container_start_time_seconds[RESTART_WINDOW])
  > RESTART_MAX`, by name.
- **Container OOM** (`oom`): `increase(container_oom_events_total[OOM_WINDOW])` by name.
- **CPU Throttling** (`cpu`): throttled/total CFS periods over `CPU_THROTTLE_PCT` AND
  throttled seconds/s over `CPU_MIN_THROTTLED_CORES`, by name, on the `CPU_CONSECUTIVE` (3)
  streak. The cores floor is load-bearing: the ratio alone runs 30–90% for tiny sidecars.
- **Scrape Targets** (`targets`): `up == 0` with `origin="daniel-server"`, naming the job.
- **Traefik 5xx** (`traefik5xx`): 5xx ratio over 5m per service, over `TRAEFIK_5XX_PCT`,
  behind the per-service `TRAEFIK_MIN_RPS` volume floor.
- **Traefik Latency** (`traefik_latency`): share of requests past the histogram bucket
  `TRAEFIK_SLOW_BUCKET` (5.0 s) over `TRAEFIK_SLOW_PCT`, per service, behind the same floor
  AND `TRAEFIK_SLOW_MIN_REQUESTS` (3) absolute slow requests. Never `histogram_quantile`:
  Traefik's buckets are 0.1/0.3/1.2/5.0/+Inf and a quantile between two is interpolated
  across an empty gap. Keep the bucket on a boundary Traefik emits — an `le=` matching no
  series is reported as a config fault. `TRAEFIK_STREAM_SERVICES` exempts long-lived
  connection services (headlamp, home-assistant, uptime-kuma) by service-label PREFIX, since
  the hash in `homelab-<name>-<hash>@kubernetescrd` moves on rename; the exempt count is
  named in the green message. bazarr and prowlarr are deliberately not exempt.
- **Traefik 404 Flood** (`traefik_404`): 404 share of ENTRYPOINT traffic over 5m past
  `TRAEFIK_404_PCT` (90), behind `TRAEFIK_MIN_RPS`. The entrypoint counter increments before
  routing, so it survives an edge that has lost every router — which emits no
  `traefik_service_*` series and reads clean to the per-service checks above.
- **n8n Prod Workflows** (`n8n`): per-active-workflow consecutive-failure streak from n8n's
  public API, accumulated across cycles because n8n saves no successful executions. `down` at
  `N8N_CONSECUTIVE_MAX` (3) for one workflow, or `N8N_SYSTEMIC_MAX` (2) workflows each at
  `N8N_SYSTEMIC_STREAK` (2). Streak state resets on a bridge restart. `N8N_API_KEY` empty
  disables.
- **Arr Queue Warnings** (`arr_queue`): sonarr's and radarr's `/api/v3/queue` — `down` on
  any item with `trackedDownloadStatus == "warning"`, `importBlocked`, or `importPending`
  carrying `statusMessages`, naming the release. Each API key is independent; both empty
  disables. The FETCH rides `ARR_FETCH_CONSECUTIVE` (3) because a rolling *arr refuses
  connections; a queue item pages on the cycle it is seen.
- **Bazarr Health** (`bazarr`): `/api/system/status` + `/api/system/health` — `down` when a
  peer version field (`sonarr_version`/`radarr_version`) is present but empty (a rejected key
  seen from outside) or bazarr self-reports a health issue. An ABSENT field is ignored. Header
  is `X-API-KEY`. Bazarr holds its own copies of the *arr keys on its PVC, so a key rotation
  reaches it only by hand — this check is what notices.
- **Prowlarr Indexers** (`prowlarr_indexers`): `down` only when an indexer's own
  `initialFailure` is older than `PROWLARR_INDEXER_MIN_DOWN_MIN` (1 week); age-based so it
  survives a bridge redeploy. `PROWLARR_INDEXER_IGNORE` drops chronically flaky public
  trackers. Pairs with Prowlarr's `includeHealthWarnings=false`.
- **GitOps Deploy — Alive** (`gitops_alive`): `/gitops-state/last_run` older than
  `GITOPS_MAX_AGE_MIN` is `down`. The deployer pushes nothing to Kuma itself.
- **GitOps Deploy — Status** (`gitops_status`): four arms over the deployer's markers,
  reported in urgency order — a non-empty `hold_sha`; a `diverged_sha`; `contention_since`
  older than `GITOPS_CONTENTION_MAX_MIN` (30); `behind_since` older than
  `GITOPS_BEHIND_MAX_MIN` (360) — then `manual_plane` LAST, once its oldest line is older
  than the same six hours, because a pending role blocks nobody where a stopped deployer
  blocks every landing. Age-gated, not presence-gated: a routine push is behind for one tick.
  Parsers come from `gitops_markers.py`, a generated copy of the deployer's module
  (`scripts/dev/gen_gitops_markers.py`). An unparseable marker reads as not-behind.
- **Staging Backfill — Alive** (`staging_backfill`): the backfill ratchet's `ExecStopPost`
  heartbeat off the same hostPath; `OnFailure=` covers a run that failed, this covers runs
  that stopped happening.
- **etcd Restore Drill** (`etcd_restore_drill`): the weekly drill's stamp, read fail-closed —
  a stale or failing stamp is `down`, since etcd holds the Longhorn CRs needed to find every
  volume backup.
- **SMART Data / Health** (`scrutiny`): every non-archived device needs a `collector_date`
  within 26 h AND `device_status == 0`; no devices at all is `down`. `SCRUTINY_TEMP_MAX` (0 =
  off) adds a temperature ceiling; the wear arm pages on NVMe `percentage_used` over
  `SCRUTINY_WEAR_MAX` (80), read per device after freshness passes, and a device with no wear
  field is named as unwatched, never as healthy or as a fault. This is the only drive-failure
  alert path — Scrutiny's own notifier is unconfigured.
- **Host Temperature** (`host_temp`): `node_hwmon_temp_celsius`, drives excluded
  (`HWMON_TEMP_EXCLUDE_CHIP` — SMART owns them). Every sensor gets a limit from one of two
  exhaustive arms: `HWMON_TEMP_RATIO` (0.90) of a plausible declared max (preferred) or crit,
  else `HWMON_TEMP_FALLBACK_C` (85); an implausible declared value is treated as undeclared
  (the `DECIDED:` in `verdicts/host.py:hwmon_temp_limits`), and `HWMON_TEMP_RATED_MAX_C`
  seeds daniel-box's k10temp/Tctl with AMD's published 100 °C. An EMPTY vector is `down`.
  `HWMON_TEMP_CONSECUTIVE` (12) is one streak for the whole check, derived at the
  `DECIDED: 12 cycles` marker in `files/bridge/config_host.py`; `HWMON_TEMP_RATIO` is
  estate-wide, not the knob for one sensor's duty cycle. Fewer than
  `HWMON_TEMP_ORIGINS_MIN` (3, a literal — raise it when a node joins) reporting hosts pages
  after `HWMON_TEMP_ORIGINS_CONSECUTIVE` (5). Two more arms, own streaks: **Undervoltage**
  (`node_hwmon_in_lcrit_alarm_volts`, judged first, no grace, gated on `up{job="node-pi"}` so
  a Pi off the network defers rather than reads green) and **CPU thermal throttling**
  (`node_cooling_device_cur_state{type="Processor"}`, 3 cycles, origins floor 2 because the Pi
  publishes none). `verdicts/host_power.thermal_monitor_verdict` composes the four arms.
- **UPS Battery Health** (`ups`): nut-exporter primary, HA's re-export as the in-query
  fallback (`max(A) or max(B)`). Mains loss (`ups.status{flag="OB"}`) is judged first and
  returns alone; otherwise charge under `UPS_CHARGE_MIN_PCT` (50), runtime under
  `UPS_RUNTIME_MIN_S` (300) or the replace-battery flag pages. All arms absent defers to
  Scrape Targets; both numeric arms absent with replace present defers to the `nut`
  healthcheck; any other partial absence is a rename and pages. `UPS_CONSECUTIVE` (2). All
  four queries empty disables.
- **Pi Pressure** (`pi_pressure`): the Pi's node-exporter series (`origin=PI_ORIGIN`) —
  `node_load5` per core over `PI_LOAD_MAX`, `MemAvailable` under `PI_MEM_MIN_MB`, any block
  device over `PI_DISK_MAX_PCT` (keyed by device, tmpfs excluded); an absent series while
  Prometheus answers pages. It owns Pi disk and memory — `check_mem` on the Pi would be a
  strictly weaker duplicate. **Published-port arm** (`with_pi_ports`): TCP-connects to every
  `PI_PUBLISHED_PORTS` entry (rendered from the Pi's `containers_list`) on
  `PI_PORTS_CONSECUTIVE` (2), naming each dead port with the recreate hint — a container back
  from a reboot with no network still reads `Up (healthy)`. It adds a named port, not coverage.
- **Home Assistant Automations** (`ha_heartbeat`): `input_datetime.ha_heartbeat` stamped by
  a `/1min` automation, `down` past `HA_HEARTBEAT_MAX_AGE` (300 s) on `HA_CONSECUTIVE` (2);
  an unreachable API rides the same streak, since both are the deploy. **ip_ban arm**
  (`with_ha_ban`): a `Banned IP` line in Loki over `HA_BAN_WINDOW` (1h) pages, skipping the
  streak. It watches the ban EVENT, not the state — the page self-clears while the entry is
  still in `/config/ip_bans.yaml`, so read that file by hand when the Discord notification
  fires. The token is HA's Long-Lived Access Token, file-mounted (`HA_TOKEN_FILE`).
- **k3s Speedtest** (`speedtest`): newest `/api/v1/results` row — status, then age
  (`SPEEDTEST_MAX_AGE_H`, 8 h against a 6 h schedule, the arm that notices a dead scheduler),
  then download under `SPEEDTEST_DOWNLOAD_MIN_MBPS` (100, because results are bimodal by Ookla
  server). Hysteresis on the fetch only (`SPEEDTEST_CONSECUTIVE`), never on the verdict.
- **Loki Log Ingestion** (`loki_ingestion`): three freshness arms, `down` if ANY is silent —
  the file-tail union `LOKI_STREAM` over `LOKI_FILETAIL_WINDOW` (deployed selector is
  `authlog|syslog`; traefik is NOT covered), the docker stream `LOKI_DOCKER_STREAM` over
  `LOKI_WINDOW` (30m), and the Pi's `LOKI_PI_STREAM` over the tolerant window.
- **Log Shipper Dropped Entries** (`shipper_dropped`): the larger of the client-side
  `loki_write_dropped_entries_total` and Loki's own `loki_discarded_samples_total` over
  `SHIPPER_DROPPED_WINDOW`, past `SHIPPER_DROPPED_MAX` (3000, derivation at the `DECIDED:` in
  `bridge/config_io.py`), naming the server-side reason. `too_far_behind` after a shipper
  restart is a benign re-tail: correlate `process_start_time_seconds{job=~"alloy.*"}` before
  reading it as loss, and land a shipper change more than an hour before pointing this at it.
  Both counters are matched by `__name__` regex so a rename cannot read as 0 forever.
- **Swallowed Push Verdicts** (`swallowed_verdicts`): a host cron's `status=down` line whose
  push `kuma-push-lib.sh` then lost, read from Loki over `SWALLOWED_VERDICTS_WINDOW_S` (3h)
  through `bridge.net.loki_lines` (plus `SWALLOWED_VERDICTS_POD_LOGQL` for pi-peer-backup's
  pod). Pages only when some OTHER tag landed a push in the window; nothing landing is
  fleet-wide and named to the edge/host tiles instead. A swallowed `up` is not counted.
- **Kuma Notification Delivery** (`kuma_notify_failures`): Kuma's `Cannot send notification
  to <name>` lines over `KUMA_NOTIFY_FAILURES_WINDOW_S` (3h), the reason from the next line
  reduced to its HTTP status (the axios message can carry the webhook secret). The window is
  the whole hysteresis. Its tile notifies EMAIL as well as Discord.
- **Discord Delivery** (`discord`): GET-verifies all five Discord webhooks (Kuma's, CrowdSec's,
  gitops-deploy's, the *arrs', healthchecks') and names any invalid one; a GET posts nothing.
  Plus `email_backstop`: a throttled Gmail SMTP login with Kuma's own creds, cached on success
  for `EMAIL_PROBE_INTERVAL_S` (6h). `DISCORD_CONSECUTIVE` (2). It proves deliverability, not
  that Kuma still has the notification attached — AutoKuma re-applies that on every deploy.
- **R2 Free Tier Headroom** (`r2_usage`): one GraphQL POST for month-to-date storage, Class A
  and Class B ops against the free tier, `down` past `R2_USAGE_MAX_PCT` (80) on any arm or
  past `R2_UPLOADS_MAX` (25) incomplete multipart uploads. An unlisted actionType counts as
  Class A and is named. Successes cached for `R2_PROBE_INTERVAL_S`, failures not — the inverse
  of B2, because analytics calls are free. A 200 carrying `errors` is raised. The query filters
  by `bucketName`; with a second bucket drop the filter rather than raise the thresholds.
- **Cloudflare IP Drift** (`cloudflare_ips_drift`): the two published range pages against
  `CLOUDFLARE_IPS_EXPECTED`; a success is cached for a day, drift or a short page is `down`
  and re-probed every cycle, so the tile clears one cycle after `cloudflare_ips` is fixed.
- **B2 Storage Usage** (`b2_storage`): B2 queried live behind the B2 gate, so a cap incident
  pages once.
- **k3s Workload Health** (`k8s_workloads`): kube-state-metrics through the cluster
  Prometheus. Fails closed: fewer than `K8S_MIN_WORKLOADS` (5) Deployment or
  `K8S_MIN_DAEMONSETS` (9) DaemonSet series reads UNKNOWN, never OK. Arms: unavailable
  replicas on `K8S_WORKLOADS_CONSECUTIVE` (3) — the ONLY arm with grace; a stalled rollout
  (`updated < spec`) on `K8S_ROLLOUT_STALL_CONSECUTIVE` (3); zero available replicas on
  `K8S_ZERO_AVAILABLE_CONSECUTIVE` (2); crash-looping restarts (`increase()` over
  `K8S_RESTART_WINDOW` past `K8S_RESTART_MAX` AND a restart inside
  `K8S_RESTART_RECENT_WINDOW`, so a recovered pod leaves the arm); and every
  `K8S_EXTENDED_RESOURCES` name still allocatable on some node, through `ksm_resource_label()`
  (INERT and named without the `nodes` collector). `svc(0/1)` messages take the desired count
  from a second query, because a PromQL `<` returns the left side alone.
- **Cluster Scrape Targets** (`cluster_targets`): `up{origin!="daniel-server"}`, the complement
  of Scrape Targets, `CLUSTER_TARGETS_MIN` (3) floor, `CLUSTER_TARGETS_CONSECUTIVE` (3).
- **Longhorn Volume Redundancy** (`longhorn_volumes`): `longhorn_volume_robustness` —
  `degraded` or `faulted`, named by PVC, on `LONGHORN_CONSECUTIVE`; the absent-metric branch
  pages, which is why it is prom-dependent.
- **k3s PVC Fullness** (`pvc_fullness`): `kubelet_volume_stats_available_bytes /
  _capacity_bytes`, `max by (namespace, persistentvolumeclaim)` because daniel-box's claims
  are scraped under two jobs, past `PVC_MAX_PCT` (85 — a full PVC needs an expand, not a
  delete) with no grace; a named `PVC_MIN_FREE` (`<pvc>=<bytes>`) floor pages below the
  claim's own declared transient whatever the percentage. `PVC_EXCLUDE` drops `media-data`
  (a local PV that IS `/`). The census floor `PVC_MIN_CLAIMS` (32) is DERIVED: the apiserver
  job alone answers 27, so any lower floor reads a dead kubelet job as healthy; it rides
  `PVC_CLAIMS_CONSECUTIVE`.
- **Longhorn Snapshot Headroom** (`snapshot_headroom`): `longhorn_snapshot_actual_size_bytes`
  summed per volume (deduped by snapshot) against the caps DECLARED in `SNAPSHOT_CAPS`
  (`<pvc>=<bytes>`), on `SNAPSHOT_CAP_CONSECUTIVE`. Nothing exports the cap, so
  `tests/test_check_snapshot_headroom.py` derives the capped set from the tree and fails on an
  undeclared one; `"0"` is uncapped and dropped; a declared cap with no series is a breach.
- **Kubelet CSI Mount Read-Only** (`kubelet_plugin_readonly`):
  `node_filesystem_readonly{mountpoint=~"/var/lib/kubelet/plugins/.*"} == 1`, no grace, over
  `host_metric_sel` so both nodes are covered. An absent series is healthy — a dead exporter
  is Scrape Targets' page — and `ansible/tests/k8s/test_node_exporter_filesystem_exclusion.py`
  guards the exclusion regex that would otherwise hide the fault again.

## Push-monitor mechanics

- **Every push monitor has `max_retries=0`** so the bridge's own `down` push flips the state
  and the descriptive message reaches Discord; with retries Kuma parks the push in PENDING
  and the watchdog's "No heartbeat" replaces it. Post-boot flapping is fixed by widening the
  heartbeat window, never by adding retries (`test_push_monitors_never_retry` is the guard).
- **The heartbeat window is `kuma_bridge_push_interval` = 1200 s** (4 × the loop): three missed
  pushes are absorbed and a dead bridge still pages, 20 min in.
- **Liveness probe:** `cli.py` touches `/tmp/heartbeat` after every cycle and the probe in
  `templates/deployment.yaml.j2` fails past ~3×INTERVAL, so the kubelet restarts a hung loop.
- **Push tokens:** `templates/env-secret.yaml.j2`'s `KUMA_PUSH_*` keys are the list, one
  SOPS `monitor_bridge_<check>_push_token` each; `test_every_push_token_env_is_wired_to_a_monitor`
  pins them to the AutoKuma monitors and `test_checks_and_env_secret_push_tokens_agree` to the
  registry. Count them with `grep -c '^\s*KUMA_PUSH_[A-Z0-9_]*:'` on that template rather
  than trusting a number written here.
- **Folding an arm into an existing monitor is the default** over a new tile, which costs a
  new push token in SOPS and a monitor created by hand. Fold when the arm answers the tile's
  existing question (cgroups into Memory, ports into Pi Pressure, ip_ban into HA); the arm
  wins the message and keeps the host tile's text after it.
- **A new arm ships with its selector run against the live source over a window holding a
  KNOWN event.** Unit tests mock the payload and prove the verdict, never the selector; a
  fail-open arm goes green on a typo and a fail-closed one pages on it (*Traps*).
- **File-mounted credentials** (`HA_TOKEN_FILE`, `B2_PROBE_APPLICATION_KEY_FILE` reusing
  `longhorn_b2_application_key`, `CF_ANALYTICS_TOKEN_FILE`) are rendered 0600 and read through
  `bridge.config._env_file`; an empty file disables the check. Ids stay inline.
- **The gitops checks read `/var/lib/gitops-deploy` over a `:ro` hostPath** the
  `gitops_deploy` role creates: deploy that role before this one on a fresh host.
- Thresholds are env-tunable in `templates/env-secret.yaml.j2`; `bridge/config*.py` names the
  default for each. A failed query makes that monitor `down` with an explanatory msg.

## Operator prerequisites
1. A push token in `secrets.yml` for every `KUMA_PUSH_*` key in `templates/env-secret.yaml.j2`
   — exactly 32 alphanumeric chars (`openssl rand -hex 16`); AutoKuma silently refuses the
   monitor otherwise (`Invalid push_token`).
2. `n8n_api_key`: minted in n8n → Settings → n8n API, scoped to read Workflow + Execution.
3. `sonarr_api_key` / `radarr_api_key` / `bazarr_api_key` / `prowlarr_api_key`: the apps'
   own keys; re-derive from `/config/config.xml` inside the pod if needed.
4. `cloudflare_analytics_token`: a Custom token with exactly **Account → Account Analytics →
   Read** — never write or R2 permissions, which would let the bridge hard-stop the bucket it
   guards; the monitor pages and a human decides. `secret_rotation.py sync`, then smoke-test
   with `--once`. The bucket's 7-day `AbortIncompleteMultipartUpload` lifecycle rule is set by
   hand (`wrangler r2 bucket lifecycle add`); the uploads arm notices it missing.
5. `monitor_bridge_ha_token`: an HA Long-Lived Access Token (Profile → Security), tier
   `assisted`.
6. Notifications attach automatically through the `kuma()` macro's `notification_name_list`.

## Module layout — and the one rule that governs it

`files/` holds four flat modules and three packages: `bridge/` (the plumbing every check
shares), `checks/` (one module per domain of `check_*` bodies, mirroring the test file for
that domain) and `verdicts/` (pure logic taking its inputs as arguments). `registry.py` and
`gates.py` import `bridge.types` and the `checks.*` bodies and never each other or `check`. A
module imports a sibling by package (`from bridge.config import Config`): `/app` is
`sys.path[0]` in the pod and `files/` is on `pythonpath` under pytest. No `__init__.py`.
Modules split at a 600-line cap.

**Adding a module means adding its path to `monitor_bridge_modules`** in `defaults/main.yml`
(`checks/newdomain.py`, shipped as the flat ConfigMap key `checks_newdomain.py` and mounted
back at its path by the Deployment's `items:`). A module missing from it kills the pod at
import on its next roll, on the one workload that cannot page about its own failure, and
pytest cannot see it because it imports from `files/`.
`ansible/tests/services/test_monitor_bridge_modules.py` and
`test_monitor_bridge_mount_layout.py` beside it are what do.

| module | holds |
|---|---|
| `cli.py` | the `argparse` front end and `main(argv, env, checks, gate_config) -> int`, which builds the `Config`, the registry and the `Gates`, validates the check filter and loops `run_once` |
| `check.py` | `run_once(cfg, checks, gates, dry_run, only)` — the run loop, and nothing else |
| `registry.py` | `build_checks(env)`, every `Check` with its `KUMA_PUSH_*` name read from the environment it is handed |
| `gates.py` | the five `*_DEPENDENT` sets, `STARTUP_GRACE`, `GATE_DEPENDENTS`, `check_enabled`, `validate_check_filter`, `expand_gates_for_cli`, `down_exporters`, `_evaluate`, `_gate`, and the frozen `Gates` seam `run_once` reads every gate fact through |
| `bridge/types.py` | `Check`, `CheckResult`, `CheckFn` — shared by `registry.py` and `check.py` without either importing the other |
| `checks/<domain>.py` | the `check_*` bodies by domain: `service`, `gitops`, `notify`, `logs`, `cluster` (+ `cluster_rollout`, `cluster_zero`), `host`, `host_thermal`, `host_edge`, `b2`, `r2`, `cloudflare_ips`, `storage`. `checks/gitops.py` holds `gitops_status` beside its check — the one verdict that reads `cfg` itself — and its parsers come from `gitops_markers.py`, the generated copy of the deployer's module. `host_edge`'s entry points take the prober as `tcp_open` so a test injects a port map |
| `bridge/config.py` + `config_{host,service,cluster,io}.py` | the `_env`/`_int`/`_num`/`_env_file` parsers, `class Config(HostConfig, ServiceConfig, ClusterConfig, IoConfig)`, `load_config(env)`; one builder per domain. `K8S_EXTENDED_RESOURCES` and `PVC_EXCLUDE` stay in `config.py` because a repo test greps for them by text |
| `bridge/net.py` | `_get_json`, `_post_json`, `prom_scalar`, `prom_vector`, the `loki_*` queries, `push` with `cap_push_msg` (`PUSH_MSG_MAX`), and the selector builders (`origin_sel`, `cadvisor_sel`, `host_metric_sel`). Every helper that reads a URL or the origin pin takes `cfg` FIRST |
| `bridge/msgfmt.py` | `format_down(unit, state, items, details)` — the one grammar for a push message naming several things; import-free so `probe.py releases --kuma` loads it from a host too |
| `bridge/streaks.py` | `down_streak` (the consecutive-down counter four domains share, cleared by `conftest.py`) and `apply_startup_grace` |
| `bridge/common.py` | `_env`, `sanitize` — the two helpers shared verbatim with autofix-bridge's `autofix.py`; its header records what was considered and rejected |
| `bridge/parsing.py` | duration/timestamp parsing, `endpoint_label`, `describe_fetch_failure` |
| `verdicts/<domain>.py` | pure decisions taking every threshold as an argument: `cluster`, `host` (`hwmon_temp_limits`, `pi_pressure`), `host_smart` (`scrutiny_*`), `host_power` (`ups_health`, `thermal_monitor_verdict`), `host_cgroups`, `service`, `logs`, `notify`, `storage` (the B2/R2 decisions with the R2 Class A/B/free ACTION LISTS beside them — policy, not configuration) |

## Configuration is a parameter, not a module global

`cli.py`'s `main()` calls `load_config(os.environ)` ONCE and hands the frozen `Config` to
`check.run_once`, which passes it to every gate, every check body (`Check.fn(cfg)`) and every
`bridge.net` helper that reads a URL; the registry (`build_checks(env)`) and the `Gates` are
built there too and passed in, so a test states them rather than patching tables. Building the
config MUST NOT raise: `_int`/`_num` record a malformed value in
`bridge.common.CONFIG_PROBLEMS`, keep the default, and `main()` reports each and exits 2.

- **A default argument cannot read the config** — defaults evaluate at import. Default to
  `None` and resolve `cfg.X` in the body.
- **A test states its configuration**: the `cfg` fixture is `load_config({})`; narrow it with
  `dataclasses.replace(cfg, X=...)`, or call `load_config({...})` when the READ is under test.
- **A `verdicts/` module reads no `cfg`** and takes every threshold as an argument.
- **A test patches the module that READS the name, and a module reads it qualified**
  (`bridge.net._get_json` at call time, never `from bridge.net import _get_json`). Getting it
  wrong is silent, so `ansible/tests/services/test_monitor_bridge_modules.py` re-derives every
  patched `(module, name)` pair by AST and `test_bridge_patch_boundary.py` beside it fails a
  runtime module that from-imports a patched name.
- Mutable per-check state stays with the code that mutates it; `_down_streaks` has its own
  module only because four domains mutate it.

## Editing & testing
- Manifests: `templates/deployment.yaml.j2`, `templates/env-secret.yaml.j2` · Logic:
  `files/cli.py` plus the modules beside it (*Module layout* above)
- Unit tests: `uv run pytest ansible/roles/k8s/monitor-bridge/tests`, one file per domain
  (`test_check_<domain>.py`); put a new test with the domain it exercises.
- **A shared test helper goes under `tests/`, never beside `cli.py`** — every `.py` in
  `files/` is production code to `_runtime_modules()`. A fixture goes in `tests/conftest.py`;
  a helper taking arguments goes in an underscore-prefixed, repo-unique module
  (`_check_gate_helpers.py`), never `from conftest import ...`.
- Smoke test one pass:
  `sudo k3s kubectl -n homelab exec deploy/monitor-bridge -- python /app/cli.py --once`
  (the readonly SA holds no exec verb). `--dry-run` evaluates every check and pushes nothing,
  so a hand run cannot overwrite a live monitor's state; `--check <name>` narrows it, validated
  like `CHECKS_ONLY`, including the refusal to run a gated check without its gate.
- Deploy: `./scripts/deploy.sh --tags "monitor-bridge"`

## Traps

Each rule here came from an incident; `docs/monitor-bridge-checks.md` holds the evidence.

- **kube-state-metrics sanitizes resource names into labels:** `devic.es/dri` is emitted as
  `resource="devic_es_dri"`. A fail-closed check cannot tell "deregistered" from "wrong
  question" — both are an empty vector — so sanitize at query time (`ksm_resource_label`),
  keep the operator-facing name the one `kubectl` prints, and name both forms in the message.
- **Promtail's k8s streams have no `app` label** — they carry `container` / `pod` / `job` /
  `namespace` / `service_name`. A selector written from the `-l app=` habit matches nothing,
  and a fail-open arm reads that as a clean bill of health. `LOKI_STREAM_LABELS` and
  `test_loki_selectors_use_real_stream_labels` pin the vocabulary; a green first cycle is not
  evidence.
- **The runtime stamps the log lines; `bridge.common.log` does not.** Pass `--timestamps` to
  `kubectl logs` and read the prefix as UTC. A line archived before 2026-08-16 carries a
  bracketed America/Chicago stamp — any container with `TZ=America/Chicago` that stamps its
  own lines has the same trap, so cross-check one line against `date -u` before anchoring a
  timeline on it.
