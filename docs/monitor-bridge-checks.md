# monitor-bridge checks: the per-check record

The measurements, incidents and retirements behind every threshold check the
`monitor-bridge` pod pushes to Uptime Kuma. The per-check rule — what it reads, the threshold
and the arms — lives in `files/registry.py` and the `verdicts/` function each entry names;
`ansible/roles/k8s/monitor-bridge/CLAUDE.md` carries what holds across all of them (the gates,
the hysteresis, the module layout) and is what a session loads before touching the role. This
page is where the numbers came from and what each arm was added after, and its *Live checks*
walks the registry in order.

`files/registry.py`'s `build_checks()` is the authority on which checks exist. Nothing tests
this page: a bullet here can describe a check that has since moved or been retired, and the
*Retired and moved checks* section is exactly that record. If a bullet disagrees with the
registry, the registry is right.


## How the bridge got here

> **THE bridge since the Docker uninstall (2026-08-14).** Born as the daniel-server
> sidecar, split at the Phase F drain, whole again in-cluster: this role's
> `files/` runs every check, entered at `files/cli.py`. The GitOps pair reads daniel-box's own deployer
> via a hostPath (the pod is pinned there); `disk_prune` retired with the Docker daemon;
> `pi_peers` and `renovate_alive` dissolved into direct pushers at the host flips
> (k8s/pi-peer-backup CronJob; `renovate-notify`'s ExecStartPost). check.py still
> refuses a CHECKS_ONLY/CHECKS_SKIP filter naming an unknown check or a gated check
> without its gate, and `test_checks_and_env_secret_push_tokens_agree` asserts the
> env-secret carries exactly the token set the code reads. Much of the per-check
> documentation below predates the moves — Docker-era plumbing details (compose, bind
> mounts, networks) are history: `git show 2460d0675fd748e70fcbcde87185371ffd62402b:ansible/roles/containers/archive/monitor-bridge/`.
>
> **`files/registry.py`'s `build_checks()` is the authority on which checks exist.** This file
> is prose and nothing tests it: until 2026-08-16 the three retired above were still written up
> here in the present tense, as live checks with unit-tested pure functions, four weeks after
> the functions were deleted. If a bullet below disagrees with the registry, the registry is
> right.

A tiny sidecar that turns host-cron state files into Uptime Kuma **push** monitors, so
threshold problems actually page. See repo-root `CLAUDE.md`. (The kopia backup checks
retired with kopia on 2026-08-10 — the backup plane is Longhorn;
`backup-consolidation-longhorn.md`.)

## Live checks

gates (`prometheus`, `loki_reachable`, `b2_reachable`, `cluster_prometheus`) and pushes
`status=up|down&msg=…` to one Kuma push monitor each:
- **Prometheus Reachable** (a trivial `vector(1)` instant query — the root-cause GATE for the
  prom-dependent checks. Evaluated FIRST each cycle: when Prometheus is unreachable, every
  prom-dependent check (disk/cert/memory/restarts/oom/cpu/targets/traefik5xx/traefik_404/ups/
  host_temp/shipper_dropped/longhorn_volumes/snapshot_headroom/kubelet_plugin_readonly/pi_pressure) is
  **suppressed** — pushed `up` with a "skipped — Prometheus unreachable" `msg` so their push-monitor
  heartbeats stay alive — and only THIS monitor pages. Without the gate one Prometheus outage
  fires all of them at once: one root cause, one page per dependent check. A single scrape
  target down (Prometheus up, one exporter gone) still surfaces separately on Scrape Targets.
  The `PROM_DEPENDENT` set is guarded by a test against the live `CHECKS` so it can't drift,
  and `tests/test_claude_md_prom_dependent_enumeration.py` pins the enumeration above to that
  set so this prose can't drift either. It carried a hardcoded count of ten while the set held
  more (#1359); the enumeration replaces the count because a count nothing reads goes stale
  silently, and this one had.)
- **Root Disk** (`node_filesystem_*` for `/`, `/boot` **and `/boot/efi`** — old kernels
  filling /boot quietly breaks upgrades, and a full ESP breaks firmware/bootloader
  updates the same way; server-only, the Pi's disk lives in the Pi Pressure check)
- **TLS Cert Expiry** (`traefik_tls_certs_not_after`) — per-series, so the message names each
  breaching certificate's `cn`. It read `min(traefik_tls_certs_not_after)` until 2026-10-01,
  which aggregated the labels away and left the tile promising a name the message could not
  carry (#3101). Latent while Traefik's store holds the one wildcard; a page naming nothing is
  what a second certificate would have produced.
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
- **Claude Code cgroups** — not a monitor of its own either, but a third arm inside **Memory**
  (`with_claude_cgroups`, issue #1258). It reads the `claude_cgroup_*` node-exporter textfile
  series PR #1251 added for `claude-rc.service` and `user.slice/user-1000.slice`, on every
  host with `has_claude_code: true` (daniel-box and daniel-server). Because both hosts emit
  the same cgroup names, the two queries group by `(origin, cgroup)` / `(origin, cgroup,
  event)` and the verdict's message names `origin/cgroup` (for example `daniel-server/fleet 40.0%`)
  rather than the cgroup alone.
  Folded here rather than given its own tile for the reason recorded at `with_pi_ports`: a new
  Kuma monitor costs a new push token in SOPS and a monitor created by hand in the UI, and
  **Memory** already owns "this box is running out of memory" — which a cgroup taking the box
  is. On 2026-09-05 (#1243) that cgroup held all 8 GiB of this box's swap plus 6.96 GB anon,
  stalled in reclaim for about ten minutes, before anything downstream failed.
  Three sub-arms, reported in this order: a watched `memory.events` counter increasing
  (`CLAUDE_CGROUP_EVENTS` = `max|oom|oom_kill|oom_group_kill`, paging on ANY increase — a kill
  outranks a stall), the PSI `full` stall rate over `CLAUDE_CGROUP_STALL_MAX_PCT`, and an
  expected cgroup not reporting at all. `high` is deliberately NOT alerted on: MemoryHigh
  throttling is the cap working, and its value belongs to `roles/setup/claude_code`, so an arm
  keyed on it would move whenever those caps move. It is graphed instead, on the
  `AI/claude-code-host-cgroups` board, which carries an `origin` template variable so either
  host's curves can be isolated.
  `CLAUDE_CGROUPS` (rendered in `templates/env-secret.yaml.j2`, `claude-rc,fleet`) is the set
  whose ABSENCE is a fault, not the set that is judged — the queries filter by metric, so
  `user-1000-slice` is watched whenever it exists and its absence never pages, because its
  cgroup only exists once somebody has logged in since boot. **Empty disables the whole arm**; the full threshold derivation, settled 2026-09-11 against 5.8 days of
  history that included both cgroups hitting their MemoryHigh caps (#1288), is at
  `CLAUDE_CGROUP_STALL_MAX_PCT` in `bridge/config_host.py`.
- **Container Restarts** (`changes(container_start_time_seconds[15m]) > RESTART_MAX`)
- **Container OOM** (`increase(container_oom_events_total[1h]) by (name)` — names the
  offender; supersedes the old host-aggregate OOM that lived in the Memory check)
- **CPU Throttling** (throttled/total CFS *periods* `> CPU_THROTTLE_PCT` **and** throttled
  *seconds*/s `> CPU_MIN_THROTTLED_CORES`, by name — catches a container pinned at its
  `deploy.resources` CPU cap, which throttles silently without OOM/restart/5xx. The cores
  floor (same volume-floor idea as Traefik's `TRAEFIK_MIN_RPS`) is essential: the period
  ratio alone runs 30–90% for tiny low-limit sidecars that briefly burst over their slice
  while losing negligible absolute CPU — a perpetual false `down`, which Kuma renders as
  "No heartbeat in the time window" since only `up` pushes satisfy a push monitor's
  watchdog. Unlimited containers give 0/0→NaN and are ignored. On top of both gates,
  `CPU_CONSECUTIVE` (3) adds hysteresis: only the third consecutive breaching cycle
  (~15 min) pushes `down`; shorter bursts push `up` with a "throttling streak n/3"
  `msg` naming the offender, and a clean cycle resets the streak — so one-cycle blips
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
  every service at once.
  **Two guards were added 2026-09-11 after this became the flappiest monitor in the estate** —
  51 DOWN episodes over 30 days, more than any other, and a live DOWN that day naming headlamp.
  They are independent faults and neither fixes the other.
  `TRAEFIK_STREAM_SERVICES` (`homelab-headlamp-`, `homelab-home-assistant-`,
  `homelab-uptime-kuma-`) exempts services whose traffic is LONG-LIVED CONNECTIONS. Traefik's
  histogram times a request until the response completes, so a websocket, an SSE stream or a
  Kubernetes watch sits past every bucket edge while perfectly healthy — and no
  `TRAEFIK_SLOW_BUCKET` fixes that, because the buckets end at 5.0s and a stream outlives all of
  them. Measured 2026-09-11: headlamp ran 94.2% of requests under 0.1s and 3.1% past 5.0s, with
  a whole-service mean of 1.7s, which puts that tail's own mean near 53s. Matched as a PREFIX of
  the service label, which is `homelab-<name>-<hash>@kubernetescrd` — the hash moves when an
  IngressRoute is renamed, so never match the full label. The exempt count is named in the green
  message, because an exempt service is not measured and a bare "ok" would overstate coverage.
  **bazarr and prowlarr are deliberately NOT exempt** despite being the other two names in the
  30-day offender history: an *arr indexer search genuinely takes 5-30s and a user genuinely
  waits for it, which is exactly the user-visible slowness this check exists to catch.
  `TRAEFIK_SLOW_MIN_REQUESTS` (3) requires an absolute count of slow requests behind the ratio.
  `TRAEFIK_MIN_RPS` admits a service with 0.05 x 300 = 15 requests in the `[5m]` window, so ONE
  request past the bucket is 6.7% — already over `TRAEFIK_SLOW_PCT`. The ratio alone therefore
  pages on a single request for every low-traffic route. Derived from the window and the floor,
  not fitted to an observed service.)
- **Traefik 404 Flood** (404 share of **entrypoint** traffic over 5m, behind the same
  `TRAEFIK_MIN_RPS` floor — the gap BOTH checks above leave open, and the one #1322 fell
  through. They are per-SERVICE, and an edge that has lost its routers emits no
  `traefik_service_*` series at all, so their loops iterate an empty vector and report
  `0 service(s) above floor` while every route 404s. The entrypoint counter is incremented
  before routing, so it survives that. `TRAEFIK_404_PCT` is 90 rather than a low number
  because a homelab edge serves a steady 404 trickle: measured 2026-09-06, 4.0% of 0.83 rps
  healthy against 100% of 0.61 rps during the outage.)
- **Traefik 421** (per-**router** 421 rate over 5m, #2757 — the failure all three checks
  above miss. `SNICheck` pins the TLS-options name once per connection, so a client that
  connects while Traefik is still reconciling routes after a reboot gets 421 for the life of
  that connection. The request never reaches a service, and its code is not 404. Three such
  wedges went unseen in the week to 2026-09-27: authelia for 14.5h from 2026-09-20 23:10, the
  Pi's Alloy for 4h15m (#2747) and the apiserver's OIDC fetches for 5h30m (#2749). Their
  steady-state rates were 0.10-0.18 rps. The only other 421 in that week was one 5-minute
  window on uptime-kuma at 0.0083 rps. `TRAEFIK_421_RPS` is 0.02, an absolute rate rather
  than a share, and `TRAEFIK_421_CONSECUTIVE` (3) holds a one-shot mismatch below a page: it
  stays inside a `[5m]` rate for at most two evaluations.)
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
  unreachable *arr API rides `ARR_FETCH_CONSECUTIVE` (3 cycles = 15 min) since 2026-09-11,
  which is a DIVERGENCE from `check_n8n`/`check_scrutiny` — those still let the error bubble
  to `_evaluate` and page at once. The *arrs differ in one way that matters: they are
  Deployments this same bridge watches rolling, so their API refuses connections every time
  k3s replaces the pod, and three of this monitor's DOWN episodes in the 30 days to
  2026-09-11 were a fetch error co-timed with a `k8s_workloads ... radarr(1)` episode — one
  rollout reported twice. The streak covers the FETCH only: a queue item needing review pages
  on the cycle it is seen, because a poisoned release sitting in the queue is not a transient.
  **This REPLACED `arr_queue`'s `STARTUP_GRACE` membership** rather than stacking on it — that
  grace covered the same transient at 2 cycles and covered the queue verdict too. Pure
  `queue_warnings()` is unit-tested; the fetch streak has its own accept/reject pair in
  `test_check_service.py`.
  **Sonarr's self-clearing title holds are the one queue reason that waits** — held for
  `ARR_TITLE_HOLD_GRACE_H` (48) since #2786. Three of this check's seven DOWN episodes in the
  14 days to 2026-09-27 were "Episode has a TBA title and recently aired" (2.9h, 7.4h, 2.9h):
  Sonarr applies that hold itself and releases it once the title arrives, so the page named no
  action a human could take. 48h is upstream's own window rather than a taste — Sonarr's
  `EpisodeTitleSpecification` stops applying the rule once the episode aired more than 48h ago,
  so an item still carrying the message past that is stuck, not waiting. The item's `added`
  timestamp is the clock. The hold is narrow deliberately: it needs EVERY reason on the item to
  be self-clearing (the TitleTba and TitleMissing messages), it never applies to
  `importBlocked`/`importFailed` or to `trackedDownloadStatus == "error"`, and an item with no
  readable `added` timestamp is flagged rather than held. The other four episodes in that window
  were "Not a Custom Format upgrade for existing episode files" and still page on sight.)
- **Bazarr Health** (bazarr's `/api/system/status` + `/api/system/health` over `media`:
  `down` when a peer version field is present-but-empty, or when bazarr self-reports a health
  issue. **Bazarr is the *arr with no exporter, and that is the whole point.** It holds its
  OWN copies of Sonarr's and Radarr's API keys, in its config on the `bazarr-config` PVC and
  entered through its UI, so no Ansible template carries them and no deploy updates them —
  the 2026-08-29 rotation swept the eight templated consumers and missed the ninth. Bazarr
  then reconnect-looped, leaked 173 MiB to its 1Gi cap in 90 minutes and OOM-killed, and the
  only signal was the "k3s Container OOM" tile, which clears one hour after the kill and takes
  the evidence with it. Sonarr's and Radarr's own stale keys surfaced immediately as failing
  exportarr scrapes; bazarr had nothing.
  The peer-version fields are the detector: bazarr fills `sonarr_version`/`radarr_version` by
  calling each app with its stored key, so an empty one is a rejected key seen from outside.
  An **absent** field is ignored — bazarr omits it when that integration is switched off, and
  alerting there would page forever. Header is `X-API-KEY`, not the `X-Api-Key` sonarr/radarr
  take. Empty `BAZARR_API_KEY` disables the check (stays up), like `N8N_API_KEY`; an
  unreachable bazarr surfaces via `_evaluate`, which is also how the 401 from a stale
  `bazarr_api_key` in SOPS shows up. Pure `bazarr_problems()` is unit-tested both ways.
  **Not an exportarr sidecar, deliberately:** exportarr does speak bazarr, but at the pinned
  v2.3.0 its collector always runs the full episode-subtitle walk — upstream measures it in
  "tens of seconds," spent *inside* bazarr — and v2.3.0 predates the overlapping-collection
  skip added upstream for exactly that drainage (their #380). These two endpoints measured
  2-7 ms and 477+13 bytes, 2026-08-29.)
- **Prowlarr Indexers** (Prowlarr's `/api/v1/indexerstatus` + `/api/v1/indexer` over `media`,
  `X-Api-Key`: `down` only when an indexer has been failing ≥ `PROWLARR_INDEXER_MIN_DOWN_MIN`
  (1 week = 10080 min — only a genuinely long outage pages; short flaps are noise) measured from
  Prowlarr's own `initialFailure` — the age-based, per-indexer SUSTAINED
  signal Prowlarr's binary in-app health notification can't express (it's warnings-on-every-flap
  or all-indexers-down-only). Suppresses the transient tracker flaps that self-clear inside
  Prowlarr's ~5-15 min backoff. Age-based (not consecutive-cycle) so it survives a bridge
  redeploy mid-outage. Empty `PROWLARR_API_KEY` = disabled (stays up); a null/unparseable
  `initialFailure` is skipped, an unreachable Prowlarr surfaces as `down` via `_evaluate` (the
  `check_n8n`/`check_bazarr` convention — no grace; `check_arr_queue` left that convention on
  2026-09-11 and now rides `ARR_FETCH_CONSECUTIVE`). Pairs with Prowlarr set to
  `includeHealthWarnings=false` (keeps `onHealthIssue` = the instant all-down red backstop).
  `PROWLARR_INDEXER_IGNORE` (comma-separated names, case-insensitive) drops chronically flaky
  public trackers from the offender list — set to `The Pirate Bay,1337x` — the first after its
  apibay.org backend 503'd/timed-out for hours and flapped this monitor up/down on 2026-07-05,
  the second for the same chronic flapping (the remaining indexers
  cover the same searches; the all-down onHealthIssue is still the backstop). Pure
  `indexers_down()` is unit-tested. It shipped in commit `9abc18da4`.)
- **GitOps Deploy — Alive** (reads `/gitops-state/last_run`, a bind-mounted host timestamp the
  `gitops_deploy` deployer rewrites each non-crashing tick; `down` once it's older than
  `GITOPS_MAX_AGE_MIN` — that is, the deployer stalled / host down. The deployer no longer pushes
  to Kuma itself — see `ansible/roles/setup/gitops_deploy/CLAUDE.md`)
- **GitOps Deploy — Status** (reads `/gitops-state/hold_sha`, `/gitops-state/diverged_sha`,
  `/gitops-state/contention_since`, `/gitops-state/manual_plane` **and
  `/gitops-state/behind_since`**;
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
  name the cause where "behind" names the symptom. So is the **busy service lock** arm
  (`contention_since`, issue #1847): a tick that finds an operator `deploy.sh` still holding a
  service lock for its whole budget resets its tree and returns 0, so `last_run` advances,
  `hold_sha` stays empty and only `behind_since` ages — toward this six-hour threshold, sized
  for a dirty tree. The deployer records the streak's first tick in `contention_since` and
  this pages once it is older than `GITOPS_CONTENTION_MAX_MIN` (30 = the deployer's longest
  apply budget, `gitops_deploy_broad_timeout_s`), naming the lock; the tick clears the marker
  on its next run that is not deferred, and `gitops_state.py clear-contention` clears it by
  hand. The last arm is **`k8s_deferred`** (#2449): a promoted image bump a broad tick
  fast-forwarded and then deferred, either because the shared budget left less than
  `K8S_DEPLOY_TIMEOUT_S`. The range is merged,
  so `behind_since` is empty and no later tick's range carries the bump — the defer-and-alert post names it once and then nothing does. This
  pages once the oldest line is older than the same six hours, naming the
  `./scripts/deploy.sh --tags <svc>` that applies it and the
  `gitops_state.py clear-owed k8s_deferred <svc>` that follows; any tick that deploys the service
  clears the line itself. The arm reads the `owed` ledger's `k8s_deferred` class (#3392).
  Pure `gitops_status()` and its parsers are unit-tested; an
  unparseable marker reads as not-behind rather than paging forever on garbage.)
- **WG Pi Peer Backup** — RETIRED from this container at the host flips (2026-08-14). The pull
  became the `pi-peer-backup` k8s CronJob, which pushes its Kuma monitor directly, so there is
  no `/pi-peers/state.json` on this host and no `pi_peers()` check here. The monitor and the
  gap it watches are unchanged: the rsync uses no `--delete`, so a silently failing pull leaves
  the last-good copy in place while the Pi's un-rebuildable WireGuard peer keys go stale.
- **CrowdSec Home Allowlist** — RETIRED from this container at slice-6 B2 (2026-08-09). `cscli
  allowlists` is LAPI-machine-only, so the updater cron followed the LAPI into the cluster
  (`roles/k8s/crowdsec`) and pushes the Kuma monitor directly from daniel-box; there is no
  state file on this host to read, so the check, its `HOME_ALLOWLIST_*` env, its bind mount and
  its tests are gone. The monitor itself still exists — its AutoKuma label moved to the
  `uptime-kuma` role.
- **Public origin lock & AppSec verifiers** — RETIRED at E7 (2026-08-13) with the docker-edge
  public 80/443 origin. The Cloudflare-only origin (`docker-user-verify.sh` cron) and Cloudflare-IP-
  drift checks guarded the legacy Traefik@docker; the CrowdSec AppSec verifier has re-homed to
  daniel-box as a root cron pushing the same "CrowdSec AppSec" Kuma monitor directly from
  `roles/k8s/crowdsec/templates/crowdsec-appsec-verify.sh.j2`.
- **Disk Autoprune** — RETIRED at the Docker uninstall (2026-08-14), with no successor. The
  cron pruned daniel-server's Docker daemon, which no longer exists; containerd's own image GC
  owns that concern now (`files/bridge/config.py`, the `disk_prune check REMOVED` comment). Root Disk's threshold pager is what is
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
  (`docs/archive/b2-transaction-cap-monitoring-gaps.md`): B2 caps **transactions** separately from
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
  probe runs at most once per `B2_PROBE_INTERVAL_S` (1800 s = 48 calls/day) and a BILLED outcome
  is cached, because the fault being detected is a transaction cap and an every-cycle probe
  (288/day) — or `email_backstop`'s cache-successes-only idiom, which retries on failure — would
  spend the budget it's watching. The cached verdict is still pushed every cycle so the push
  monitor's heartbeat stays alive. Empty credentials = disabled (stays up).
  **A failure that never reached B2 takes a short TTL instead** (`B2_TRANSPORT_RETRY_S`, one
  INTERVAL — added 2026-08-30). Only an answer from B2 costs a transaction, so the argument for
  the long cache does not cover a DNS/connect/timeout failure, and `_get_json`'s contract makes
  the two separable: it re-raises `HTTPError` untouched and wraps everything else as
  `RuntimeError`. Caching both alike meant one transient failure pinned this gate DOWN for the
  full 30 minutes — on the 2026-08-30 restart the bridge's first cycle probed B2 before cluster
  egress was serving, and the tile read DOWN for 25 minutes against an 8m35s outage. The cache
  was holding back the RECOVERY, not just the retry.
  Note for anyone writing a test here: a real cap denial arrives as `HTTPError`, so faking it as
  a `RuntimeError` now exercises the transport path and proves the opposite of what it claims.
  The tests used to do exactly that; `_cap_denial()` in `test_check_gates.py` is the fix. `B2_DEPENDENT` is
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
  the Class A / Class B mapping lives in `check.py` from the pricing page. An `actionType` in
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
- **Cloudflare IP Drift** (`checks/cloudflare_ips.py`: the two published range pages against
  `CLOUDFLARE_IPS_EXPECTED`, the `cloudflare_ips` allowlist traefik trusts and netpol-baseline
  admits. A success is cached for `CLOUDFLARE_IPS_PROBE_INTERVAL_S` (a day); drift, a page
  with under 10 ranges, or a fetch that did not answer is DOWN and re-probed every cycle, so
  the tile clears one cycle after `cloudflare_ips` is fixed. That is why it moved here from
  k8s/traefik's daily root cron on 2026-09-19: the cron left a red tile until the next 05:25.
  Empty expected list = disabled (stays up). Pure `cloudflare_ips_verdict()` is unit-tested.)
- **Healthchecks.io Console Drift** (`checks/healthchecks.py`, #2566: `GET /api/v3/checks/` with
  the read-only `healthchecks_api_read_only_key`, compared per slug against
  `HEALTHCHECKS_EXPECTED` — the schedule type, the Simple period or the Cron expression and
  timezone, and the grace). The console is the only place those values live, and it drifted
  twice before anything noticed (#2563). `monitor_bridge_healthchecks_expected` in
  `defaults/main.yml` is the expectation; `ansible/tests/services/test_monitor_bridge_healthchecks_expected.py`
  holds it to the cron variables and to `docs/healthchecks-io-deadman.md`'s period-and-grace
  table. A documented slug missing from the console is DOWN; an undocumented console check is
  named and does not page (the `DECIDED:` in `healthchecks_verdict`). Same cache as Cloudflare
  IP Drift: a success is cached for `HEALTHCHECKS_PROBE_INTERVAL_S` (a day), a failure is
  re-probed every cycle, and it sits in `STARTUP_GRACE` because the fetch has no gate. The API
  answered a request without a key in 0.40-0.59 s across three calls on 2026-09-25, far inside
  `HTTP_TIMEOUT`. Empty key or expected list = disabled (stays up).)
- **SMART Data / Health** (Scrutiny's web API `/api/summary` over `monitoring`: every
  non-archived device must have a `collector_date` within 26 h **AND a passing `device_status`**
  (0 = SMART self-assessment + Scrutiny's attribute thresholds both OK; non-zero decodes to
  "SMART self-assessment FAILED" / "attribute threshold breached"). Freshness catches a
  silently dead collector (cron-as-PID1, no usable healthcheck → only shows as aging data); the
  status check catches a drive that goes SMART-FAILED / breaches a threshold while STILL reporting
  fresh data — nothing else alerts on that (Scrutiny writes to InfluxDB not Prometheus, and its
  own Shoutrrr notifier is unconfigured, so this bridge check is the only drive-failure alert
  path). Also `down` when Scrutiny lists no devices at all. `SCRUTINY_TEMP_MAX` (°C, default
  0 = off) adds an optional early warning temperature ceiling on top.
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
  its own declared max or crit where either is plausible (max preferred when both are — see
  below), else the flat `HWMON_TEMP_FALLBACK_C` (85 °C). **The fallback is not a nicety.**
  Measured live 2026-08-28, only 7 of 21 scraped sensors declare a usable max, and both
  daniel-pi sensors declare none — so a declared-max-only check would be silent on two thirds
  of the estate, daniel-pi included.
  **The declared value is sanity-bounded, not trusted:** three NVMe sensors declare 65261.85
  (a 0xFFFF sentinel for "no max"), and a ratio of that is unreachable, so those sensors
  would read green through a fire. A declared max or crit outside
  (`HWMON_TEMP_MIN_PLAUSIBLE_C`, `HWMON_TEMP_MAX_PLAUSIBLE_C`] is treated as UNDECLARED.
  An EMPTY sensor vector is `down`, not `up` — zero readings means EVERY collector went blind,
  and "nothing is too hot" from no data is a lie. `HWMON_TEMP_CONSECUTIVE` (12, one hour at
  `INTERVAL`=300) rides out a boost excursion; it is ONE streak for the whole check rather than
  one per sensor. It was 3 until issue #1186 — see *The excursions are workload, so the remedy
  is hysteresis* below.
  **The message names hardware, not sysfs paths** (2026-09-01): two extra instant queries
  (`node_hwmon_chip_names`, `node_hwmon_sensor_label`) turn
  `daniel-box/pci0000:00_0000:00:18_3/temp1` into `daniel-box k10temp/Tctl`. Both lookups are
  partial — 10 of 21 series carry a sensor label — so each half of the name degrades to its
  raw sysfs component independently, and an empty answer costs readability rather than the
  verdict. Each hot sensor also names the arm that set its limit, because a `declared` breach
  is the hardware calling itself too hot while a `fallback` breach may only mean the flat 85 °C
  does not suit that chip.
  **A second declared source, `node_hwmon_temp_crit_celsius`, joined 2026-09-03 (issue #995).**
  A driver need not publish `temp*_max` at all, and daniel-box's own k10temp is the case: it
  fired the fallback three times in five days (93.5 °C on 2026-09-03), which reads as either a
  real thermal excursion or a Tctl-over-junction offset the flat 85 °C never accounted for —
  nothing distinguishes the two from Prometheus alone. Read directly from
  `/sys/class/hwmon/hwmon2/` on daniel-box the same day: only `temp1_input` and `temp1_label`
  (`Tctl`) exist for this chip — no `temp1_max`, no `temp1_crit` — so k10temp declares NEITHER
  source here, and no code change can turn this specific alert into a declared-limit one. The
  fix that shipped is legibility, not a threshold: the message already said "fallback limit"
  and named the chip before this issue (PR #692), so daniel-box's k10temp/Tctl breach already
  read as "this chip rates nothing" rather than "this chip is over its own rating."
  **The offset half is settled, and a Tdie/Tccd series was never what would have settled it**
  (issue #1003). k10temp reports `Tdie = Tctl - temp_offset` and sets `temp_offset` only for
  six family 0x17 SKUs, none of them this one, so a Tdie on this chip would carry the SAME
  number as its Tctl — reading one would be a no-op, not a correction. The `DECIDED:` marker in
  `verdicts.host.hwmon_temp_limits` holds the driver evidence.
  **The number half is settled too, by a third arm rather than a fourth query** (#1152). AMD
  publishes `Max. Operating Temperature (Tjmax)` = 100 °C for the Ryzen 7 8845HS, so
  `HWMON_TEMP_RATED_MAX_C` carries that rating for this one sensor as
  `instance/chip/sensor=celsius`, and it is ratioed exactly like a declared max — 90 °C, the
  same effective limit daniel-server's coretemp already gets from its own declared max of 100.
  A rating is seeded BEFORE `crits` and `maxes`, so a driver that starts declaring either still
  wins, and it passes the same plausibility gate, so a typo cannot un-watch the sensor. Keyed by
  the raw sysfs triple, not the readable `daniel-box k10temp/Tctl`: `hwmon_name_maps` is partial
  by construction, and a name-keyed entry would stop matching in silence. Three earlier attempts
  recorded amd.com as unreachable from this estate — **it is reachable; a plain fetch is not.**
  With a browser `User-Agent` the product page returned 200 in 0.66 s on 2026-09-05.
  What this does NOT clear: daniel-box still exceeds the new 90 °C routinely, so the
  calibration narrowed a true condition rather than removing it. **The excursions are workload,
  so the remedy is hysteresis** (issue #1186, shipped 2026-09-06 as
  `HWMON_TEMP_CONSECUTIVE` 3 → 12). Measured over a true 7 days to 2026-09-06 at the 5 min loop
  cadence — Prometheus retains ~11.4 days here, so the `[30d]` figures in #1152 and #1186 really
  covered ~11 days (#1314): this sensor is above 90 °C for **12.0 %** of samples, p50 52.875 °C,
  p95 93.125 °C, max 93.75 °C against the rated 100 °C. Those 241 hot samples are 115 separate
  excursions, and their run lengths in cycles are 66×1, 23×2, 12×3, 5×4, 2×5, 3×6, 3×7, 1×8 and
  then a single 18 (90 min, from 2026-09-03T12:20Z) — **nothing between 9 and 17**. 3 cycles
  paged 26 times in that week; 12 pages once, on the 18-cycle outlier, which is kept on purpose
  because an hour and a half pinned above 90 °C is the shape of a cooling fault. Every excursion
  falls in the 11:00–03:20Z band with an 8-hour overnight hole, which is what settles it as
  scheduled and interactive work rather than an idle-state thermal floor — the correlation #1186
  asked for. `HWMON_TEMP_RATIO` was NOT raised: it is estate-wide and this is one sensor's
  duty cycle. The derivation and the pages-per-week table are at the `DECIDED: 12 cycles` marker
  in `files/bridge/config_host.py`.
  `crits` is still read for every OTHER sensor,
  because a driver that skips `max` but declares `crit` (none in this estate on 2026-09-03; added
  defensively) would otherwise take the flat fallback despite declaring a real limit. **`max`
  wins when a sensor declares a plausible value for both**, not `crit`: hwmon's own convention
  has `crit` as the LATER shutdown point, not an earlier warning — measured live 2026-09-03,
  daniel-server's NVMe declares max 85.85 / crit 86.85 — so ratioing `crit` would page closer
  to hardware failure than the max-based 90 % this estate already runs on. `crit` is used only
  when `max` is absent or implausible for that sensor.
  **A PARTIAL blindness is a separate arm** (`HWMON_TEMP_ORIGINS_MIN`, added 2026-08-29 for
  review M-9): the empty-vector branch fires only when ALL hosts go quiet, so until this arm
  existed one host's hwmon collector could die while the other two answered "all below limit"
  for the estate. It reuses `_host_origin_shortfall`, the same helper Root Disk and Memory
  use, but passes its OWN floor — **3, not the shared `HOST_ORIGINS_MIN` of 2**, because all
  three hosts declare non-excluded sensors (measured 2026-08-29: 9 / 5 / 2), so a floor of 2
  is met by any two of them. Origins are counted over the series that survive
  `HWMON_TEMP_EXCLUDE_CHIP`, through the same predicate `hwmon_temp_limits` uses — a host
  whose only sensors are `nvme` is a host this check does not cover, and counting it would
  satisfy the floor with a host nothing watches.
  `HWMON_TEMP_ORIGINS_CONSECUTIVE` (5) is **longer than `HOST_ORIGINS_CONSECUTIVE`** (3) on
  purpose: the third host is the Pi, and over the 7d to 2026-08-29 its hwmon series went
  absent for about 20 minutes (6 of 1054 samples at a 5m step, all daniel-pi), which the
  shared 15-minute grace would have paged on. The two hysteresis mechanisms are never
  compounded — `down_streak` is the thermal-spike grace and applies only to the hot-sensor
  path, so a missing host pages on its own fifth cycle rather than the fifteenth.
  Adding the arm is also why `host_temp` joined `EXPORTER_DEPENDENT` — under **two** job
  keys, `node` and `node-pi`. A dead node-exporter now trips the floor, and without the entry
  one root cause would page twice; the Pi scrapes under its own job (`count by (job, origin)
  (node_hwmon_temp_celsius)` measured 2026-08-29: job=node for daniel-server and daniel-box,
  job=node-pi for daniel-pi), so a `node`-only entry suppresses two of the three hosts and
  leaves the Pi double-paging. Pure `hwmon_temp_limits()` / `hwmon_temp_verdict()` are unit-tested in
  `test_host_temp.py`, each rule as an accept/reject pair; the coverage test is the load-bearing
  one, since this check's failure mode is silence rather than a wrong threshold.)

  **This monitor carries two more arms since 2026-09-10 (issue #1471), each with its own
  streak key.** Both signals were already plotted on `Infrastructure/hardware-thermal.json`
  (PR #1463) and alerted on by nothing. They are folded in here rather than given their own
  monitors for the reason `check_scrutiny`'s wear arm records: a new Kuma monitor needs a new
  push token in SOPS, and both answer the same question the temperature arm does — is the
  hardware being damaged right now.

  - **Undervoltage** (`_undervoltage_arm`, `UNDERVOLTAGE_QUERY` =
    `node_hwmon_in_lcrit_alarm_volts`). The Raspberry Pi firmware's own low-critical voltage
    alarm, a clean 0/1 with no threshold to choose. Evaluated FIRST and returning ahead of
    every other arm when asserted, because undervoltage corrupts SD cards and cooling down
    does not undo that. `UNDERVOLTAGE_CONSECUTIVE` is **1** — no grace, unlike every other
    arm here: the firmware latched a bit, it did not report a measurement that can spike.
    The load-bearing part is `UNDERVOLTAGE_UP_QUERY` (`up{job="node-pi"}`). The sensor is ONE
    series from ONE host, so a `max() > 0` arm reads green the moment daniel-pi stops
    answering — and a Pi falling off the network is what sustained undervoltage causes. So an
    empty vector defers only while that gate is not affirmatively up (`check_cluster_targets`
    owns a dead scrape, and an unqueryable gate defers too); an empty vector while the Pi IS
    scraping pages, because the sensor was renamed or the collector went blind.
  - **CPU thermal throttling** (`_thermal_throttle_arm`, `THERMAL_THROTTLE_QUERY` =
    `node_cooling_device_cur_state{type="Processor"}`). A non-zero `cur_state` means the
    kernel is derating the CPU now. A different fault from `check_cpu_throttle`, which reads
    CFS throttling — a cgroup quota, not heat — and from the temperature arm, since the
    firmware can enforce a limit below the one the driver declares. `type="Processor"` is
    load-bearing: unfiltered, the metric also carries PCIe link-speed and `intel_powerclamp`
    devices, which throttle for reasons that are not heat.
    It carries its own source gate, `THERMAL_THROTTLE_UP_QUERY` = `up{job="node"}` — `node`
    rather than `node-pi`, because the Pi publishes none of these series. A fully empty vector
    therefore means both amd64 exporters went quiet, which `check_cluster_targets` owns; an
    empty vector while `node` IS scraping pages, since a driver or kernel change taking the
    sensors away would otherwise go unnoticed.
    `THERMAL_THROTTLE_ORIGINS_MIN` is **2**, not the temperature arm's 3, because daniel-pi
    publishes no Processor cooling device at all (measured live 2026-09-10: daniel-box 16
    devices, daniel-server 8, daniel-pi 0). That floor is what stops the arm being inert —
    without it, one node's collector going blind leaves the other answering "not throttling"
    for the whole estate. `THERMAL_THROTTLE_CONSECUTIVE` is 3 (15 min at `INTERVAL=300`):
    one cycle of throttling during a compile is ordinary, sustained throttling is a cooling
    fault.

  A clean arm returns **None and says nothing**, so an ordinary cycle's tile text is
  byte-identical to what this monitor reported before the arms existed. An arm HOLDING inside
  its own grace does append its note — a monitor that is up while a fault accumulates has to
  say so. Both are unit-tested in `test_host_thermal_arms.py` as accept/reject pairs, plus one
  structural test that reads `check_host_temp.__code__.co_names` to prove the check calls both
  arms and calls the undervoltage one first: testing an arm alone would pass even if the check
  never called it. That structural test proves the call sites exist and their order, and it
  cannot prove the check PROPAGATES an arm's verdict — drop a `return` and the name stays in
  `co_names`. **`verdicts/host_power.thermal_monitor_verdict` is where the propagation lives**
  (issue #1547): the check fetches and holds the streak state, the composer decides which of
  the four arms reaches Kuma, and every ordering and propagation rule has a direct test in
  `test_host_thermal_arms.py` with nothing patched. Deleting the undervoltage `return` turns
  `test_an_asserted_undervoltage_alarm_reaches_the_monitor` red, which is the deletion the
  structural test could not see.

  Transport, measured before shipping because the arms read Prometheus: both queries answered
  in 0.48-0.58 ms, three runs each, against Prometheus's loopback on daniel-server (the node
  its pod was on), 2026-09-10. Same shape as the five instant queries the temperature arm
  already makes.)
- **UPS Battery Health** (mains loss + the APC UPS's charge % + estimated runtime + the
  replace-battery self-test verdict, all four read from **nut-exporter** and nothing else).
  Issue #1548 made nut-exporter the primary and kept HA's re-export of the same UPS as a
  `max(A) or max(B)` fallback inside each query string; **#3105 dropped the fallback half**. HA's
  NUT integration reads the SAME upsd over the `nut` ClusterIP, so the fallback never covered a
  upsd outage — only nut-exporter dying while upsd lived, which Prometheus reports as
  `up{job="nut"} == 0` and Scrape Targets already pages for. Measured 2026-10-01 against live
  Prometheus: nut answers all four arms (charge 100, runtime 1959, RB 0, OB 0), so the second
  source could only disagree with the first. HA's `hass_*` UPS series stay in the
  `Infrastructure/ups-power-battery` Grafana board, where a second view during an exporter
  outage is worth having and a disagreement costs nothing.
  `down` on sustained **mains loss** (`UPS_ON_BATTERY_QUERY`, the NUT
  `ups.status{flag="OB"}` — one-hot over `flag`, so the exporter forces a 0 when the UPS is not
  asserting it and the series is a real 0/1 alert input; judged FIRST and returning alone,
  because charge and runtime read the RUNWAY and hold green through most of an outage; its own
  streak key) or on a low battery RUNWAY: charge <
  `UPS_CHARGE_MIN_PCT` (50, a deep discharge while on battery) OR estimated runtime <
  `UPS_RUNTIME_MIN_S` (300 s — an aged battery whose full-charge runway has decayed, OR a discharge
  nearing shutdown) OR the UPS's own **replace-battery** verdict (`UPS_REPLACE_QUERY`, the NUT
  `RB` flag — the earliest signal, it can trip while charge/runtime still read fine; before this
  the RB verdict reached NEITHER channel, 2026-07-14 review). One defer path avoids double-paging
  a source outage another monitor owns: ALL arms absent while the nut scrape is down → Scrape
  Targets' page. That one shape now covers a dead upsd as well as a dead exporter, because
  nut-exporter fails the WHOLE `/ups_metrics` scrape when upsd is unreachable — which is why its
  probes are `tcpSocket` (`roles/k8s/nut-exporter/CLAUDE.md`). A second defer for **both numeric
  arms absent while the replace arm reports** existed until #3105 and only ever fired because the
  HA fallback dropped its numeric sensors while its replace-battery template floored to 0; one
  source has one absence shape. A **partial** absence (one arm gone while the others report) is a
  specific series rename → pages through the streak rather than silently
  monitoring the survivor. The only
  pre-existing UPS alert is an HA automation → **mobile** push (a separate channel from this
  Kuma→Discord brain) and nothing trended the battery, so a slowly degrading battery was invisible
  until an outage collapsed it — this is the health/runway signal + the Discord escalation path.
  **Prom-dependent** (queries the `nut` scrape). `UPS_CONSECUTIVE` (2, like
  `HA_CONSECUTIVE`) rides out a one-cycle dip from a transient load spike, a restart blip that
  briefly drops one arm, or a brownout shorter than the grace window. Queries are env-driven
  (`UPS_CHARGE_QUERY`/`UPS_RUNTIME_QUERY`/`UPS_REPLACE_QUERY`/`UPS_ON_BATTERY_QUERY`, all empty =
  disabled; `UPS_SOURCE_UP_QUERY` is the all-absent gate) so a series rename needs no code edit.
  Pure `ups_health()` and `ups_on_battery_verdict()` are unit-tested.)
- **Pi Pressure** (the Pi's own node-exporter series on the `node-pi` scrape job, five
  instant queries selected by `origin=PI_ORIGIN`: `down` when `node_load5` per core >
  `PI_LOAD_MAX`, `node_memory_MemAvailable_bytes` < `PI_MEM_MIN_MB`, any block device's
  `node_filesystem_*` usage > `PI_DISK_MAX_PCT`, or `node_filesystem_readonly` is 1 on `/` or
  `/boot/firmware`. The read-only arm landed 2026-09-26 (#2668). An SD card that remounts its
  root read-only after an I/O error freezes the fill % at its last value, keeps its open
  sockets, and keeps syslog flowing through log2ram, so every other arm stayed green through
  the classic Pi failure. It takes no grace, like `kubelet_plugin_readonly`, because a
  read-only remount does not self-heal. Filesystems are keyed by device rather
  than mountpoint because the SD card is mounted twice (`/` and `/var/hdd.log`) and one full
  card is one problem; tmpfs is excluded because log2ram's 128 MiB `/var/log` fills and
  flushes by design. A filling SD card is the classic slow Pi death the server-only Root
  Disk check can't see. The 512MB Zero 2 W dies by swap-thrash — 2026-06-11 fwupd episodes
  ran load5/core >1.7 with healthcheck-timeout storms no other monitor saw.
  **It read the Pi's glances API until 2026-09-18** (#2004), when glances retired: 66 MB
  of anonymous memory on a 456 MB host, for facts node-exporter already exported. The
  thresholds and the Kuma monitor are unchanged; the source moved, and with it the filesystem arm
  gained the vfat `/boot/firmware` partition, which glances' container view never saw —
  the same fault Root Disk watches `/boot` for on the nodes. The healthy message names
  the fullest device (`disk /dev/mmcblk0p1 37%`) because a bare percentage reads as the
  SD card. What changed with the source is the gating: this check is in `PROM_DEPENDENT`
  and in `EXPORTER_DEPENDENT["node-pi"]` now, so a Prometheus outage or a dead Pi
  node-exporter suppresses it rather than paging it a second time, and it LEFT
  `STARTUP_GRACE` — the two sets must stay disjoint, and its source is no longer a
  reach-out the reboot transient reaches. An absent series while Prometheus answers pages,
  because a Pi whose exporter stopped reporting is a Pi nothing is watching. Empty
  `PI_ORIGIN` = disabled (stays up).
  **This check still owns Pi disk and memory.** `HOST_METRIC_ORIGIN_EXCLUDE` keeps
  daniel-pi out of the Memory/Root Disk queries (`host_metric_sel`), so they stay two-host
  checks and this one stays the single source of truth for Pi pressure. Dropping the
  exclusion was considered at the glances retirement and rejected: `MEM_MAX_PCT` (90) on a
  456 MB box fires at 45.6 MB available, and `PI_MEM_MIN_MB` fires at 50 MB, so `check_mem`
  would be a strictly weaker duplicate of the floor here that pages the estate-wide Memory
  tile for a Pi fact this tile already reports. The exclusion also keeps the disk/memory
  origin floor at 2 hosts rather than 3.
  **If you add a third node-exporter host, decide explicitly whether it belongs in the
  estate-wide Memory/Root Disk checks or in a check of its own** — that choice is what this
  bullet exists to force. Since 2026-08-29 a test forces it for ONE of the two ways a host
  arrives: `test_every_node_exporter_job_is_mapped_in_exporter_dependent` (test_check_gates_exporters.py)
  derives the node-exporter scrape jobs from the Prometheus config and fails until each has an
  `EXPORTER_DEPENDENT` entry, and its sibling fails if a job whose origins are all excluded by
  `HOST_METRIC_ORIGIN_EXCLUDE` suppresses `disk`/`memory` anyway. That covers a host under a
  **new scrape job**, which is how daniel-pi arrived.
  **A host joining the existing `node` DaemonSet is still on you.** The job set does not change,
  so nothing fires — and `HWMON_TEMP_ORIGINS_MIN` is a literal 3, justified by "all three hosts
  declare non-excluded sensors". A fourth host makes that floor satisfiable by any three of
  four, so one host can go dark silently: exactly the partial blindness the arm was added for.
  Raise the floor by hand when you add a node.
  **Published-port arm** (`with_pi_ports`, folded here rather than given its own monitor for
  the push-token reason recorded at `with_ha_ban`): after a Pi reboot a container can come
  back attached to no Docker network while still reporting `Up (healthy)`, because its
  healthcheck curls loopback inside its own netns. The observable harm is that its published
  port stops listening, and only a recreate restores it — autoheal's restart loop re-enters
  the same empty sandbox and structurally cannot.
  The arm TCP-connects from the bridge to each expected port on `PI_HOST` (the Pi's LAN
  address, rendered from `hostvars['daniel-pi'].server_ip`) and names every dead one.
  Until 2026-09-18 it then fetched glances' `/api/4/containers` to say WHY — detached, or
  publishing but unreachable, or not up. Nothing the cluster can reach serves that view now
  (docker-proxy publishes no port), so the message carries the recreate hint and
  `ssh daniel-pi docker ps` tells the causes apart. The verdict never depended on the
  attribution — a failed fetch already read "cause unknown" — so the arm lost a diagnosis and
  kept its page. **The arm rides this check's gates**: a Prometheus outage or a dead Pi
  node-exporter skips the port probes with the pressure arms. Both already page, and the
  Kuma HTTP monitor on wg-easy still watches the one Pi port a person uses.
  `PI_PUBLISHED_PORTS` renders `name:port` pairs from daniel-pi's `containers_list` (every
  entry with a `port`), so `docker-proxy`, `autoheal` and `docker-proxy-lifecycle` — which
  publish nothing forever — fall out by construction rather than by an exclusion list.
  `udp_port` is excluded: there is no TCP-connect equivalent for UDP. `PI_PORTS_CONSECUTIVE`
  (2) rides out the seconds of closed ports a Pi deploy causes when it recreates a container.
  **This arm adds no reachability coverage — it adds a named port, and that is the whole
  case for it.** Measured 2026-08-27: Kuma HTTP-monitored glances and wg-easy (and dozzle,
  until it retired 2026-08-29), and `alloy` is a Prometheus scrape target (`job=alloy-pi`)
  that `check_targets_down` already covers. So every publisher was already watched. What
  nothing said was *which* port went quiet, and on 2026-08-08 that cost a manual sweep
  across four monitors which then missed dozzle entirely. Do not re-justify this arm as
  filling a monitoring gap; it does not.)
- **Home Assistant Automations** (HA's REST API `/api/states/input_datetime.ha_heartbeat` over
  `apps`, Bearer `HA_TOKEN`: an HA `time_pattern:/1min` automation stamps that helper with `now()`,
  so its `last_changed` is fresh ONLY while HA's automation *scheduler* is executing. `down` once
  it's older than `HA_HEARTBEAT_MAX_AGE` (300 s) — a wedged-but-running HA (HTTP `:8123` up,
  scheduler stuck) that the container healthcheck can't see. **Consecutive-cycle hysteresis
  (`HA_CONSECUTIVE`=2, same idiom as `CPU_CONSECUTIVE`):** a planned redeploy takes the API
  unreachable for ~120 s and then leaves the scheduler a beat behind, so a single cycle reads
  unreachable OR stale — only the second straight down cycle pages; the first pushes `up` with a
  "down streak n/N" `msg`, and one fresh read resets the streak. The unreachable-API error is
  caught inside the check (not left to `run_once`) so it rides the SAME grace as staleness — both
  are the deploy, not a wedge; a genuinely wedged/auth-broken HA stays bad across cycles and still
  pages. Empty `HA_URL`/`HA_TOKEN` = disabled (stays up). Pure `ha_heartbeat_fresh()` + the
  streak wrapper are unit-tested.
  **A second arm watches HA's own `ip_ban`** (added 2026-08-23): a `count_over_time` LogQL query
  for `Banned IP` lines over `HA_BAN_WINDOW` (1h), keyed on `container="home-assistant"` (see the no-`app`-label trap below), `down` on any hit. HA's ban middleware runs on
  every request and keys on the peer address, so an unauthenticated burst from inside the cluster
  bans an INFRASTRUCTURE IP — on 2026-08-23 five ad-hoc `curl` calls banned `10.42.0.1`, the
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
  It shipped in commit `9a3404b4b`.)
- **k3s Speedtest** (speedtest-tracker's `/api/v1/results`, newest row only, Bearer
  `speedtest_api_token` from the mounted credentials Secret. Three arms in this order —
  status, then age, then the download floor. The order is load-bearing: `download_bits` is
  null on a failed row, so a floor comparison ahead of the status arm compares None.
  **The floor was 100 Mbps because results were bimodal; the two populations have since
  collapsed into one.** As first measured over 2026-08-14..24, 20 runs on server 41671 had a
  median of 910 Mbps and a worst of 119, while 17 runs on six other servers had a median of
  12.8 and a best of 42.8, and nothing landed between — so 100 sat in an empty band. Re-measured
  2026-09-27 (#2785) the band is gone. The speedtest role pins server 41671, so every scheduled
  run now draws it and the second population has left the series; 41671's own low tail is
  continuous. Over the 95 six-hourly samples Prometheus retained (28.4d of the 30d asked for):
  min 61.4, p2 75.0, p5 95.1, p10 146.0, median 868.8 Mbps, 5 samples under 100. **100 is now a
  percentile cut at about p5, which is why the arm needs a RUN of results rather than one.**
  The value stays: p5 of a ~870 Mbps link is still the degradation reading this check exists
  for, and the floor remains what makes the pin going bad visible — 1775 was clean for 54 runs
  and then was not.
  **The floor arm pages on `SPEEDTEST_FLOOR_CONSECUTIVE` (2) consecutive sub-floor results**,
  counted backwards from the newest, and holds `up` with a `1/2 sub-floor results` note below
  that. The history comes from the same fetch — the page asks for that many rows, newest
  first — so the arm never waits a second cycle for it. A result that failed or recorded no
  figure is not a sub-floor result and breaks the run. Before this, one slow result held the
  tile red for the full 6h until the next test: three times in the 14 days to 2026-09-27,
  11.6h in total.
  The age arm (`SPEEDTEST_MAX_AGE_H`, 8h against a 6h schedule) is the one that notices the
  scheduler dying, which has no other symptom: the pod keeps serving its UI and passing both
  probes while writing no new rows.
  **No consecutive-CYCLE hysteresis on the verdict, only on the fetch.** The app produces a row
  every 6h and this loop runs every 5 min, so a consecutive-cycle streak would re-read one row
  up to 72 times — delaying the page and proving nothing. `SPEEDTEST_FLOOR_CONSECUTIVE` is not
  a counter-example: it counts RESULTS, each of them a separate measurement. The fetch rides `SPEEDTEST_CONSECUTIVE`
  because an app restart under a deploy is a real transient; `speedtest` is also in
  `STARTUP_GRACE`. Same split as `check_ha_heartbeat`.
  Reaching the app needs `monitor-bridge` in speedtest's `netpol_from` list in
  `ansible/inventory/host_vars/daniel-box.yml` — the
  baseline admits `traefik`, `prometheus` and two cni0 /32s, none of which is this pod.
  **The verdict stays on the REST API although Prometheus scrapes the same app** — #3105 asked
  for the switch and this is the recorded answer, marked `# DECIDED:` at the fetch in
  `checks/host_edge.py`. Three facts decide it. The scrape carries no timestamp: `count by
  (__name__) ({job="speedtest"})` returned 28 names on 2026-10-01 and not one is a created-at,
  age or timestamp series, so the age arm — the only arm whose failure mode nothing else sees —
  could only be inferred from when `speedtest_tracker_result_id` last changed over an 8h window.
  `SPEEDTEST_FLOOR_CONSECUTIVE` counts RESULTS, which one REST fetch hands back, where
  Prometheus samples a 6-hourly result every 5 min and the arm would have to group samples by
  result id before counting. And the scrape depends on a manual UI toggle that
  `roles/k8s/speedtest/CLAUDE.md` records as impossible to set from config at the pinned build,
  on a `longhorn-nobackup` PVC — a human step this alert path should not acquire. The scrape stays
  what #996 added it for: history in Grafana, which a Kuma tile cannot keep.)
- **Renovate Notifier — Alive** — RETIRED from this container at the host flips (2026-08-14).
  The notifier pushes its own Kuma monitor from an `ExecStartPost` now, so there is no
  `/renovate-state/last_run` bind mount and no `renovate_alive()` check here. The monitor and
  its dead-man semantics are unchanged.
  It shipped in commit `e02965544` and its neighbours.
- **Loki Reachable** (a fixed `/loki/api/v1/labels` probe — the root-cause GATE for the
  Loki-querying checks, the peer of Prometheus Reachable. Evaluated each cycle: when Loki is
  unreachable the `LOKI_DEPENDENT` check (`loki_ingestion`) is
  **suppressed** — pushed `up` with a "skipped — Loki unreachable" `msg` — and only THIS monitor
  pages. It was two until janitorr's watchdog moved to the cluster (2026-08-08); one Loki outage
  firing both at once is why the gate exists. Loki being UP but promtail not
  shipping is a different signal Loki Log Ingestion still surfaces. `LOKI_DEPENDENT` is guarded by
  a test against the live `CHECKS` so it can't drift.)
- **WAN Reachable** (two provider URLs fetched by hostname, tried in order — the root-cause
  GATE for the internet-reaching checks, and the newest peer of Prometheus/Loki/B2 Reachable.
  An internet outage had no gate at all before 2026-09-27: on 2026-09-18 from 05:05 a single
  WAN outage turned 11 tiles red inside 90 minutes — `b2_reachable` (3.9 h), `r2_usage`
  (3.6 h), `discord` (3.7 h), `kuma_notify_failures` with 16 sends dropped on ETIMEDOUT or
  ENETUNREACH, `crowdsec-home-allowlist` (3.8 h, `failed to resolve public IPv4 from ipify`),
  `cloudflare-ip-drift`, `github-ruleset-drift`, `release-staleness-check` (3 h),
  `docs-refresh`, `swallowed_verdicts` and `gitops_alive`. Gates `WAN_DEPENDENT` — `r2_usage`,
  `cloudflare_ips_drift`, `healthchecks_drift`, `discord`.

  **DOWN only when NEITHER endpoint answers.** One provider's outage is that provider's
  problem, and suppressing on it would turn Cloudflare's own tiles green during a Cloudflare
  outage. Requiring both to fail leaves the gate reporting the one fault it can attribute to
  this house's link. The endpoints are checked in order and the first answer stops the probe,
  so a healthy cycle costs one request.

  **By hostname, never an `anycast` IP.** That outage included DNS failure, and an IP-only probe
  stays green through a DNS-only outage while every dependent fails — the exact storm the gate
  exists to suppress. Measured from daniel-server 2026-09-27, three runs each:
  `cloudflare.com/cdn-cgi/trace` 0.095-0.100 s total, `www.google.com/generate_204`
  0.074-0.122 s, both with 0.002-0.052 s of DNS.

  `b2_reachable` is the tile's PEER rather than a member: it is itself a gate, and
  `GATE_DEPENDENTS`' values are check names `run_once` iterates, not gates it evaluates. Two
  tiles for a WAN outage is the accepted cost of not building gate-of-a-gate for one caller.
  `kuma_notify_failures` and `swallowed_verdicts` are not members either — both read in-cluster
  Loki, so the Loki gate owns their source, and what failed for them on 2026-09-18 was Kuma's
  own outbound send, which is a real fault the tile should report.

  **The four HOST crons that outage lit are covered by a second, separate half** (#2793).
  `crowdsec-home-allowlist`, `github-ruleset-drift`, `release-staleness-check` and
  `docs-refresh` run on the host and push their own tiles, so they are outside this loop and
  this gate structurally cannot suppress them. Each now consults `wan_reachable` in
  `ansible/roles/setup/initial_setup/files/kuma-push-lib.sh` on the failure path that reached
  the internet, and reports `skipped: WAN unreachable` as an `up` when neither provider
  answers. The probe is what classifies, not the error text: parsing curl exit codes and git
  stderr across four heterogeneous crons is the "green and inert" shape that library's header
  records paying for twice, and the probe answers the same question empirically — a GitHub 403
  while the link is up makes it succeed, so that tile still pages. The endpoint list is
  `ansible/inventory/group_vars/all.yml:wan_probe_urls`, pinned equal to this gate's
  `WAN_PROBE_DEFAULT` by a test, because two halves disagreeing about what "the internet"
  means would leave a single-endpoint outage reported by nothing at all. The one case it gets
  wrong is an HTTP-level failure coinciding with a WAN outage, reported as a skip; both tiles
  would have been red for one root cause anyway.

  `r2_usage` and `healthchecks_drift` left `STARTUP_GRACE` to join this set, the move
  `pi_pressure` made when it gained the Prometheus gate (#2004): the two sets must stay
  disjoint so a graced check reaches the evaluation path every cycle, and the gate covers the
  post-reboot transient the grace covered plus the outage it never could.
  Empty `WAN_PROBE_URLS` = disabled.)
- **Cluster Prometheus Reachable — RETIRED 2026-09-28** (#2825). A second `vector(1)` gate
  against `CLUSTER_PROMETHEUS_URL`, kept separate while that URL and `PROMETHEUS_URL` named two
  instances on two hosts. The Docker plane retired 2026-08-14 and both rendered to one cluster
  Service after it, so the tile could not go red on its own: `run_once` reused the `prometheus`
  gate's verdict and pushed the tile `up` regardless (#2780). Its four members —
  `k8s_workloads`, `cluster_targets`, `pvc_fullness`, `etcd_db_size` — are in `PROM_DEPENDENT`
  now, and the URL, push token, gate and tile are gone. A `git revert` restores the split; a
  check reading a second Prometheus needs a gate watching that instance.
- **k3s Workload Health** (`kube_deployment_status_replicas_unavailable` from kube-state-metrics — the only monitor the seven routeless k8s workloads have, and the
  reason the metric exists at all: `registry`, both `cloudflare-ddns` copies, karakeep's
  `chrome`/`meilisearch`/`time-tagger`, and `n8n-runners`, which executes every workflow's code.
  Three expose only a ClusterIP (unreachable from daniel-server), four expose **no Service at
  all**, and none has an ingress route — so their health is a Kubernetes API property, not an
  HTTP one, and nothing here can probe them directly. **Fails closed on an absent series, and
  this is the whole point:** `unavailable > 0` returns an empty vector both when every workload
  is healthy AND when there are no series at all, so the check `count()`s the series FIRST and
  reports `UNKNOWN, not OK` when the count is missing or below `K8S_MIN_WORKLOADS` (5). Reading
  the healthy meaning onto both is how a monitor goes green while blind — the shape of the B2
  transaction cap (2026-08-02) and the GitOps-behind defer (2026-08-07). The floor also covers a
  partially loaded kube-state-metrics: its ClusterRole is deliberately scoped, so dropping `apps`
  would take every deployment series away while the pod stays up and Ready. That fault is
  invisible to the reachability gate above, which is why both exist.
  **`K8S_WORKLOADS_CONSECUTIVE` (3) gates the unavailable-replica arm ALONE**, added
  2026-09-11 (#1780). A Deployment rolling has one unavailable replica by definition, so that
  arm reported every ordinary rollout: of the 48 DOWN episodes this monitor opened in the 30
  days to 2026-09-11, the replica ones are all single-cycle and all name one workload
  (`unavailable replicas: uptime-kuma(1)`, and the same for valheim, radarr, jellyfin,
  speedtest, karakeep-chrome). A held cycle appends the sibling `down streak n/N (rollout)`
  note to the tile rather than reading plain green — a monitor that is up while a fault
  accumulates has to say so. **Every other arm keeps no grace**: a crash loop is already a
  multi-cycle condition by the time `increase()` sees it, and a floor breach means the check
  is blind, which delaying helps nobody. `test_check_k8s_workload_replicas.py` proves the
  selectivity by running a crash loop and a rolling replica in ONE cycle — a blanket streak
  would pass a naive accept/reject pair while silently delaying every crash-loop page.
  **A stalled rollout is its own arm** (added 2026-09-11, #1783):
  `kube_deployment_status_replicas_updated < on(namespace, deployment)
  kube_deployment_spec_replicas`, gated by `K8S_ROLLOUT_STALL_CONSECUTIVE` (3). The arm above
  reads `unavailable`, which counts the replicas a Deployment HAS; this one reads `updated`,
  which counts the replicas carrying the CURRENT spec. A new ReplicaSet that never gets a pod
  while the old one keeps serving moves the second and not the first, so every other arm here
  reads `N k8s workloads healthy` while the cluster runs the previous spec.
  **It is the complement of the 2026-09-10 Authelia stall, not a second reading of it.**
  kube-state-metrics has authelia at `updated=1, available=0, unavailable=1` from 12:52 to
  13:03 that day, so that Deployment terminated its pod and failed to bring the replacement
  up — the unavailable-replica arm's shape, and the one the 300s `rollout status` wait failed
  on. Nothing watched the other shape, which is what this closes.
  The gate came from the series, not from feel: over the 16.9 days Prometheus retained on
  2026-09-11, `updated < spec` occurred for 1-2 five-minute samples at a time and NEVER for
  three consecutive ones, so at 3 cycles this arm had no false page in that window. It is also
  longer than the 300s the playbook itself waits. The desired count comes from a SECOND query,
  because a PromQL `<` returns the left-hand series alone — `authelia(0/1)` reads as "zero of
  one replicas carry the new spec", where a bare `authelia(0)` would read as "zero replicas,"
  a different fault. The stall helpers and BOTH replica streak gates live in
  `checks/cluster_rollout.py` rather than `checks/cluster.py`, which is at the 600-line
  module cap.
  **A Deployment at ZERO available replicas is its own arm** (added 2026-09-17, #1802):
  `kube_deployment_status_replicas_available == 0 and on(namespace, deployment)
  kube_deployment_spec_replicas > 0`, gated by `K8S_ZERO_AVAILABLE_CONSECUTIVE` (2), in
  `checks/cluster_zero.py`. Zero available is down, not rolling, and the unavailable arm's
  15-minute grace held the two alike. The census that set 2 rather than 1: over the 16 days
  Prometheus retained on 2026-09-17, every zero-available episode of two or more 5-minute
  samples was either a crash loop the restart arm had paged on its first cycle (authelia
  2026-09-10 — the incident #1802 cites was paged at 12:53 by that arm, one minute in —
  jellyfin/valheim 09-09, zigbee2mqtt 09-12), a 20-minute cold start after a node event
  (jellyfin 09-02, traefik 09-03, both paged by the 3-cycle arm), or the 2026-09-05 outage.
  The issue's "valheim 9, jellyfin 12, traefik 5, authelia 6" sample counts were those
  incidents, not routine Recreate swaps: a routine swap is one sample, and at 1 cycle this arm
  would page each of them. The one page 2 adds over 3 in that window is sonarr 09-01, a
  two-sample cold start. The streak is separate from the unavailable arm's, so a Deployment at
  zero is held with two notes (`cold start` and `rollout`), pages here on its second cycle,
  and is named again by the unavailable arm on its third; the `desired` count comes from the
  same second query the stall arm makes, so the message reads `authelia(0/1)`.
  **A second arm covers DaemonSets** (added 2026-08-13):
  `kube_daemonset_status_number_unavailable`, with its own `K8S_MIN_DAEMONSETS` floor (9) and
  the same fail-closed-on-absent-series logic — a Deployment-shaped census cannot see promtail,
  node-exporter or the `otel` collector, which run one pod per node and are exactly the workloads
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
  plugin's own liveness: `dri-device-plugin` has no `readinessProbe`, and a container without one
  is Ready the instant it starts, so a plugin whose gRPC registration hangs keeps a Running,
  Ready, fully available DaemonSet while kubelet deregisters `devic.es/dri` — invisible to the
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
  `PROM_DEPENDENT` is guarded against the live
  `CHECKS` and asserted disjoint from the other skip sets.)
- **Cluster Scrape Targets** (`up{origin!="daniel-server"}` — the
  complement of Scrape Targets' `origin="daniel-server"` pin, so every `up` series belongs to
  exactly one of the two. Same fail-closed floor (`CLUSTER_TARGETS_MIN`, 3): an emptied `up`
  reads as UNKNOWN rather than as nothing being wrong.
  **`CLUSTER_TARGETS_CONSECUTIVE` (3) was added 2026-09-11** and is the only hysteresis here.
  A rolling workload drops its own `up` series for a scrape or two, and this check cannot tell
  that from an exporter that died — so every ordinary rollout opened a DOWN episode. 66 of them
  in the 30 days to 2026-09-11, the highest count in the estate, nearly all one cycle long and
  naming a single target that was rolling at the time. 3 cycles = 15 min at `INTERVAL=300`, the
  same value `LONGHORN_CONSECUTIVE` / `PVC_CLAIMS_CONSECUTIVE` / `SNAPSHOT_CAP_CONSECUTIVE`
  already carry: far longer than any rollout's scrape gap, far shorter than a dead exporter. The
  gate delays, it does not suppress — a target genuinely down still pages, one cycle later.
  The floor arm rides the same streak deliberately: an emptied `up` during a Prometheus roll is
  the same transient.)
- **k3s PVC Fullness** (`kubelet_volume_stats_available_bytes / _capacity_bytes` via the cluster
  Prometheus, added 2026-09-01 — the SPACE axis of the storage layer, where Longhorn Volume
  Redundancy is the replica axis. A Longhorn PVC is its own filesystem at a fixed capacity, so a
  2 Gi claim can fill to 100% while Root Disk reports 620 GB free; nothing watched that, and
  `kubelet_volume_stats` appeared in zero files repo-wide before this. `PVC_MAX_PCT` is **85**,
  not Root Disk's 90: a full PVC can't be relieved by deleting something elsewhere — the
  operator has to expand the volume — and on the smallest genuine claim (973 MiB) 85% leaves
  146 MiB of headroom against 97 MiB at 90%. **A percentage cannot warn before a step**:
  valheim-server sat at 79% for days and reached 100% inside one 15-minute updater cycle
  when a Steam update staged a fourth copy of the install (#1866), so `PVC_MIN_FREE` (added
  2026-09-17, #1875) declares a per-claim free-bytes floor — `<pvc>=<bytes>`, the
  SNAPSHOT_CAPS shape — and the check pages while a named claim's
  `kubelet_volume_stats_available_bytes` is below it, no streak, whatever its percentage
  reads. The value is the claim's largest transient, declared by its own role
  (`valheim_k8s_server_update_transient_bytes`, 3 GiB against a 2.2 G copy) and pinned to
  this one by `tests/test_check_pvc_floors.py`. A rate signal was rejected: the updater's
  cycle is 15 min and the check runs every 300 s, so a stable window fires after the ENOSPC
  as often as before. A named claim reporting no free bytes is a breach, and the green
  summary names every floor held, so an arm that stopped evaluating is visible as its
  absence. `PVC_EXCLUDE` drops `media-data`, the one claim
  backed by a `local` PV at `/srv/media` rather than by Longhorn: it IS the `/` filesystem Root
  Disk already watches, so scanning it here pages twice for one full disk. **Aggregated `max by
  (namespace, persistentvolumeclaim)`** because daniel-box's claims are scraped TWICE — k3s
  serves the kubelet registry on the supervisor's `/metrics` too, so the same series arrives
  under `job="kubernetes-kubelet"` and `job="kubernetes-apiserver"` (43 + 27 series over 43
  claims, 2026-09-01); `sum` would report a double-scraped claim at twice its real fullness.
  **Fails closed on a thin claim census**, and `PVC_MIN_CLAIMS` (32) is DERIVED, not a
  conservative round number: the kubelet job alone reports all 43 claims and the apiserver job
  alone 27, so losing the apiserver job costs no coverage and the only hazard is a dead kubelet
  job — which leaves 27 claims answering, every one under the limit, while daniel-server's go
  dark. Any floor at or under 27 reads that as healthy. For the same reason it gets **no
  `EXPORTER_DEPENDENT` entry** keyed on the kubelet job: that would suppress the page on exactly
  the partial outage the floor exists to catch, which is the `node`-only mistake that blinded
  Host Temperature on two hosts of three, not a fix for it. A fullness breach gets no grace —
  it is monotonic, not flappy — while the census arm rides `PVC_CLAIMS_CONSECUTIVE`.)
- **Longhorn Snapshot Headroom** (`longhorn_snapshot_actual_size_bytes` joined to
  `longhorn_volume_capacity_bytes` for the claim name, added 2026-09-10, #1627 — the SNAPSHOT
  axis, where PVC Fullness is the filesystem axis and Longhorn Volume Redundancy the replica
  axis. Snapshots live in the Longhorn backend, so a volume can fill its
  `spec.snapshotMaxSize` while its claim reads 12% full and every replica reads healthy. A cap
  that is REACHED does not prune, it refuses: Longhorn stops accepting new snapshots, and
  `k8s/volume-snapshot` snapshots before it prunes, so the first deploy past the cap fails and
  every later one fails identically until snapshots are deleted by hand (#1560). That role's
  own gate fires only during a deploy of the capped service; this arm is what watches a volume
  filling BETWEEN deploys, which a recurring Longhorn backup job does with nobody deploying.
  **The caps are DECLARED in `SNAPSHOT_CAPS`**, `<pvc>=<bytes>`, because nothing exports the
  field: Longhorn's exporter publishes snapshot sizes and no cap, this pod runs with
  `automountServiceAccountToken: false`, and the Longhorn HTTP API answers only from the
  node-local manager. Ansible is the only writer of a cap — `roles/k8s/jellyfin/tasks/main.yml`
  patches the only one (jellyfin-config, 16 GiB = 2 x the PVC size, the smallest Longhorn
  accepts) — and `tests/test_check_snapshot_headroom.py` derives the capped set from the tree,
  so a second capped volume missing from the declaration fails CI rather than going unwatched.
  `"0"` is Longhorn's UNCAPPED value and the fleet default, so it is dropped rather than read
  as a cap of zero; a check treating 0 as a cap would report every volume full. **Usage is a
  superset of what the deploy gate sums**: the gate skips `status.markRemoved` snapshots and
  the metric carries no such label (2026-09-10: 25 of 114 Snapshot CRs were `markRemoved` and
  every one still had a series), which errs safely — those blocks are still held until
  Longhorn purges them — but can overstate usage for a cycle after a prune, so a breach rides
  `SNAPSHOT_CAP_CONSECUTIVE`. Deduped by (volume, snapshot) before summing, like PVC
  Fullness's `max by`: both longhorn-manager pods are scraped independently. A declared cap
  whose volume has NO series is a breach, not green. **Armed 2026-09-10** (#1627) —
  `monitor_bridge_snapshot_headroom_push_token` is in SOPS, and both halves read it unguarded:
  `KUMA_PUSH_SNAPSHOT_HEADROOM` in this role's env-secret and the Kuma declaration in
  k8s/uptime-kuma/templates/static-monitors.yaml.j2. It shipped inert on 2026-09-10 with the
  token absent, which is the shape #1632 names: a gated monitor with an unset variable reads
  green while watching nothing.)
- **Kubelet CSI Mount Read-Only** (`node_filesystem_readonly{mountpoint=~"/var/lib/kubelet/
  plugins/.*"} == 1`, added 2026-09-05, #1243 — a reclaim stall dropped Longhorn's iSCSI
  sessions, `replacement_timeout` expiry then aborted several ext4 journals and remounted them
  read-only, and every existing monitor stayed silent for 50 minutes because Volume CRs read
  `attached healthy` throughout — Longhorn's own state is structurally blind to a
  filesystem-level fault under it. The metric was already scraped; node-exporter's
  `mount-points-exclude` hid it under a wholesale `var/lib/kubelet` exclusion, narrowed
  alongside this check to `var/lib/kubelet/pods` (`daemonset.yaml.j2`) — the per-pod bind
  mounts stay excluded (unbounded cardinality), the CSI global mounts under `plugins/` do
  not (~40 volumes × ~7 series, ~300 total). **No grace**, unlike Longhorn Volume
  Redundancy: a read-only remount does not self-heal the way a replica rebuild or a kubelet
  restart does, and the schedule (node-exporter's `node` job at 1m, this check on the
  bridge's own 300s cadence) means a streak would only delay a real page, never absorb a
  routine blip. **`host_metric_sel`, not `origin_sel`**, for the same reason check_disk/
  `check_mem` use it: `PROM_ORIGIN` resolves to `origin="daniel-server"` in the deployed env,
  and pinning would hide the identical fault on daniel-box behind a green tile. An absent
  series reads as healthy (no CSI global mount is read-only) rather than as blind — unlike
  Longhorn Volume Redundancy/PVC Fullness, node-exporter being entirely down is
  check_targets_down's/check_disk's job, not this one's. The
  `node-exporter-scrapes-csi-global-mounts` row of
  `ansible/tests/k8s/_config_property_rows.py` guards the exclusion regex directly: this
  check's own empty-is-healthy logic cannot tell a real all-clear from a re-widened
  exclusion silently hiding the same fault again. **In `PROM_DEPENDENT`**: `prom_vector`
  raises on an unreachable Prometheus, which `_evaluate` turns into a `down` — without the
  gate, a Prometheus restart pages this monitor a second time for the one root cause the
  Prometheus monitor already reports.)
- **etcd DB Size** (`max(apiserver_storage_size_bytes)` against `ETCD_DB_QUOTA_BYTES`, down at
  `ETCD_DB_MAX_PCT` = 80% — `files/checks/cluster_etcd.py`, #2403. etcd goes READ-ONLY at its
  backend quota and the control plane stops accepting writes, which is the failure the restore
  runbook exists for, and until 2026-09-25 nothing watched it. **Why the proxy and not etcd's
  own series:** `k3s_etcd_expose_metrics` is off, so `etcd_mvcc_db_total_size_in_bytes`,
  `etcd_server_leader_changes_seen_total` and `etcd_disk_wal_fsync_duration_seconds_*` return
  no data. The
  `DECIDED:` marker at that switch in `group_vars/all.yml` records why it stays off — arming it
  restarts k3s and re-encrypts every Secret (#2294) from a manual-plane playbook, and opens an
  unauthenticated `:2381`. `apiserver_storage_size_bytes` is scraped unconditionally, and #2403
  reports it matching the etcd snapshot's size — 53,768,192 there on 2026-09-24, 56,389,632 read
  the next day, 2.6% of the quota. **`max(...)` over the bare series, no `by` and no `job`
  selector:** k3s
  serves the apiserver and the kubelet from one process, so the series is scraped TWICE with the
  same value — under `job="kubernetes-apiserver"` and `job="kubernetes-kubelet"` — and two
  identical series would make `prom_scalar`'s `result[0]` an arbitrary pick, while selecting on
  `job` would tie the check to a scrape-job name rather than to the metric. **2 GiB is etcd's
  OWN default** and is what is in force: `k3s_server_args` carries no
  `--etcd-arg=quota-backend-bytes`, so adding one there moves `ETCD_DB_QUOTA_BYTES` with it or
  the check measures against a quota the cluster does not have. **No grace**, for the reason CSI
  Read-only Mounts has none: a DB near its quota does not shrink on its own, so a streak delays
  a real page without absorbing a blip. An absent series reads as healthy and points at Scrape
  Targets — unlike PVC Fullness, whose fail-closed arm exists because 27 of 43 claims survive a
  dead kubelet job; one series carried by two jobs has no partial-coverage case, so empty means
  total scrape loss, which `targets`/`cluster_targets` already page on. **In
  `CLUSTER_DEPENDENT`**, because it reads `CLUSTER_PROM_URL`.)
- **Loki Log Ingestion** (three-arm LogQL freshness against the cluster `loki-homelab` via
  its in-cluster Service, `down`
  if ANY arm is silent — a silently dead Alloy→Loki pipeline (docker-proxy break,
  positions-file corruption, relabel regression) that Loki's `/ready` Kuma probe stays green
  through. **Arm 1 — file-tail union** `sum(count_over_time({job=~"authlog|syslog"}[3h]))`:
  (`check.py`'s in-code default also lists `traefik`, but `LOKI_STREAM` in
  `templates/env-secret.yaml.j2` overrides it and does not — the deployed selector is the two
  named here, so traefik's freshness is NOT covered by this arm.)
  counts the file-tailed streams — not one, so if Alloy dies they ALL fall silent together
  while syslog's routine volume keeps a quiet night alive (no single low-volume file trips it) —
  over a TOLERANT window. It deliberately EXCLUDES the `docker_sd` stream: Alloy stamps that
  stream `job: docker` (so a bare `{job=~".+"}` would swallow it), and it dwarfs the file-tail
  streams (~all 44 containers' stdout), so including it let a healthy container stream mask a
  total file-tail outage — arm 1 could then only reach zero if Alloy was *totally* dead, which
  arm 2 already catches (the 2026-07-07 blind-spot review re-scoped it to file-tail-only). The
  window is wider than arm 2's because file-tail volume is low and dips overnight (a lone
  `{job="syslog"}` over 10m false-paged 2026-06-23 — a 15m35s idle gap was observed). **Arm 2 —
  docker stream** `sum(count_over_time({container=~".+"}[30m]))` (`LOKI_DOCKER_STREAM`): the
  `docker_sd` stream carries a `container` label, no `job`, so it's exactly the one arm 1 excludes;
  a docker_sd-specific break (docker-proxy down, the docker relabel regressing) silences every
  container log while the file-tail streams keep flowing, and a tight window catches a total
  Alloy death fast. **Arm 3 — daniel-pi** `sum(count_over_time({job="pi"}[3h]))`
  (`LOKI_PI_STREAM`, added 2026-08-25 review M-11): arms 1 and 2 only count CLUSTER streams, so
  the Pi's own Alloy could die with every cluster stream still flowing and both arms green —
  the Pi's logs simply stop arriving and nothing said so. Runs on the TOLERANT window
  (`LOKI_FILETAIL_WINDOW`, same as arm 1), for a stronger reason than arm 1's: the Pi is a
  Zero 2 W running five LAN-only containers, so its log volume is genuinely low and bursty, not
  just quiet overnight. Selectors/windows tunable via
  `LOKI_STREAM`/`LOKI_FILETAIL_WINDOW`/`LOKI_DOCKER_STREAM`/`LOKI_WINDOW`/`LOKI_PI_STREAM`. Pure
  `loki_ingestion_fresh()` + `loki_count()` are unit-tested. A freshness watchdog in the same
  idiom as the SMART/restore-drill checks.)
- **Log Shipper Dropped Entries** (three arms. Two log-pipe arms, `down` on whichever counted MORE — added
  2026-09-03, #993, after the CLIENT-only arm was measured understating the server-side total
  by ~150x with nothing reading the server side at all. **Arm 1, client-side:**
  `sum(increase({__name__=~"loki_write_dropped_entries_total"}[1h]))` from Prometheus, which
  scrapes both shippers — the cluster Alloy DaemonSet (`job=alloy`, port 12345) and
  daniel-pi's Alloy container (`job=alloy-pi`) — across ALL drop reasons (`ingester_error`,
  `rate_limited`, `stream_limited`, `line_too_long` — the selector dropped its
  `ingester_error`-only filter on 2026-07-15, review M2). This only sees what a shipper
  itself gave up on. **Arm 2, server-side:**
  `sum by (reason) (increase({__name__=~"loki_discarded_samples_total"}[1h]))` read directly
  from Loki's own distributor (`job=loki-homelab`) — entries Loki rejected that no shipper
  ever attributed to itself. Measured 2026-09-03: Loki discarded ~161k samples server-side
  under `reason="too_far_behind"` in the trailing 24h, where the client-side counter saw only
  1,027 in the same window, entirely on `job=alloy-pi` under the unrelated
  `reason="ingester_error"` — the client-side arm alone gave no visibility into the
  server-side total or its real reason. `down` when either arm's total exceeds
  `SHIPPER_DROPPED_MAX` (3000, raised from 1000 on 2026-09-11 — a 1020-entry hour held this
  tile red for 51 minutes, and over the 14 days Prometheus retains the client counter is
  non-zero in 26 of ~336 hours with NOTHING between 1020 and 6261; the derivation and the
  rejected streak are at the `DECIDED:` marker in `bridge/config_io.py`) over the window; the message names which reason fired when the
  server-side arm is the one that dominates — `too_far_behind` is a clock/backfill problem,
  every other reason is throughput/limits, and the operator needs to know which. Where **Loki
  Log Ingestion** catches TOTAL silence, this surfaces PARTIAL loss either shipper- or
  Loki-side. The threshold keeps a transient Loki restart's handful of drops from paging;
  `increase()` handles counter resets; no series on either arm → 0 → up (a dead shipper
  scrape is Scrape Targets' page). **Prom-dependent** — suppressed under the Prometheus gate.
  Pure `shipper_dropped()` is unit-tested; `SHIPPER_DROPPED_WINDOW`/`SHIPPER_DROPPED_MAX` tune
  both arms, `SHIPPER_DROPPED_METRICS`/`SHIPPER_DROPPED_SERVER_METRIC` tune which counters
  they read — both queried by `__name__` regex, not a bare metric name, so a counter rename
  on either side can't silently read as "0 dropped forever."
  **Reading a `too_far_behind` page:** a shipper's FIRST start re-tails every current file
  from offset 0, and Loki rejects the already-ingested history as "too far behind" — 193,348
  at the 2026-09-02 cluster Alloy cutover, nothing lost, counted client-side under
  `reason="ingester_error"`. INFERRED from the observed labels, not read from Alloy's source:
  the client-side counter appears to fold `too_far_behind` into `ingester_error`, since that
  label only ever surfaces as `too_far_behind` on the SERVER-side arm. Read the shipper's own
  source before relying on the mapping for anything beyond reading a page.
  The 2026-09-03 #993 burst above lines
  up with the same shape: `alloy-pi`'s own uptime measured 58,756s (~16.3h) at discovery time,
  inside the 24h window the burst was measured over, and the client-side drops that same
  window were 100% `job=alloy-pi`. A shipper restart in the window is consistent with a
  benign re-tail, not confirmation of one — correlate `time() - process_start_time_seconds{job=~"alloy.*"}`
  against the alert window before treating a `too_far_behind` page as data loss. Either way
  it must page: a benign re-tail still means the SLA on "logs land in Loki promptly" briefly
  broke, and nothing but this arm would have shown an operator the shape of what happened.
  Land a shipper change more than an hour before pointing this check at its counters, to
  keep the cutover's own re-tail from paging the deploy.
  **The weekly reboot is the one `too_far_behind` case that is neither data loss worth paging
  nor a benign re-tail**, and the one exemption to "either way it must page" above (#2783): Alloy ships its backlog for hours afterwards and Loki
  discards the late part, which held this tile red for 4.4h on 2026-09-27 against a 1h rolling
  window — so the discards ran ~3.4h past boot. Inside `SHIPPER_BACKLOG_GRACE_S` (21600 s,
  ~1.75x that) of the NODE's boot — `bridge/common.py:host_uptime_s`, never this pod's age,
  because a deploy restarts the bridge without rebooting anything — the `too_far_behind`
  reason alone is dropped from the server-side total and named in the `up` message. Every
  other reason and the whole client-side arm stay live, so a throughput fault on a reboot
  morning still pages; suppressing the check outright would be six hours of weekly blindness
  on partial log loss.
  **The reboot also loses entries on the client side, and that loss is bounded by the lookback
  rather than held by a grace** (#3490). While Loki is down for the restart, daniel-pi's Alloy
  keeps shipping and drops what Loki refuses: 179,396 `reason="ingester_error"` entries on
  2026-10-04 under `job="alloy-pi"`, a node that did not reboot. The tile paged at 08:19:30,
  four minutes after the Kuma maintenance window closed, and recovered at 08:39:30. Against a 1h
  lookback that recovery puts the last drop at about 07:39, before daniel-box finished booting at
  07:46:35 UTC. The Pi pushes through Traefik on daniel-box, the node this pod runs on, so the
  loss cannot outlast that node's boot whichever node comes back first. Both shipper queries therefore follow `check_swallowed_verdicts`:
  inside `BOOT_SETTLE_S` (1200 s) of the node's boot the shipper arms are skipped with an `up`
  message, and after it the range is the time since the settle window ended, growing back to
  `SHIPPER_DROPPED_WINDOW`. A drop after the settle window is inside that range and still pages.
  Raising the maintenance window's recovery allowance to ~70 minutes was the alternative. It was
  not taken, because a window over 60 minutes covers every minute of the hour and the status
  page sync (`20 * * * *`) would then have to skip hour 8.
  **Arm 3, the collector's export failures**, folded in on 2026-10-01 from the
  `observability` role's `telemetry-health.sh` host cron (#3094), which ran this same query with
  its own Prometheus client: `sum(increase({__name__=~"otelcol_exporter_send_failed_.*"}[15m]))`,
  `down` above `OTELCOL_SEND_FAILED_MAX` (0). It is the third producer on the same pipe — the
  collector exports Claude Code's logs, metrics and traces to Loki, Prometheus and Tempo — and
  the only evidence this particular loss leaves anywhere: a failed export writes nothing to Loki
  or Tempo by definition while the collector's receiver counters keep climbing, so a stack
  dropping 100% of its data reads identically to one working perfectly from every other angle.
  Its own window and threshold rather than the two arms above, because an export failure is not
  log-line churn at any rate: there is no ordinary rate of giving up. The selector is a
  `__name__` regex for the reason arm 1's is — the family is one counter per signal
  (`_log_records`, `_metric_points`, `_spans`) and naming one would read the other two as 0
  forever. Measured on daniel-box 2026-10-01: the send-failed family has NO live series, because
  the collector mints a send-failed counter only once an export has failed; its `sent` twins
  (`otelcol_exporter_sent_log_records`, `_sent_metric_points`, `_sent_spans`, none `_total`-
  suffixed) are live and are what pins the naming. An absent family returns no series, which
  `prom_scalar` reads as None and the verdict counts as 0, so the arm is clean until the first
  real failure. Pure `otelcol_export_failures()` is unit-tested; an empty
  `OTELCOL_SEND_FAILED_METRICS` disables the arm. **What did NOT fold** is the OTLP hostPort
  probe: that door is a CNI portmap DNAT on the node's 127.0.0.1, so no in-cluster pod can reach
  it, and `telemetry-health.sh` survives as that one TCP connect — the `DECIDED:` marker at the
  top of the template carries the rejected alternatives.)
- **Swallowed Push Verdicts** (a host cron's DOWN verdict that `kuma-push-lib.sh` logged
  and then lost — added 2026-09-17, #1869. The library returns 0 after a failed push by
  design, so the cron does not fail, and the verdict reached nobody until the tile's
  heartbeat deadline: a day and an hour later for the daily drift producers, which is what
  reported setup-drift-check's lost DOWN of 2026-08-29 and release-staleness-check's
  http=500 of 2026-09-10. `check_swallowed_verdicts` reads the crons' push-outcome lines out
  of Loki over `SWALLOWED_VERDICTS_WINDOW_S` (3h) — the cron's own `status=<up|down>` line
  and the library's final `push failed (` line, the transient-retry line excluded in the
  LogQL — through `bridge.net.loki_lines`, a range query, because the newest line per tag is
  what decides and no metric query returns a line's timestamp. `down` when some tag's newest
  line is a swallowed `status=down` AND some other tag landed a push in the window; when
  nothing landed the loss is fleet-wide — Kuma unreachable, a total-404 edge, the host that
  runs Kuma down — and the message names the edge/host tiles as the owner instead of paging
  a second time for one cause. That gate is what separates this from the plain count of
  push failures `uptime-kuma/CLAUDE.md` measured and rejected. A swallowed `up` is not
  counted: its tile goes red at the deadline for a cron that ran, which is the library's
  case for the library's retry (#1010), not a hidden finding. **It never reads back past the node's
  boot** (#2783): uptime-kuma's Service has no endpoint at all for ~16 minutes of the weekly
  restart, so every host cron whose slot falls in there loses its push for one known cause, and
  a daily producer's lost verdict stays the newest line for its tag until tomorrow — 2.9h of red
  on 2026-09-27 after everything else had recovered. Inside `BOOT_SETTLE_S` (1200 s) the cycle
  is skipped and says so; after it the lookback is the smaller of the configured window and the
  time since the settle window ended, so the full window returns on its own and the tile pages
  again on the first verdict lost for any other reason. `BOOT_SETTLE_S=0` restores the earlier
  reading exactly. The one push counted whatever its status and
  company is one Kuma REJECTED — `by=kuma` in the http/rc pair, which the library appends
  when the 404 came back as Kuma's own `application/json` rather than Traefik's `text/plain`
  page (#1803). That is a token no live monitor holds: the edge and Kuma are both up so no
  other tile pages, and there is no tile to reach a deadline, so the verdicts are lost for
  as long as the cron and the static monitors carry different tokens. (The 2026-09-06 3.2h
  burst of 602 `http=404` lines that #1803 read as this was the other kind — Traefik
  rejecting every router over a missing CrowdSec middleware, #1322 — which Traefik 404
  Flood owns.) In `LOKI_DEPENDENT`. A fetch that hits the 5000-line cap says so in the
  message rather than deciding on the newest part. **Two pushers used to bypass the reader**
  (#1943, fixed 2026-09-18): the CrowdSec home-allowlist cron keeps its own curl but now logs
  the library's line shape, and pi-peer-backup's CronJob — a pod with no `logger` — echoes
  its `status=` and `push failed (` lines in the syslog prefix shape to stdout, where a
  second, narrow selector (`SWALLOWED_VERDICTS_POD_LOGQL`, `{container="pi-peer-backup"}`) reads them
  and the two fetches are merged before the verdict. Not `job=~"syslog|k8s"`: the cap was
  sized against the syslog stream alone.)
- **Kuma Notification Delivery** (a notification Kuma tried to send and dropped — added
  2026-09-17, #1891. Kuma logs `Cannot send notification to <name>` and does not retry, so
  the transition or resend behind that line reached nobody: twice during the qbittorrent
  lockout, 2026-09-10 18:00 and 2026-09-15 14:50, both Discord HTTP 429s. Discord Delivery
  GET-verifies the webhook and cannot see a dropped POST. `check_kuma_notify_failures` reads
  Kuma's own line out of `{container="uptime-kuma"}` over `KUMA_NOTIFY_FAILURES_WINDOW_S`
  (3h) through `bridge.net.loki_lines`, and `down` names each notification with its drop
  count and the reasons Kuma recorded with theirs (`reasons: HTTP 429 Too Many Requests
  x2`). The reason is Kuma's NEXT line, also at ERROR level — #1895 filed it as debug-only,
  and measured 2026-09-17 every one of 74 drops in 14 days had one (69 x 429, 5 x 400) —
  so the LogQL fetches both and counts reasons as a set beside the drops rather than
  joining them one to one; a drop with no reason in the window reads `reason not logged`.
  An HTTP reason is reduced to its status before it reaches the tile, because the axios
  message can carry the request URL and Discord's is the webhook secret. Raising Kuma's
  log level is neither needed nor safe (uptime-kuma/CLAUDE.md, the debug-logging trap).
  **A 400 here was an oversized push `msg` until #2013 (2026-09-18):** Kuma puts a push
  monitor's `msg` into a Discord embed field capped at 1024 chars and never truncates, so a
  fleet-wide list from release-staleness-check was rejected whole. `bridge.net.push` and
  `kuma-push-lib.sh` now cap the `msg` at 900 chars (`PUSH_MSG_MAX`, keeping a trailing
  `(N cycles)`), and that producer pushes names only. The
  window is the whole hysteresis:
  a drop pages for 3h and clears on its own, since nothing is cleared by hand. Its tile
  notifies EMAIL as well as Discord, on purpose: a page for a dropped Discord send that goes
  only over the same webhook is the failure it reports. In `LOKI_DEPENDENT`; a fetch that
  hits the 500-line cap says so in the message.)
- **Discord Delivery** (GET-verifies **all five** Discord notification webhooks: Kuma's own
  `monitor_discord_webhook_url` — the one Kuma sends every alert to — CrowdSec's
  `crowdsec_discord_webhook_url`, which CrowdSec sends ban alerts to *directly* (not via Kuma),
  the `gitops_deploy_discord_webhook`, which delivers the `gitops-deploy` rollback alert AND every
  `renovate_notify` digest (its Renovate Notifier — Alive marker greens even when the POST fails —
  no Kuma backstop), and `arr_discord_webhook_url`, which Sonarr/Radarr/Prowlarr POST their own
  onHealthIssue alerts to via in-app Discord Connect (config lives in the app DBs, not templated —
  the Arr Queue check covers stuck downloads, NOT indexer/download-client health), and
  `healthchecks_discord_webhook_url`, the retired self-hosted healthchecks app's check-down/up
  webhook (#2806). It stays verified because nothing in the repo records whether the
  healthchecks.io account's own integrations post to it. The
  latter four have NO Kuma backstop of their own. `down` if ANY is invalid,
  naming which; each empty URL is skipped. A
  rotated/revoked/deleted webhook makes those alerts silently fail to deliver while every monitor
  stays GREEN in the Kuma UI; this is the alert chain's delivery hop that NO other monitor — not
  even the off-box UptimeRobot host dead-man — exercises. A webhook GET returns Discord's metadata (200) when valid
  and 404 once gone, and never posts a message (no channel spam) — unlike a test POST. **It also
  probes the alert-EMAIL second channel** (`email_backstop`): the Gmail SMTP notification attached
  (only) to THIS monitor as the escape hatch when the Discord webhook is dead — a throttled SMTP
  login with the same creds Kuma uses (`SMTP_USER`/`SMTP_PASSWORD`), so a silently revoked
  app-password flips this monitor down and still pages via the working Discord channel. Throttled to
  `EMAIL_PROBE_INTERVAL_S` (6h — Gmail flags frequent `AUTH` commands): a success is cached, a failure
  re-probes every cycle. Empty `SMTP_PASSWORD` = that probe disabled. Both webhooks + SMTP reach the
  PUBLIC internet, so `DISCORD_CONSECUTIVE` (2) adds the same streak
  hysteresis as the HA heartbeat: a single transient non-200/network blip pushes `up` with a
  "down streak n/N" `msg` and only the second straight failure pages. Empty `DISCORD_WEBHOOK_URL` =
  disabled (stays up), like `N8N_API_KEY`. Pure `discord_webhook_ok()`, `email_backstop()`'s
  throttle + the streak wrapper are unit-tested. NOTE: it verifies the webhook is DELIVERABLE
  (catches a rotated/revoked URL); it does NOT assert Kuma still has the notification *attached* to
  each monitor — AutoKuma re-applies that on every deploy via the `kuma()` macro's `notification_name_list`.)

## Retired and moved checks

The bullets above that read RETIRED or *moved out* are kept in place so the monitor's
history stays readable beside its successor. The direct pushers that replaced them: the
`pi-peer-backup` k8s CronJob (WG Pi Peer Backup), the `crowdsec` role's allowlist cron
(CrowdSec Home Allowlist), `crowdsec-appsec-verify.sh.j2` (CrowdSec AppSec), the
`fake_remux` setup role's `state_push.py` (Fake Remux Scan / Replace), `renovate-notify`'s
`ExecStartPost` (Renovate Notifier — Alive), and the configarr and janitorr roles' health
crons (Configarr Sync, Janitorr Errors). Disk Autoprune retired with the Docker daemon and has
no successor.

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

## Push-monitor mechanics, and what set each number

- The restart/OOM/cpu/target/5xx/cert checks use `prom_vector()` (keeps series labels) so the
  alert names *which* container / target / route / certificate is failing; the others use
  `prom_scalar()`.
- Explicit `down` = fast, descriptive alert; the push monitor's heartbeat interval
  (`uptime_kuma_k8s_bridge_push_interval`, 1200 s = 4× the loop) is the backstop for "the bridge itself
  died". Same dead-man's-switch idea as `cloudflare-ddns` — see [[its CLAUDE.md]] and the
  `kuma(..., monitor_type='push')` macro.
  **It was 600 s until 2026-08-30**, tolerating a single missed push — which a host restart
  exceeds. Hosts went down at 07:36:31 and the last pod reached Ready at 07:45:06, but the
  bridge's last push landed before 07:36 and its first after was ~07:45, so nine bridge-fed
  tiles flipped DOWN and notified for one 8m35s event that had already ended. At 1200 s three
  missed pushes are absorbed and a genuinely dead bridge still pages, 20 min in rather than 10.
  The knob is the interval and NOT `max_retries`, which buys the same tolerance and costs the
  descriptive message — see the next bullet.
- **All push monitors set `max_retries=0`** (2026-06-12): with retries, Kuma parks a pushed
  `down` in PENDING and the 60s watchdog — which only `up` pushes satisfy — crosses
  `maxretries` first, so every visible DOWN event read "No heartbeat in the time window"
  instead of the check's named-offender `msg`. Zero retries means the bridge's own push flips
  the state and the descriptive `msg` lands in the event + Discord notification. Trade-off:
  a dead bridge pages after one missed heartbeat window (acceptable — that's the dead-man's
  switch doing its job).
  **This is why post-boot flapping is fixed by widening the window, never by adding retries.**
  The two are interchangeable as tolerance and are not interchangeable as alerts: a wider
  window costs only detection latency, while a retry parks the push in PENDING and hands the
  Discord message back to the watchdog. `test_push_monitors_never_retry` in
  `ansible/tests/services/test_kuma_static_monitors.py` is the guard.
- **Startup/redeploy grace for the reach-out checks (`STARTUP_GRACE`, 2026-07-12):** the
  checks that poll a live app dependency with **no reachability gate and no per-check hysteresis**
  — **n8n Prod Workflows** (n8n), **Bazarr Health** (bazarr), **Prowlarr Indexers** (prowlarr)
  and **SMART Data / Health** (Scrutiny) (both added 2026-07-14) — get a consecutive-down
  grace applied in
  `run_once` (peer mechanism to `PROM_DEPENDENT`/`LOKI_DEPENDENT`, but a *hysteresis* not a
  *suppression*). Cause: the bridge's first cycle after the **weekly Sunday 07:30 host reboot**
  runs before those heavy apps finish starting, so each un-graced `max_retries=0` monitor flipped
  DOWN on that one transient cycle (`<name> check error: Connection refused` / n8n `HTTP 404` while
  its API routes warmed up) and paged, then recovered next cycle — a weekly DOWN/UP flap. Which
  subset actually paged varied week to week (a startup race: some weeks the DOWN push itself failed
  because uptime-kuma wasn't ready yet). `apply_startup_grace()` holds each `up` for the first
  `GRACE_CYCLES`-1 (default 2−1 = 1) consecutive down cycles — the same "down streak n/N" idiom as
  `check_ha_heartbeat`'s `HA_CONSECUTIVE` — so only the `GRACE_CYCLES`'th straight down pages a
  genuinely dead dependency (~one extra INTERVAL later), and one `ok` resets the streak. The set is
  **disjoint from every `run_once` skip set** (so a graced check reaches the evaluation path each cycle and
  its streak advances) — both invariants guarded by a test against `CHECKS`. `GRACE_CYCLES` is
  env-tunable. Pure `bridge.streaks.apply_startup_grace()` is unit-tested.
- **Liveness probe (2026-06-10, k8s since the migration):** cli.py touches `/tmp/heartbeat`
  (tmpfs) after every cycle; the `livenessProbe` in `templates/deployment.yaml.j2` fails when the
  mtime exceeds ~3×INTERVAL, so **the kubelet** restarts a *hung* loop (death alone already exits
  the container). Kuma push silence remains the alerting path; the probe adds auto-recovery.
  (This was a Compose healthcheck restarted by autoheal until the k8s migration. autoheal now
  runs only on daniel-pi, so looking for its log line here finds nothing.)
- Push tokens — **`templates/env-secret.yaml.j2`'s `KUMA_PUSH_*` keys are the list**; no test
  reads this prose, so treat the names below as a reading aid that can go stale rather than as the
  source of truth. Count them rather than trusting a number written here — this bullet carried a
  hand-maintained "29" that was wrong by five when it was replaced:
  `grep -c '^\s*KUMA_PUSH_[A-Z0-9_]*:' ansible/roles/k8s/monitor-bridge/templates/env-secret.yaml.j2`.
  (The names went stale the same way: they once carried eight tokens retired at the 2026-08-14
  host flips and were missing six added after them.) The names on 2026-09-01:
  `monitor_bridge_{arr_queue,b2_reachable,b2_storage,bazarr,cert,cluster_prometheus,cluster_targets,cpu,discord,disk,etcd_drill,gitops_alive,gitops_status,ha,host_temp,k8s_workloads,loki,loki_reachable,longhorn_volumes,mem,n8n,oom,pi,prometheus,promtail_dropped,prowlarr_indexers,pvc,r2_usage,restarts,scrutiny,speedtest,targets,traefik,traefik_latency,ups}_push_token`
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
  existing `longhorn_b2_application_key` (ADR-0014; spelled `kopia_b2_*` until 2026-09-09),
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
  connection config: `SONARR_URL`/`SONARR_API_KEY`/`RADARR_URL`/`RADARR_API_KEY`/`ARR_TITLE_HOLD_GRACE_H`; GitOps
  liveness: `GITOPS_MAX_AGE_MIN`/`GITOPS_STATE_DIR`; Pi pressure:
  `PI_ORIGIN`/`PI_HOST`/`PI_LOAD_MAX`/`PI_MEM_MIN_MB`/`PI_DISK_MAX_PCT`/`PI_PUBLISHED_PORTS`/`PI_PORT_TIMEOUT`/`PI_PORTS_CONSECUTIVE`; HA heartbeat:
  `HA_URL`/`HA_TOKEN`/`HA_HEARTBEAT_MAX_AGE`/`HA_CONSECUTIVE`; speedtest:
  `SPEEDTEST_URL`/`SPEEDTEST_TOKEN`/`SPEEDTEST_DOWNLOAD_MIN_MBPS`/`SPEEDTEST_MAX_AGE_H`/`SPEEDTEST_CONSECUTIVE`/`SPEEDTEST_FLOOR_CONSECUTIVE`;
  host-coverage floor:
  `HOST_ORIGINS_MIN`/`HOST_ORIGINS_CONSECUTIVE`, and the thermal check's own pair
  `HWMON_TEMP_ORIGINS_MIN`/`HWMON_TEMP_ORIGINS_CONSECUTIVE`). A failed
  query/unreachable source makes that monitor `down` with an explanatory `msg` — a broken
  exporter is surfaced, not silently green.

## Operator prerequisites, as first written

The role file keeps the short form; this is the original, with the Docker-era `media`
network note that no longer applies to the k8s pod.

1. Add a push token to `secrets.yml` (`sops ansible/vars/secrets.yml`) for every `KUMA_PUSH_*`
   entry in `templates/env-secret.yaml.j2` — `test_every_push_token_env_is_wired_to_a_monitor`
   asserts that template's keys match the AutoKuma monitors, so the template is the list. (It
   does not read this file; the token names quoted above are prose and have drifted before.)
   **They must
   be exactly 32 alphanumeric chars** (Kuma rejects others; for example `openssl rand -hex 16`);
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
   `sudo k3s kubectl -n homelab exec deploy/monitor-bridge -- python /app/cli.py --once` — the
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

## The two configuration refactors

The thresholds became fields on a frozen `Config` on 2026-09-04, replacing 118
`monkeypatch.setattr(bridge.config, "X", ...)` sites and the two `importlib.reload(bridge.config)`
tests. Until 2026-09-01 the patch-boundary rule was the inverse of today's — a function could
leave `check.py` only if nothing patched it — and that capped `check.py` near 2,500 lines,
because every `check_*` reads `_get_json` or a threshold. The two tests that re-derive
`PROM_ORIGIN` from the environment used to `importlib.reload(bridge.config)`; since the seam
they call `load_config({...})` with the two Prometheus URLs they mean, which asks the same
question without mutating the process. The registry and the gates took the same shape on
2026-09-05. The module split began in commit
`9e7040cf0` (seven slices, all
landed 2026-09-01, `check.py` from 3,732 lines to ~510; slice 17b took the last 675 down to the
run loop alone on 2026-09-05).

## Traps: the incidents behind the rules

The rule each of these produced is in the role `CLAUDE.md`'s *Traps* section; this is the
evidence.

### kube-state-metrics sanitizes resource names into labels
`kubectl describe node` prints `devic.es/dri`. kube-state-metrics emits
`kube_node_status_allocatable{resource="devic_es_dri"}` — every character outside
`[a-zA-Z0-9_]` becomes `_`. A query written with the Kubernetes name matches no series, on a
cluster where both nodes advertise the resource at capacity 4.

A check designed to fail closed on an absent series cannot tell "the resource is
deregistered" from "I asked the wrong question." Both return an empty vector. The 2026-08-20
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
no `app`. `LOKI_DOCKER_STREAM` already used `container=~".+"`; the `ip_ban` arm added 2026-08-23
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
from "nothing matched." Do that before trusting any new log- or metric-backed arm; a green first
cycle is not evidence. `LOKI_STREAM_LABELS` +
`test_loki_selectors_use_real_stream_labels` in `test_check_loki.py` now pin the vocabulary
for all three Loki selectors.

### The runtime stamps the log lines; `bridge.common.log` does not
`bridge.common.log` prints the bare message — `DOWN b2_reachable ...` — and the container
runtime supplies the time. Pass `--timestamps` to `kubectl logs` when building a timeline, and
read that prefix as UTC.

It used to print its own bracketed stamp, and that stamp was the trap. `time.strftime` with no
offset rendered the container's local America/Chicago wall clock while looking like an ISO
instant. Verified 2026-08-16: bracketed `07:26:57` paired with kubectl's `12:26:57Z`. Reading
the brackets as UTC shifted a B2 cap-breach 5h early and pointed the investigation at the wrong
window — a "03:09 breach" that was really 08:09 UTC, minutes after the 07:30 weekly reboot. The
stamp was dropped rather than made offset-aware, because the runtime already supplies one and two
stamps that disagree is the whole fault.

**A log line archived before this change still carries the bracket, and it is still Central.**
Any homelab container that sets `TZ=America/Chicago` and stamps its own lines has the same
problem; cross-check one line against `date -u` before anchoring an incident timeline on it.
