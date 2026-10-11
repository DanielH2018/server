# monitor-bridge checks: the per-check record

The measurements, incidents and retirements behind every threshold check the
`monitor-bridge` pod pushes to Uptime Kuma. The per-check rule (what it reads, the threshold
and the arms) lives in the `verdicts/` function that each `files/check_table.py` row names.
`ansible/roles/k8s/monitor-bridge/CLAUDE.md` carries what holds across all checks: the gates,
the hysteresis and the module layout. `docs/monitor-bridge-internals.md` owns the module table,
the operator prerequisites, the push-token plumbing and the gate-set membership. This page
records where each number came from and which incident added each arm.

`files/check_table.py`'s `CHECKS` is the authority on which checks exist, and the
*Live checks* section opens with a table generated from it. A test
(`test_fragments_bridge.py`) holds the bullet titles in *Live checks* to that table: every
live check has a bullet, and every bullet names a live check. The prose inside a bullet is not
tested. If a bullet disagrees with the registry, the registry is right.

The bullets group the checks by domain, not in registry order. Each is named by its Kuma tile,
and the row's `name` in `files/check_table.py` is the key the code uses.

**HISTORY —** The Docker-era plumbing (compose, bind mounts, networks) is in git:
`git show 2460d0675fd748e70fcbcde87185371ffd62402b:ansible/roles/containers/archive/monitor-bridge/`.
The kopia backup checks retired with kopia on 2026-08-10, and the backup plane is Longhorn
(`docs/archive/k3s-migration/backup-consolidation-longhorn.md`).

## Live checks

Each row below is one Kuma push monitor. The table is generated from `CHECKS` and lists every
check and gate in registry order. The bullets after it carry the measurements and incidents
behind each tile, under the tile's display name.

--8<-- "assets/generated/fragments/bridge-checks.md"

The bridge evaluates the four reachability gates first. The internals page lists which
checks each gate suppresses.

- **Prometheus Reachable** (a trivial `vector(1)` instant query: the root-cause GATE for the
  prom-dependent checks, evaluated FIRST each cycle. When Prometheus is unreachable, every
  prom-dependent check (each row of `files/check_table.py` with `gate="prometheus"`) is
  **suppressed**, pushed `up` with a "skipped — Prometheus unreachable" `msg` so its push-monitor
  heartbeat stays alive, and only THIS monitor pages. Without the gate one Prometheus outage fires
  all of them at once. A single scrape target down (Prometheus up, one exporter gone) still
  surfaces separately on Scrape Targets. `PROM_DEPENDENT` derives from the `gate="prometheus"`
  rows, so neither a count nor an enumeration of it is written anywhere else. The role CLAUDE.md
  once carried a hardcoded count of ten while the set held more (#1359), and then an enumeration a
  test pinned, until the column made both redundant (#3788).)
- **Root Disk** (`node_filesystem_*` for `/`, `/boot` **and `/boot/efi`**: old kernels filling
  /boot quietly break upgrades, and a full ESP breaks firmware/bootloader updates the same way.
  Server-only; the Pi's disk lives in the Pi Pressure check. Root Disk pages without remediating:
  containerd's image GC owns image cleanup since the Docker daemon retired, and no autoprune cron
  replaced the old one.)
- **TLS Cert Expiry** (`traefik_tls_certs_not_after`, per-series, so the message names each
  breaching certificate's `cn`. The query aggregates no label away: an earlier `min(...)` left the
  tile promising a name the message could not carry (#3101). The difference is latent while
  Traefik's store holds the one wildcard.)
- **Memory** (host `node_memory_*` pressure only)
- **Host coverage floor** — not a monitor of its own, but a second arm inside **Root Disk** and
  **Memory** (`_host_origin_shortfall`). Both checks group by `origin`, and node-exporter is a
  DaemonSet on both nodes, so a vector covering fewer than `HOST_ORIGINS_MIN` (2) distinct hosts
  has **lost** a host rather than measured a healthy estate, and the surviving node's numbers
  would be reported as the estate's. This happened once: a one-directional UFW rule left
  daniel-box's node-exporter unreachable for 5.4h (2026-08-23), and both checks pushed OK off
  daniel-server alone while daniel-box's host memory and `/boot` went unwatched.
  **Why not lean on Scrape Targets:** that keys on `up`, and node-exporter's normal failure is
  PER-COLLECTOR. A filesystem or meminfo collector can fail with `up == 1`, leaving Scrape Targets
  green while the host silently drops out of these two checks. This is the shape of `check_ups`'s
  partial-absence arm: never monitor the survivor silently.
  `HOST_ORIGINS_CONSECUTIVE` gives it hysteresis, for the reason `UPS_CONSECUTIVE` exists: the
  weekly Sunday restart takes a node's exporter away against a 1m scrape and a 5m loop. The
  shortfall is reported AFTER each check's own breach scan, because a reporting host that is
  genuinely out of memory outranks a complaint about the absent one. `HOST_ORIGINS_MIN` is
  rendered in `templates/env-secret.yaml.j2` so a planned single-node maintenance window can lower
  it and put it back. **At 1 the arm is inert**, which is the original failure, so treat it as a
  temporary setting rather than a fix for a noisy tile.
- **Claude Code cgroups** — not a monitor of its own either, but a third arm inside **Memory**
  (`with_claude_cgroups`, #1258). It reads the `claude_cgroup_*` node-exporter textfile series
  (PR #1251) for `claude-rc.service` and `user.slice/user-1000.slice`, on every host with
  `has_claude_code: true` (daniel-box and daniel-server). Both hosts emit the same cgroup names, so
  the two queries group by `(origin, cgroup)` / `(origin, cgroup, event)` and the verdict's message
  names `origin/cgroup` (for example `daniel-server/fleet 40.0%`).
  It is folded into Memory rather than given its own tile for the reason recorded at
  `with_pi_ports`: a new Kuma monitor costs a new push token in SOPS and a monitor created by hand
  in the UI, and **Memory** already owns "this box is running out of memory," which a cgroup
  taking the box is. On 2026-09-05 (#1243) one cgroup held all 8 GiB of the box's swap plus 6.96 GB
  anon and stalled in reclaim for about ten minutes before anything downstream failed.
  Three sub-arms, reported in this order: a watched `memory.events` counter increasing
  (`CLAUDE_CGROUP_EVENTS` = `max|oom|oom_kill|oom_group_kill`, paging on ANY increase, because a
  kill outranks a stall), the PSI `full` stall rate over `CLAUDE_CGROUP_STALL_MAX_PCT`, and an
  expected cgroup not reporting at all. `high` is deliberately NOT alerted on: MemoryHigh
  throttling is the cap working, and its value belongs to `roles/setup/claude_code`, so an arm
  keyed on it would move whenever those caps move. It is graphed instead on the
  `AI/claude-code-host-cgroups` board, which carries an `origin` template variable.
  `CLAUDE_CGROUPS` (rendered in `templates/env-secret.yaml.j2`, `claude-rc,fleet`) is the set
  whose ABSENCE is a fault, not the set that is judged. The queries filter by metric, so
  `user-1000-slice` is watched whenever it exists and its absence never pages, because that cgroup
  only exists once somebody has logged in since boot. **Empty disables the whole arm.** The full
  threshold derivation, settled against 5.8 days of history that included both cgroups hitting
  their MemoryHigh caps (#1288), is at `CLAUDE_CGROUP_STALL_MAX_PCT` in `bridge/config_host.py`.
- **k3s Container Restarts** (`changes(container_start_time_seconds[15m]) > RESTART_MAX`)
- **k3s Container OOM** (`increase(container_oom_events_total[1h]) by (name)` — names the
  offender; supersedes the old host-aggregate OOM that lived in the Memory check)
- **k3s CPU Throttling** (throttled/total CFS *periods* `> CPU_THROTTLE_PCT` **and** throttled
  *seconds*/s `> CPU_MIN_THROTTLED_CORES`, by name. It catches a container pinned at its
  `deploy.resources` CPU cap, which throttles silently without OOM, restart or 5xx. The cores floor
  (the volume-floor idea of Traefik's `TRAEFIK_MIN_RPS`) is essential: the period ratio alone runs
  30-90% for tiny low-limit sidecars that briefly burst over their slice while losing negligible
  absolute CPU, a perpetual false `down`, which Kuma renders as "No heartbeat in the time window"
  because only `up` pushes satisfy a push monitor's watchdog. Unlimited containers give 0/0→NaN
  and are ignored. `CPU_CONSECUTIVE` (3) adds hysteresis on top of both gates: only the third
  consecutive breaching cycle (~15 min) pushes `down`, shorter bursts push `up` with a "throttling
  streak n/3" `msg` naming the offender, and a clean cycle resets the streak, so one-cycle blips
  (flaresolverr solving a challenge, homepage hugging the cores floor) never page.)
- **Scrape Targets** (`up == 0` — names the down job)
- **Traefik 5xx** (5xx ratio over 5m **per service**, naming each offender, gated by a
  per-service `TRAEFIK_MIN_RPS` volume floor. It is per-service so the alert points at the
  erroring backend and a broken low-traffic service cannot hide diluted in the aggregate)
- **Traefik Latency** (share of requests slower than a histogram BUCKET BOUNDARY, per service,
  behind the same `TRAEFIK_MIN_RPS` floor: the gap the 5xx check cannot close, since a degraded
  backend still answers 200. **Not `histogram_quantile`**: Traefik's default buckets are
  0.1/0.3/1.2/5.0/+Inf, so a quantile landing between 1.2s and 5.0s is interpolated across an
  empty 3.8s-wide gap, and an earlier 3s threshold sat inside it. Every firing was that arithmetic
  (homepage@docker read 4.058s on 2026-08-06 where the Traefik access log showed a real p95 of
  1.576s). Bucket counts are exact, so ">`TRAEFIK_SLOW_PCT` (5%) of requests over
  `TRAEFIK_SLOW_BUCKET` (5.0s)" IS "p95 over 5.0s" without interpolation. Keep
  `TRAEFIK_SLOW_BUCKET` on a boundary Traefik actually emits: an `le=` matching no series is
  reported as a config fault rather than read as 0 requests under the boundary, which would page
  every service at once.
  **Two independent guards** came from this being the flappiest monitor in the estate (51 DOWN
  episodes in 30 days to 2026-09-11). `TRAEFIK_STREAM_SERVICES` (`homelab-headlamp-`,
  `homelab-home-assistant-`, `homelab-uptime-kuma-`) exempts services whose traffic is LONG-LIVED
  CONNECTIONS. Traefik's histogram times a request until the response completes, so a websocket,
  an SSE stream or a Kubernetes watch sits past every bucket edge while healthy, and no
  `TRAEFIK_SLOW_BUCKET` fixes that because the buckets end at 5.0s. Headlamp ran 94.2% of requests
  under 0.1s and 3.1% past 5.0s with a whole-service mean of 1.7s, which puts that tail's own mean
  near 53s. The match is a PREFIX of the service label, which is
  `homelab-<name>-<hash>@kubernetescrd`, because the hash moves when an IngressRoute is renamed.
  The exempt count is named in the green message, because an exempt service is not measured and a
  bare "ok" would overstate coverage. **bazarr and prowlarr are deliberately NOT exempt**: an
  *arr indexer search genuinely takes 5-30s and a user genuinely waits for it, which is the
  user-visible slowness this check exists to catch.
  `TRAEFIK_SLOW_MIN_REQUESTS` (3) requires an absolute count of slow requests behind the ratio.
  `TRAEFIK_MIN_RPS` admits a service with 0.05 x 300 = 15 requests in the `[5m]` window, so ONE
  request past the bucket is 6.7%, already over `TRAEFIK_SLOW_PCT`, and the ratio alone pages on a
  single request for every low-traffic route. The count is derived from the window and the floor,
  not fitted to an observed service.)
- **Traefik 404 Flood** (404 share of **entrypoint** traffic over 5m, behind the same
  `TRAEFIK_MIN_RPS` floor: the gap BOTH checks above leave open (#1322). They are per-SERVICE, and
  an edge that has lost its routers emits no `traefik_service_*` series at all, so their loops
  iterate an empty vector and report `0 service(s) above floor` while every route 404s. The
  entrypoint counter is incremented before routing, so it survives that. `TRAEFIK_404_PCT` is 90
  rather than a low number because a homelab edge serves a steady 404 trickle: 4.0% of 0.83 rps
  when healthy against 100% of 0.61 rps during the outage (2026-09-06).)
- **Traefik 421** (per-**router** 421 rate over 5m, #2757: the failure all three checks above
  miss. `SNICheck` pins the TLS-options name once per connection, so a client that connects while
  Traefik is still reconciling routes after a restart gets 421 for the life of that connection.
  The request never reaches a service, and its code is not 404. Three such wedges went unseen in
  the week to 2026-09-27 (authelia for 14.5h, the Pi's Alloy for 4h15m (#2747) and the apiserver's
  OIDC fetches for 5h30m (#2749)) at steady-state rates of 0.10-0.18 rps. The only other 421 in
  that week was one 5-minute window on uptime-kuma at 0.0083 rps. `TRAEFIK_421_RPS` is 0.02, an
  absolute rate rather than a share, and `TRAEFIK_421_CONSECUTIVE` (3) holds a one-shot mismatch
  below a page: it stays inside a `[5m]` rate for at most two evaluations.)
- **n8n Prod Workflows** (n8n public API: per-*active*-workflow **consecutive-failure streak**.
  n8n does not save successful executions (`EXECUTIONS_DATA_SAVE_ON_SUCCESS=none`, to bound
  `database.sqlite` and its B2 backup churn), so "consecutive" cannot be read from one snapshot.
  `check_n8n` accumulates the streak ACROSS cycles, deduped by execution id so a lingering failure
  is not recounted, and resets a workflow's streak once its latest error ages past
  `N8N_FAIL_WINDOW` (recovered or idle). `down` when any workflow fails `N8N_CONSECUTIVE_MAX` (3)
  times in a row, OR when `N8N_SYSTEMIC_MAX` (2)+ workflows are each failing `N8N_SYSTEMIC_STREAK`
  (2)+ times. The second condition is the n8n-wide catch that pages promptly as ONE alert instead
  of waiting for each workflow to hit the consecutive threshold, and instead of a per-workflow
  flood. "Prod" means active. Empty `N8N_API_KEY` disables the check (stays up), and an
  unreachable API surfaces as `down`. The check reaches `n8n:5678` over `apps`, bypassing Authelia
  via the `X-N8N-API-KEY` header. The streaks live in `bridge.streaks.State` (`n8n_streaks`), so
  they reset on a bridge restart, which the STARTUP_GRACE hysteresis rides out. Pure
  `n8n_update_streaks()`/`n8n_verdict()` are unit-tested.)
- **Arr Queue Warnings** (sonarr's + radarr's own `/api/v3/queue`: `down` on any item with
  `trackedDownloadStatus == "warning"`, `trackedDownloadState == "importBlocked"`, or
  `importPending` carrying `statusMessages`, naming the release title + app. The check exists
  because an indexer once served a poisoned fake-episode `.exe` (2026-07-01): sonarr blocked the
  import and flagged the queue item `warning`, but nothing paged and the release sat seeding for a
  day. `SONARR_API_KEY`/`RADARR_API_KEY` are independent: an empty one skips that app, and both
  empty disables the whole check (stays up), like `N8N_API_KEY`.
  An unreachable *arr API rides `ARR_FETCH_CONSECUTIVE` (3 cycles = 15 min). That DIVERGES from
  `check_n8n`/`check_scrutiny`, which let the error bubble to `_evaluate` and page at once. The
  *arrs are Deployments this same bridge watches rolling, so their API refuses connections every
  time k3s replaces the pod, and three of this monitor's DOWN episodes in the 30 days to
  2026-09-11 were a fetch error co-timed with a `k8s_workloads ... radarr(1)` episode, one rollout
  reported twice. The streak covers the FETCH only: a queue item needing review pages on the cycle
  it is seen, because a poisoned release in the queue is not a transient. **The streak REPLACED
  `arr_queue`'s `STARTUP_GRACE` membership** rather than stacking on it, since that grace covered
  the same transient at 2 cycles and covered the queue verdict too. Pure `queue_warnings()` is
  unit-tested, and the fetch streak has its own accept/reject pair in `test_check_service.py`.
  **Sonarr's self-clearing title holds are the one queue reason that waits**, held for
  `ARR_TITLE_HOLD_GRACE_H` (48, #2786). Three of this check's seven DOWN episodes in the 14 days to
  2026-09-27 were "Episode has a TBA title and recently aired" (2.9h, 7.4h, 2.9h): Sonarr applies
  that hold itself and releases it once the title arrives, so the page named no action a human
  could take. 48h is upstream's own window: Sonarr's `EpisodeTitleSpecification` stops applying
  the rule once the episode aired more than 48h ago, so an item still carrying the message past
  that is stuck, not waiting. The item's `added` timestamp is the clock. The hold is narrow
  deliberately: it needs EVERY reason on the item to be self-clearing (the TitleTba and
  TitleMissing messages), it never applies to `importBlocked`/`importFailed` or to
  `trackedDownloadStatus == "error"`, and an item with no readable `added` timestamp is flagged
  rather than held. The other four episodes in that window were "Not a Custom Format upgrade for
  existing episode files" and still page on sight.)
- **Bazarr Health** (bazarr's `/api/system/status` + `/api/system/health` over `media`: `down`
  when a peer version field is present-but-empty, or when bazarr self-reports a health issue.
  **Bazarr is the *arr with no exporter, and that is the whole point.** It holds its OWN copies of
  Sonarr's and Radarr's API keys, in its config on the `bazarr-config` PVC and entered through its
  UI, so no Ansible template carries them and no deploy updates them. The 2026-08-29 key rotation
  swept the eight templated consumers and missed bazarr, which reconnect-looped, leaked to its 1Gi
  cap and was OOM-killed, and the only signal was the "k3s Container OOM" tile, which clears one
  hour after the kill. Sonarr's and Radarr's own stale keys surface immediately as failing
  exportarr scrapes, and bazarr had nothing.
  The peer-version fields are the detector: bazarr fills `sonarr_version`/`radarr_version` by
  calling each app with its stored key, so an empty one is a rejected key seen from outside. An
  **absent** field is ignored, because bazarr omits it when that integration is switched off and
  alerting there would page forever. The header is `X-API-KEY`, not the `X-Api-Key` that
  sonarr/radarr take. Empty `BAZARR_API_KEY` disables the check (stays up). An unreachable bazarr
  surfaces via `_evaluate`, which is also how the 401 from a stale `bazarr_api_key` in SOPS shows
  up. Pure `bazarr_problems()` is unit-tested both ways.
  **Not an exportarr sidecar, deliberately.** exportarr speaks bazarr, but at the pinned v2.3.0 its
  collector always runs the full episode-subtitle walk (upstream measures it in "tens of
  seconds," spent *inside* bazarr), and v2.3.0 predates the overlapping-collection skip upstream
  added for that drainage (their #380). These two endpoints measured 2-7 ms and 477+13 bytes on
  2026-08-29.)
- **Prowlarr Indexers** (Prowlarr's `/api/v1/indexerstatus` + `/api/v1/indexer` over `media`,
  `X-Api-Key`. `down` only when an indexer has been failing ≥ `PROWLARR_INDEXER_MIN_DOWN_MIN` (1
  week = 10080 min: only a genuinely long outage pages, and short flaps are noise), measured from
  Prowlarr's own `initialFailure`. That is the age-based, per-indexer SUSTAINED signal Prowlarr's
  binary in-app health notification cannot express (it warns on every flap or when all indexers
  are down). It suppresses the transient tracker flaps that self-clear inside Prowlarr's ~5-15 min
  backoff. It is age-based, not consecutive-cycle, so it survives a bridge redeploy mid-outage.
  Empty `PROWLARR_API_KEY` disables the check (stays up). A null or unparseable `initialFailure`
  is skipped. An unreachable Prowlarr surfaces as `down` through `_evaluate` (the
  `check_n8n`/`check_bazarr` convention, no grace). It pairs with Prowlarr set to
  `includeHealthWarnings=false`, which keeps `onHealthIssue` as the instant all-down red backstop.
  `PROWLARR_INDEXER_IGNORE` (comma-separated names, case-insensitive) drops chronically flaky
  public trackers from the offender list. It is set to `The Pirate Bay,1337x`, because their
  backends flap for hours and the remaining indexers cover the same searches. Pure
  `indexers_down()` is unit-tested.)
- **GitOps Deploy — Alive** (reads `/gitops-state/last_run`, a bind-mounted host timestamp the
  `gitops_deploy` deployer rewrites each non-crashing tick. `down` once it is older than
  `GITOPS_MAX_AGE_MIN`, which means the deployer stalled or the host is down. The deployer does
  not push to Kuma itself; see `ansible/roles/setup/gitops_deploy/CLAUDE.md`)
- **GitOps Deploy — Status** (reads `/gitops-state/hold_sha`, `/gitops-state/diverged_sha`,
  `/gitops-state/contention_since`, `/gitops-state/manual_plane` **and
  `/gitops-state/behind_since`**. `down` while a rolled-back commit is held pending the operator
  reverting the offending PR (self-heals when the hold clears), OR while local and origin have
  **diverged**, so the deployer cannot fast-forward and silently noops forever (the deployer
  records the diverged SHA each tick, 2026-07-15 review L3), OR while the host has sat **behind
  origin** longer than `GITOPS_BEHIND_MAX_MIN` (360 = 6 h).
  That last arm is the general case the other two are instances of. A deferred **broad** change
  never fast-forwards, so the host parks on an old tree with `last_run` still ticking and
  `is_diverged` false (origin is a strict descendant). daniel-server ran a 12-commit-old tree for
  hours on 2026-08-02 with every GitOps signal green. The arm is age-gated, not presence-gated: a
  routine push is behind for one tick, and the deployer's dirty-tree path is behind for a whole
  edit session by design, so only sustained behind-ness is a fault. hold/diverged are reported
  ahead of it, because they name the cause where "behind" names the symptom.
  The **busy service lock** arm (`contention_since`, #1847) comes next. A tick that finds an
  operator `deploy.sh` still holding a service lock for its whole budget resets its tree and
  returns 0, so `last_run` advances, `hold_sha` stays empty and only `behind_since` ages toward the
  six-hour threshold, which is sized for a dirty tree. The deployer records the streak's first tick
  in `contention_since`, and this pages once it is older than `GITOPS_CONTENTION_MAX_MIN` (30 = the
  deployer's longest apply budget, `gitops_deploy_broad_timeout_s`), naming the lock. The tick
  clears the marker on its next run that is not deferred, and `gitops_state.py clear-contention`
  clears it by hand.
  The last arm is **`k8s_deferred`** (#2449): a promoted image bump that a broad tick
  fast-forwarded and then deferred, because the shared budget left less than
  `K8S_DEPLOY_TIMEOUT_S`. The range is merged, so `behind_since` is empty and no later tick's range
  carries the bump, and the defer-and-alert post names it once and then nothing does. This arm
  pages once the oldest line is older than the same six hours, naming the
  `./scripts/deploy.sh --tags <svc>` that applies it and the
  `gitops_state.py clear-owed k8s_deferred <svc>` that follows. Any tick that deploys the service
  clears the line itself. The arm reads the `owed` ledger's `k8s_deferred` class (#3392). Pure
  `gitops_status()` and its parsers are unit-tested, and an unparseable marker reads as not-behind
  rather than paging forever on garbage.)
- **B2 Reachable** (authenticates against B2's native API (`b2_authorize_account`, Basic auth with
  `B2_PROBE_KEY_ID` + the file-mounted `B2_PROBE_APPLICATION_KEY_FILE`), because Longhorn's own
  B2-backed backups need it. The gate comes from the 2026-08-02 transaction-cap incident
  (`docs/archive/b2-transaction-cap-monitoring-gaps.md`): B2 caps **transactions** separately from
  storage bytes, and five kopia-era state-file checks read green for nine and a half hours because
  they reported their cron's LAST SUCCESSFUL RUN rather than current B2 health. Those checks left
  with kopia, and backup moved to Longhorn
  (`docs/archive/k3s-migration/backup-consolidation-longhorn.md`). `B2_DEPENDENT` holds
  `check_b2_storage` (B2 Storage Usage), which queries B2 live and is gated here, so a
  transaction-cap incident does not page twice for one root cause. This check reports B2's own
  `transaction_cap_exceeded` error text directly.
  **Throttled**, unlike the other two gates: the probe runs at most once per
  `B2_PROBE_INTERVAL_S` (1800 s = 48 calls/day) and a BILLED outcome is cached, because the fault
  being detected is a transaction cap and an every-cycle probe (288/day), or `email_backstop`'s
  cache-successes-only idiom (which retries on failure), would spend the budget it is watching.
  The cached verdict is still pushed every cycle so the push monitor's heartbeat stays alive.
  Empty credentials disable the check (stays up).
  **A failure that never reached B2 takes a short TTL instead** (`B2_TRANSPORT_RETRY_S`, one
  INTERVAL). Only an answer from B2 costs a transaction, so the argument for the long cache does
  not cover a DNS/connect/timeout failure, and `_get_json`'s contract makes the two separable: it
  re-raises `HTTPError` untouched and wraps everything else as `RuntimeError`. Caching both alike
  pinned this gate DOWN for 25 minutes after an 8m35s outage (2026-08-30), because the bridge's
  first cycle probed B2 before cluster egress was serving. The cache was holding back the
  RECOVERY, not just the retry.
  A test that fakes a real cap denial must raise `HTTPError`, because a `RuntimeError` exercises
  the transport path and proves the opposite of what it claims. `_cap_denial()` in
  `test_check_gates.py` does this. Tests guard `B2_DEPENDENT` against the live `CHECKS` and against
  `STARTUP_GRACE`.
  **Assumption, stated in the code because it cannot be tested without a live breach:** that
  `b2_authorize_account` is itself subject to the cap (Backblaze's endpoint docs list
  403/`transaction_cap_exceeded` among its errors). If a future breach leaves this monitor green,
  point `B2_PROBE_URL` at a Class C call instead; it is a URL swap by design.)
- **B2 Free Tier Headroom** (`files/checks/b2.py:b2_storage_usage`. Lists every file version
  in the bucket the `B2_PROBE_*` key is scoped to, sums `contentLength` including hidden
  versions and unfinished large-file parts, and compares the total with `B2_STORAGE_CAP_BYTES`
  (10 GB). `down` past `B2_STORAGE_MAX_PCT` (80). Those hidden bytes bill as stored data, so a
  plain object listing reads lower than the invoice. A listing that hits `B2_STORAGE_MAX_PAGES`
  (50) before the cursor clears is `down`, not a smaller number: the message calls the total a
  FLOOR. A key with no `bucketId` also reads `down`, because a bucket-scoped key is the only
  kind that can size one bucket. A success is cached for `B2_STORAGE_INTERVAL_S` (86400), since
  each 1000 versions costs one Class C call and a budget guard must not be a real part of the
  spend. A failure is not cached, because `b2_reachable` owns the cap signal and a listing
  failure is more often a 5xx. Empty credentials disable the check. **In `B2_DEPENDENT`**: a
  transaction cap fails this check and B2 Reachable together, and one root cause must not light
  two tiles. Runbook: `longhorn-backup-tiering`.)
- **R2 Free Tier Headroom** (Cloudflare's GraphQL Analytics API, `r2StorageAdaptiveGroups` +
  `r2OperationsAdaptiveGroups` in ONE POST: month-to-date storage bytes, Class A and Class B
  operation counts as a percentage of the free tier (10 GB / 1M / 10M), `down` past
  `R2_USAGE_MAX_PCT` (80) on any arm. **Cloudflare offers no spending cap or usage limit on R2, on
  any plan** (the Usage-Based Billing notification needs a Pro plan and this account is Free), so a
  watched threshold is the only boundary that exists. It is a headroom guard, not a bill-shock
  alarm: overage is $0.015/GB-month, $4.50/M Class A, $0.36/M Class B with egress free, so the
  realistic worst case is under a dollar. It catches a **runaway client** while the fix is still a
  config edit, the shape of the 2026-08-13 Longhorn retry storm (~2.5k B2 Class B/day against a
  hard cap).
  A fourth arm counts **outstanding incomplete multipart uploads** (`R2_UPLOADS_MAX`, 25): they
  bill as stored bytes and do NOT appear in an object listing, which is the quiet way a 10 GB
  budget fills. The durable fix is a bucket lifecycle rule (set by hand, per the internals page's
  prerequisites), and this arm is the backstop for that rule being absent, deleted, or not working.
  Operation classes are NOT in the API response, because Cloudflare returns raw `actionType`
  names, so the Class A / Class B mapping lives in `verdicts/storage.py` (`R2_CLASS_A_ACTIONS`)
  from the pricing page. An `actionType` in neither published list counts toward **Class A** (the
  tighter, more expensive arm) and is named in the message: over-counting reports headroom we do
  not have, which is the safe direction, and the name explains why the numbers moved when
  Cloudflare adds an operation.
  **SUCCESSES are cached for `R2_PROBE_INTERVAL_S` (1800 s); a failure is NOT.** That is the
  inverse of `b2_reachable`'s cache-both, deliberately: B2's cached failure protects a spend cap
  that retrying would deepen, whereas GraphQL analytics calls are free and count against no R2
  budget. The check follows `EMAIL_PROBE_INTERVAL_S`'s cache-successes-only idiom, and the WAN gate
  absorbs a transient Cloudflare blip instead of a stale verdict.
  A 200 carrying a populated `errors` array (how an under-scoped token arrives) is raised, not
  parsed: unchecked it reads as a zero-usage bucket, a monitor green because it is blind.
  **The free tier is per-ACCOUNT; this query filters by `bucketName`.** The two are identical
  while there is one bucket, and the filter silently under-reports the day there is a second, at
  which point drop the `bucketName` filter rather than raising the thresholds.
  Empty `CF_ACCOUNT_ID`/`CF_ANALYTICS_TOKEN`/`R2_BUCKET` disables the check (stays up). Pure
  `r2_month_start()`/`r2_classify_operations()`/`r2_usage_verdict()` are unit-tested.)
- **Cloudflare IP Drift** (`checks/cloudflare_ips.py`: the two published range pages against
  `CLOUDFLARE_IPS_EXPECTED`, the `cloudflare_ips` allowlist traefik trusts and netpol-baseline
  admits. A success is cached for `CLOUDFLARE_IPS_PROBE_INTERVAL_S` (a day). Drift, a page with
  under 10 ranges, or a fetch that did not answer is DOWN and re-probed every cycle, so the tile
  clears one cycle after `cloudflare_ips` is fixed. That is why the check lives here and not in
  k8s/traefik's daily root cron, which left a red tile until the next 05:25. Empty expected list
  disables the check (stays up). Pure `cloudflare_ips_verdict()` is unit-tested.)
- **Healthchecks.io Console Drift** (`checks/healthchecks.py`, #2566: compares the console's
  schedule type, period or cron expression, timezone and grace for each slug with
  `monitor_bridge_healthchecks_expected`. `docs/healthchecks-io-deadman.md` owns the expectation
  table, the cache and the history of the drift it caught. An undocumented console check is named
  and does not page (the `DECIDED:` in `healthchecks_verdict`). Gate: `wan_reachable`. An empty key
  or expected list disables it, and it stays up.)
- **SMART Data / Health** (Scrutiny's web API `/api/summary` over `monitoring`: every
  non-archived device must have a `collector_date` within 26 h **AND a passing `device_status`**
  (0 = SMART self-assessment and Scrutiny's attribute thresholds both OK; non-zero decodes to
  "SMART self-assessment FAILED" / "attribute threshold breached"). Freshness catches a silently
  dead collector (cron-as-PID1, no usable healthcheck, so it only shows as aging data). The status
  check catches a drive that goes SMART-FAILED or breaches a threshold while STILL reporting fresh
  data. Nothing else alerts on that: Scrutiny writes to InfluxDB not Prometheus, and its own
  Shoutrrr notifier is unconfigured, so this bridge check is the only drive-failure alert path.
  Also `down` when Scrutiny lists no devices at all. `SCRUTINY_TEMP_MAX` (°C, default 0 = off) adds
  an optional early warning temperature ceiling.
  **A third arm watches NVMe endurance**: `percentage_used` against `SCRUTINY_WEAR_MAX` (default
  80). Scrutiny ships that attribute with `thresh=100`, so its own evaluation cannot fold a breach
  into `device_status` until the drive's rated write endurance is fully spent, while the wear
  curve offers months of warning where `device_status` offers days. It is the one arm NOT served
  by `/api/summary`, whose `smart` block carries `collector_date`, `temp` and `power_on_hours` but
  no wear attributes. It costs one `/api/device/<wwn>/details` fetch per non-archived device per
  cycle (~19 KB each, only `smart_results[0]` read), taken after freshness passes so a dead
  collector costs no per-device calls. A device that reports no `percentage_used` is **unwatched,
  not healthy**: the message names it, and says INERT when no device reports the field at all.
  Missing must not page, because `percentage_used` is NVMe-only and a SATA disk added later
  legitimately has none. Pure `scrutiny_freshness()`, `scrutiny_health()`,
  `scrutiny_device_wear()` and `scrutiny_wear_verdict()` are unit-tested; those tests mock the
  payload, so they prove the verdict logic and nothing about the endpoint path.)
- **Host Temperature** (board and CPU sensors from node-exporter's hwmon collector,
  `node_hwmon_temp_celsius`, with drives EXCLUDED: `HWMON_TEMP_EXCLUDE_CHIP` drops the `nvme_`
  chips because SMART Data / Health above owns them, and reading them here would double-page one
  condition. `check_cpu_throttle` sees CFS throttling, which is a cgroup limit rather than heat,
  and the Grafana **Hardware Temperature Monitor** panel pages nobody, so this check is the only
  host-temperature alert.
  Every sensor gets a limit from one of **two exhaustive arms**: `HWMON_TEMP_RATIO` (0.90) of its
  own declared max or crit where either is plausible (max preferred when both are), else the flat
  `HWMON_TEMP_FALLBACK_C` (85 °C). **The fallback is not a nicety.** Only 7 of 21 scraped sensors
  declare a usable max (2026-08-28), and both daniel-pi sensors declare none, so a
  declared-max-only check would be silent on two thirds of the estate.
  **The declared value is sanity-bounded, not trusted.** Three NVMe sensors declare 65261.85 (a
  0xFFFF sentinel for "no max"), and a ratio of that is unreachable, so those sensors would read
  green through a fire. A declared max or crit outside (`HWMON_TEMP_MIN_PLAUSIBLE_C`,
  `HWMON_TEMP_MAX_PLAUSIBLE_C`] is treated as UNDECLARED. An EMPTY sensor vector is `down`, not
  `up`: zero readings means EVERY collector went blind, and "nothing is too hot" from no data is a
  lie. `HWMON_TEMP_CONSECUTIVE` (12, one hour at `INTERVAL`=300) rides out a boost excursion, and
  it is ONE streak for the whole check rather than one per sensor.
  **`max` wins when a sensor declares a plausible value for both**, not `crit`: hwmon's convention
  has `crit` as the LATER emergency point, not an earlier warning (daniel-server's NVMe declares
  max 85.85 / crit 86.85), so ratioing `crit` would page closer to hardware failure than the
  max-based 90%. `node_hwmon_temp_crit_celsius` (#995) is the second declared source. It is read
  for every sensor, in case a driver declares `crit` without `max`, and is used only when `max` is
  absent or implausible for that sensor.
  **The message names hardware, not sysfs paths.** Two extra instant queries
  (`node_hwmon_chip_names`, `node_hwmon_sensor_label`) turn
  `daniel-box/pci0000:00_0000:00:18_3/temp1` into `daniel-box k10temp/Tctl`. Both lookups are
  partial (10 of 21 series carry a sensor label), so each half of the name degrades to its raw
  sysfs component independently, and an empty answer costs readability rather than the verdict.
  Each hot sensor also names the arm that set its limit, because a `declared` breach is the
  hardware calling itself too hot while a `fallback` breach may only mean the flat 85 °C does not
  suit that chip.
  **daniel-box's k10temp/Tctl declares neither `max` nor `crit`.** Read from
  `/sys/class/hwmon/hwmon2/`, only `temp1_input` and `temp1_label` (`Tctl`) exist, so no code
  change can make its alert a declared-limit one (#995). The offset question (#1003) is settled by
  the `DECIDED:` marker in `verdicts.host.hwmon_temp_limits`: k10temp reports `Tdie = Tctl -
  temp_offset` and sets `temp_offset` only for six family 0x17 SKUs, none of them this one, so a
  Tdie series would carry the SAME number as Tctl. The number is settled by a third arm (#1152).
  AMD publishes `Max. Operating Temperature (Tjmax)` = 100 °C for the Ryzen 7 8845HS, so
  `HWMON_TEMP_RATED_MAX_C` carries that rating for this one sensor as
  `instance/chip/sensor=celsius` and ratios it like a declared max (90 °C, the effective limit
  daniel-server's coretemp gets from its declared max of 100). A rating is seeded BEFORE `crits`
  and `maxes`, so a driver that starts declaring either still wins, and it passes the same
  plausibility gate, so a typo cannot un-watch the sensor. It is keyed by the raw sysfs triple, not
  the readable `daniel-box k10temp/Tctl`, because `hwmon_name_maps` is partial by construction and
  a name-keyed entry would stop matching in silence. The amd.com product page answers a fetch that
  carries a browser `User-Agent`, and a plain fetch fails.
  **The excursions are workload, so the remedy is hysteresis** (#1186, `HWMON_TEMP_CONSECUTIVE` 3
  to 12). Over a true 7 days to 2026-09-06 at the 5 min loop cadence (Prometheus retains ~11.4
  days, so the `[30d]` figures in #1152 and #1186 covered ~11 days, #1314), this sensor is above
  90 °C for **12.0 %** of samples (p50 52.875 °C, p95 93.125 °C, max 93.75 °C against the rated 100
  °C). Those 241 hot samples are 115 separate excursions, with run lengths in cycles of 66x1, 23x2,
  12x3, 5x4, 2x5, 3x6, 3x7, 1x8 and then a single 18 (90 min), and **nothing between 9 and 17**. 3
  cycles paged 26 times in that week, and 12 pages once, on the 18-cycle outlier, which is kept on
  purpose because 90 minutes pinned above 90 °C is the shape of a cooling fault. Every excursion
  falls in the 11:00-03:20Z band with an 8-hour overnight hole, which settles it as scheduled and
  interactive work rather than an idle-state thermal floor. `HWMON_TEMP_RATIO` was NOT raised,
  because it is estate-wide and this is one sensor's duty cycle. The derivation and the
  pages-per-week table are at the `DECIDED: 12 cycles` marker in `files/bridge/config_host.py`.
  **A PARTIAL blindness is a separate arm** (`HWMON_TEMP_ORIGINS_MIN`, review M-9). The
  empty-vector branch fires only when ALL hosts go quiet, so one host's hwmon collector could die
  while the other two answered "all below limit" for the estate. The arm reuses
  `_host_origin_shortfall`, the helper Root Disk and Memory use, with its OWN floor: **3, not the
  shared `HOST_ORIGINS_MIN` of 2**, because all three hosts declare non-excluded sensors (9 / 5 / 2
  on 2026-08-29), so a floor of 2 is met by any two of them. Origins are counted over the series
  that survive `HWMON_TEMP_EXCLUDE_CHIP`, through the same predicate `hwmon_temp_limits` uses,
  because a host whose only sensors are `nvme` is a host this check does not cover.
  `HWMON_TEMP_ORIGINS_CONSECUTIVE` (5) is **longer than `HOST_ORIGINS_CONSECUTIVE`** (3) on
  purpose: the third host is the Pi, and its hwmon series went absent for about 20 minutes over 7
  days (6 of 1054 samples at a 5m step), which the shared 15-minute grace would have paged on. The
  two hysteresis mechanisms never compound: `down_streak` is the thermal-spike grace and applies
  only to the hot-sensor path, so a missing host pages on its own fifth cycle rather than the
  fifteenth. The arm is why `host_temp` is in `EXPORTER_DEPENDENT` under **two** job keys, `node`
  and `node-pi`: a dead node-exporter trips the floor, and the Pi scrapes under its own job
  (`job=node` for daniel-server and daniel-box, `job=node-pi` for daniel-pi), so a `node`-only
  entry suppresses two of the three hosts and leaves the Pi double-paging. Pure
  `hwmon_temp_limits()` and `hwmon_temp_verdict()` are unit-tested in `test_host_temp.py`, each
  rule as an accept/reject pair. The coverage test is the load-bearing one, since this check's
  failure mode is silence rather than a wrong threshold.

  **Two more arms** (#1471), each with its own streak key. Both signals were plotted on
  `Infrastructure/hardware-thermal.json` (PR #1463) and alerted on by nothing. They are folded in
  here rather than given their own monitors for the reason `check_scrutiny`'s wear arm records: a
  new Kuma monitor needs a new push token in SOPS, and both answer the question the temperature
  arm does, which is whether the hardware is being damaged right now.

  - **Undervoltage** (`_undervoltage_arm`, `UNDERVOLTAGE_QUERY` =
    `node_hwmon_in_lcrit_alarm_volts`). The Raspberry Pi firmware's own low-critical voltage alarm,
    a clean 0/1 with no threshold to choose. It is evaluated FIRST and returns ahead of every other
    arm when asserted, because undervoltage corrupts SD cards and cooling down does not undo that.
    `UNDERVOLTAGE_CONSECUTIVE` is **1**, no grace, unlike every other arm here: the firmware
    latched a bit, and it did not report a measurement that can spike. The load-bearing part is
    `UNDERVOLTAGE_UP_QUERY` (`up{job="node-pi"}`). The sensor is ONE series from ONE host, so a
    `max() > 0` arm reads green the moment daniel-pi stops answering, and a Pi falling off the
    network is what sustained undervoltage causes. An empty vector therefore defers only while
    that gate is not affirmatively up (`check_cluster_targets` owns a dead scrape, and an
    unqueryable gate defers too), and pages while the Pi IS scraping, because the sensor was
    renamed or the collector went blind.
  - **CPU thermal throttling** (`_thermal_throttle_arm`, `THERMAL_THROTTLE_QUERY` =
    `node_cooling_device_cur_state{type="Processor"}`). A non-zero `cur_state` means the kernel is
    derating the CPU now. It is a different fault from `check_cpu_throttle` (CFS throttling, a
    cgroup quota, not heat) and from the temperature arm, since the firmware can enforce a limit
    below the one the driver declares. `type="Processor"` is load-bearing: unfiltered, the metric
    also carries PCIe link-speed and `intel_powerclamp` devices, which throttle for reasons that
    are not heat. The arm has its own source gate, `THERMAL_THROTTLE_UP_QUERY` = `up{job="node"}`
    (`node` rather than `node-pi`, because the Pi publishes none of these series). A fully empty
    vector therefore means both amd64 exporters went quiet, which `check_cluster_targets` owns, and
    an empty vector while `node` IS scraping pages, since a driver or kernel change taking the
    sensors away would otherwise go unnoticed. `THERMAL_THROTTLE_ORIGINS_MIN` is **2**, not the
    temperature arm's 3, because daniel-pi publishes no Processor cooling device (daniel-box 16
    devices, daniel-server 8, daniel-pi 0 on 2026-09-10). That floor stops the arm being inert:
    without it, one node's collector going blind leaves the other answering "not throttling" for
    the whole estate. `THERMAL_THROTTLE_CONSECUTIVE` is 3 (15 min at `INTERVAL=300`): one cycle of
    throttling during a compile is ordinary, and sustained throttling is a cooling fault.

  A clean arm returns **None and says nothing**, so an ordinary cycle's tile text is
  byte-identical to the text from before the arms existed. An arm HOLDING inside its own grace does
  append its note, because a monitor that is up while a fault accumulates has to say so.
  `test_host_thermal_arms.py` tests both arms as accept/reject pairs, plus one structural test that
  reads `check_host_temp.__code__.co_names` to prove the check calls both arms and calls the
  undervoltage one first. That structural test proves the call sites exist and their order, and it
  cannot prove the check PROPAGATES an arm's verdict: drop a `return` and the name stays in
  `co_names`. **`verdicts/host_power.thermal_monitor_verdict` is where the propagation lives**
  (#1547): the check fetches and holds the streak state, the composer decides which of the four
  arms reaches Kuma, and every ordering and propagation rule has a direct test in
  `test_host_thermal_arms.py` with nothing patched. Deleting the undervoltage `return` turns
  `test_an_asserted_undervoltage_alarm_reaches_the_monitor` red.

  Transport, measured before shipping because the arms read Prometheus: both queries answered in
  0.48-0.58 ms, three runs each, against Prometheus's loopback on daniel-server (2026-09-10), the
  same shape as the five instant queries the temperature arm makes.)
- **UPS Battery Health** (mains loss, the APC UPS's charge %, estimated runtime and the
  replace-battery self-test verdict, all four read from **nut-exporter** and nothing else. #3105
  dropped the HA re-export of the same UPS that #1548 had kept as a `max(A) or max(B)` fallback.
  HA's NUT integration reads the SAME upsd over the `nut` ClusterIP, so the fallback never covered
  an upsd outage, only nut-exporter dying while upsd lived, which Prometheus reports as
  `up{job="nut"} == 0` and Scrape Targets already pages for. HA's `hass_*` UPS series stay in the
  `Infrastructure/ups-power-battery` Grafana board as a second view.
  `down` on sustained **mains loss** (`UPS_ON_BATTERY_QUERY`, the NUT `ups.status{flag="OB"}`,
  one-hot over `flag`, so the exporter forces a 0 when the UPS is not asserting it and the series
  is a real 0/1 alert input; judged FIRST and returning alone, because charge and runtime read the
  RUNWAY and hold green through most of an outage; its own streak key) or on a low battery RUNWAY:
  charge < `UPS_CHARGE_MIN_PCT` (50, a deep discharge while on battery) OR estimated runtime <
  `UPS_RUNTIME_MIN_S` (300 s: an aged battery whose full-charge runway has decayed, OR a discharge
  nearing shutdown) OR the UPS's own **replace-battery** verdict (`UPS_REPLACE_QUERY`, the NUT `RB`
  flag, the earliest signal, since it can trip while charge and runtime still read fine).
  One defer path avoids double-paging a source outage another monitor owns: ALL arms absent while
  the nut scrape is down defers to Scrape Targets' page. That covers a dead upsd as well as a dead
  exporter, because nut-exporter fails the WHOLE `/ups_metrics` scrape when upsd is unreachable,
  which is why its probes are `tcpSocket` (`roles/k8s/nut-exporter/CLAUDE.md`). A **partial**
  absence (one arm gone while the others report) is a specific series rename, so it pages through
  the streak rather than silently monitoring the survivor. The only other UPS alert is an HA
  automation → **mobile** push (a separate channel from this Kuma→Discord path), and nothing
  trended the battery, so a slowly degrading battery stayed invisible until an outage collapsed it.
  **Prom-dependent** (queries the `nut` scrape). `UPS_CONSECUTIVE` (2, like `HA_CONSECUTIVE`) rides
  out a one-cycle dip from a transient load spike, a restart blip that drops one arm, or a brownout
  shorter than the grace window. Queries are env-driven
  (`UPS_CHARGE_QUERY`/`UPS_RUNTIME_QUERY`/`UPS_REPLACE_QUERY`/`UPS_ON_BATTERY_QUERY`, all empty =
  disabled; `UPS_SOURCE_UP_QUERY` is the all-absent gate), so a series rename needs no code edit.
  Pure `ups_health()` and `ups_on_battery_verdict()` are unit-tested.)
- **Pi Pressure** (the Pi's own node-exporter series on the `node-pi` scrape job, five instant
  queries selected by `origin=PI_ORIGIN`. `down` when `node_load5` per core > `PI_LOAD_MAX`,
  `node_memory_MemAvailable_bytes` < `PI_MEM_MIN_MB`, any block device's `node_filesystem_*` usage
  > `PI_DISK_MAX_PCT`, or `node_filesystem_readonly` is 1 on `/` or `/boot/firmware`. The read-only
  arm (#2668) exists because an SD card that remounts its root read-only after an I/O error
  freezes the fill % at its last value, keeps its open sockets, and keeps syslog flowing through
  log2ram, so every other arm stays green through the classic Pi failure. It takes no grace, like
  `kubelet_plugin_readonly`, because a read-only remount does not self-heal. Filesystems are keyed
  by device rather than mountpoint because the SD card is mounted twice (`/` and `/var/hdd.log`)
  and one full card is one problem. tmpfs is excluded because log2ram's 128 MiB `/var/log` fills
  and flushes by design. A filling SD card is the slow Pi death the server-only Root Disk check
  cannot see, and the 512MB Zero 2 W dies by swap-thrash (fwupd episodes ran load5/core >1.7 with
  healthcheck-timeout storms no other monitor saw). The vfat `/boot/firmware` partition is covered
  for the reason Root Disk watches `/boot` on the nodes. The healthy message names the fullest
  device (`disk /dev/mmcblk0p1 37%`) because a bare percentage reads as the SD card.
  **The source is node-exporter, not glances** (#2004): glances held 66 MB of anonymous memory on a
  456 MB host for facts node-exporter already exports. The check is in `PROM_DEPENDENT` and in
  `EXPORTER_DEPENDENT["node-pi"]`, so a Prometheus outage or a dead Pi node-exporter suppresses it
  rather than paging a second time, and it is NOT in `STARTUP_GRACE`: the two sets must stay
  disjoint, and its source is not a reach-out the post-boot transient reaches. An absent series
  while Prometheus answers pages, because a Pi whose exporter stopped reporting is a Pi nothing is
  watching. Empty `PI_ORIGIN` disables the check (stays up).
  **This check owns Pi disk and memory.** `HOST_METRIC_ORIGIN_EXCLUDE` keeps daniel-pi out of the
  Memory/Root Disk queries (`host_metric_sel`), so they stay two-host checks and this one is the
  single source of truth for Pi pressure. Dropping the exclusion was rejected: `MEM_MAX_PCT` (90)
  on a 456 MB box fires at 45.6 MB available and `PI_MEM_MIN_MB` fires at 50 MB, so `check_mem`
  would be a strictly weaker duplicate that pages the estate-wide Memory tile for a Pi fact this
  tile already reports. The exclusion also keeps the disk/memory origin floor at 2 hosts rather
  than 3.
  **If you add a third node-exporter host, decide explicitly whether it belongs in the estate-wide
  Memory/Root Disk checks or in a check of its own.** A test forces that decision for ONE of the
  two ways a host arrives: `test_every_node_exporter_job_is_mapped_in_exporter_dependent`
  (test_check_gates_exporters.py) derives the node-exporter scrape jobs from the Prometheus config
  and fails until each has an `EXPORTER_DEPENDENT` entry, and its sibling fails if a job whose
  origins are all excluded by `HOST_METRIC_ORIGIN_EXCLUDE` suppresses `disk`/`memory` anyway. That
  covers a host under a **new scrape job**, which is how daniel-pi arrived. **A host joining the
  existing `node` DaemonSet is still on you.** The job set does not change, so nothing fires, and
  `HWMON_TEMP_ORIGINS_MIN` is a literal 3 justified by "all three hosts declare non-excluded
  sensors". A fourth host makes that floor satisfiable by any three of four, so one host can go
  dark silently. Raise the floor by hand when you add a node.
  **Published-port arm** (`with_pi_ports`, folded here for the push-token reason recorded at
  `with_ha_ban`). After a Pi restart a container can come back attached to no Docker network while
  still reporting `Up (healthy)`, because its healthcheck curls loopback inside its own netns. Its
  published port stops listening, and only a recreate restores it, because autoheal's restart loop
  re-enters the same empty sandbox. The arm TCP-connects from the bridge to each expected port on
  `PI_HOST` (the Pi's LAN address, rendered from `hostvars['daniel-pi'].server_ip`) and names every
  dead one. The message carries the recreate hint, and `ssh daniel-pi docker ps` tells the causes
  apart (detached, publishing but unreachable, or not up), since nothing the cluster can reach
  serves the container view. The arm rides this check's gates: a Prometheus outage or a dead Pi
  node-exporter skips the port probes with the pressure arms. Both already page, and the Kuma HTTP
  monitor on wg-easy still watches the one Pi port a person uses. `PI_PUBLISHED_PORTS` renders
  `name:port` pairs from daniel-pi's `containers_list` (every entry with a `port`), so
  `docker-proxy`, `autoheal` and `docker-proxy-lifecycle`, which publish nothing, fall out by
  construction rather than by an exclusion list. `udp_port` is excluded because there is no
  TCP-connect equivalent for UDP. `PI_PORTS_CONSECUTIVE` (2) rides out the seconds of closed ports
  a Pi deploy causes when it recreates a container.
  **This arm adds no reachability coverage. It adds a named port, and that is the whole case for
  it.** Every publisher was already watched (Kuma HTTP-monitors wg-easy, and `alloy` is a
  Prometheus scrape target, `job=alloy-pi`, that `check_targets_down` covers). What nothing said was
  *which* port went quiet, and the 2026-08-08 sweep across four monitors for that answer missed
  dozzle entirely. Do not re-justify this arm as filling a monitoring gap.)
- **Home Assistant Automations** (HA's REST API `/api/states/input_datetime.ha_heartbeat` over
  `apps`, Bearer `HA_TOKEN`. An HA `time_pattern:/1min` automation stamps that helper with
  `now()`, so its `last_changed` is fresh ONLY while HA's automation *scheduler* is executing.
  `down` once it is older than `HA_HEARTBEAT_MAX_AGE` (300 s): a wedged-but-running HA (HTTP
  `:8123` up, scheduler stuck) that the container healthcheck cannot see. **`HA_CONSECUTIVE` (2)
  adds consecutive-cycle hysteresis** (the idiom of `CPU_CONSECUTIVE`): a planned redeploy takes
  the API unreachable for ~120 s and then leaves the scheduler a beat behind, so one cycle reads
  unreachable OR stale. The first down cycle pushes `up` with a "down streak n/N" `msg`, the
  second pages, and one fresh read resets the streak. The check catches the unreachable-API error
  itself (not `run_once`), so it rides the SAME grace as staleness. A genuinely wedged or
  auth-broken HA stays bad across cycles and pages. Empty `HA_URL`/`HA_TOKEN` disables the check
  (stays up). Pure `ha_heartbeat_fresh()` and the streak wrapper are unit-tested.
  **A second arm watches HA's own `ip_ban`**: a `count_over_time` LogQL query for `Banned IP` lines
  over `HA_BAN_WINDOW` (1h), keyed on `container="home-assistant"` (see the no-`app`-label trap
  below), `down` on any hit. HA's ban middleware keys on the peer address, so an unauthenticated
  burst from inside the cluster bans an INFRASTRUCTURE IP. On 2026-08-23 five ad-hoc `curl` calls
  banned `10.42.0.1`, the node's pod-network gateway, and HA 403'd the kubelet probes arriving from
  it into a crash loop. The probes now exec curl to `127.0.0.1` and cannot be banned, which fixes
  the crash loop and makes a ban SILENT: HA keeps serving while whatever shares that source IP
  stays locked out. This arm is the visibility half. It is folded into this monitor because a new
  Kuma monitor needs a new push token in SOPS, and a ban is an HA fault. A ban wins the message and
  keeps the heartbeat's text after it. It **skips `down_streak`**, because a ban either happened in
  the window or did not, and a second cycle adds nothing.
  **It watches the ban EVENT, not the ban STATE.** `Banned IP` is logged once, at ban time, so the
  arm pages for `HA_BAN_WINDOW` and then SELF-CLEARS while the entry is still in
  `/config/ip_bans.yaml`. A ban older than the window, or one reloaded from that file by an HA
  restart (which logs nothing), is invisible. **A green `ha_heartbeat` means "no ban was issued in
  the last `HA_BAN_WINDOW`," not "no IP is banned."** That is the only signal available, because HA
  does not log its ongoing 403s to a banned peer and this pod cannot read HA's PVC. The durable
  artifact is the **Discord notification** Kuma fires on the down transition, not the monitor's
  colour. When one fires, read `/config/ip_bans.yaml` by hand rather than waiting for the monitor
  to clear.
  **`ha_heartbeat` is deliberately NOT in `LOKI_DEPENDENT`**: membership suppresses the WHOLE check
  during a Loki outage, which would blind the real heartbeat. The ban arm fails open on a Loki
  error and keeps the heartbeat's own verdict. Pure `ha_ban_verdict()` is unit-tested.)
- **k3s Speedtest** (speedtest-tracker's `/api/v1/results`, newest row only, Bearer
  `speedtest_api_token` from the mounted credentials Secret. Three arms in this order: status, then
  age, then the download floor. The order is load-bearing, because `download_bits` is null on a
  failed row and a floor comparison ahead of the status arm compares None.
  **The download floor is 100 Mbps, a percentile cut at about p5** (#2785). The speedtest role
  pins server 41671, so every scheduled run draws it. Over the 95 six-hourly samples Prometheus
  retained (28.4d): min 61.4, p2 75.0, p5 95.1, p10 146.0, median 868.8 Mbps, 5 samples under 100.
  Before the pin the results were bimodal (server 41671 had a median of 910 Mbps and a worst of
  119, six other servers a median of 12.8 and a best of 42.8), so 100 sat in an empty band. p5 of a
  ~870 Mbps link is the degradation reading this check exists for, and the floor is what makes the
  pin going bad visible (server 1775 was clean for 54 runs and then was not). **That is why the arm
  needs a RUN of results rather than one.**
  **The floor arm pages on `SPEEDTEST_FLOOR_CONSECUTIVE` (2) consecutive sub-floor results**,
  counted backwards from the newest, and holds `up` with a `1/2 sub-floor results` note below
  that. The history comes from the same fetch (the page asks for that many rows, newest first), so
  the arm never waits a second cycle. A result that failed or recorded no figure is not a
  sub-floor result and breaks the run. One slow result used to hold the tile red for the full 6h
  until the next test (three times in 14 days, 11.6h in total).
  The age arm (`SPEEDTEST_MAX_AGE_H`, 8h against a 6h schedule) notices the scheduler dying, which
  has no other symptom: the pod keeps serving its UI and passing both probes while writing no new
  rows.
  **There is no consecutive-CYCLE hysteresis on the verdict, only on the fetch.** The app produces
  a row every 6h and this loop runs every 5 min, so a consecutive-cycle streak would re-read one
  row up to 72 times, delaying the page and proving nothing. `SPEEDTEST_FLOOR_CONSECUTIVE` is not a
  counter-example, because it counts RESULTS, each a separate measurement. The fetch rides
  `SPEEDTEST_CONSECUTIVE` because an app restart under a deploy is a real transient, and
  `speedtest` is also in `STARTUP_GRACE`. This is the same split as `check_ha_heartbeat`.
  Reaching the app needs `monitor-bridge` in speedtest's `netpol_from` list in
  `ansible/inventory/host_vars/daniel-box.yml`, because the baseline admits `traefik`,
  `prometheus` and two cni0 /32s, none of which is this pod.
  **The verdict stays on the REST API although Prometheus scrapes the same app.** #3105 asked for
  the switch, and the answer is marked `# DECIDED:` at the fetch in `checks/host_edge.py`. Three
  facts decide it. The scrape carries no timestamp (`count by (__name__) ({job="speedtest"})`
  returned 28 names on 2026-10-01, none a created-at, age or timestamp series), so the age arm, the
  only arm whose failure mode nothing else sees, could only be inferred from when
  `speedtest_tracker_result_id` last changed over an 8h window. `SPEEDTEST_FLOOR_CONSECUTIVE`
  counts RESULTS, which one REST fetch hands back, where Prometheus samples a 6-hourly result
  every 5 min and the arm would have to group samples by result id. And the scrape depends on a
  manual UI toggle that `roles/k8s/speedtest/CLAUDE.md` records as impossible to set from config
  at the pinned build, on a `longhorn-nobackup` PVC, a human step this alert path should not
  acquire. The scrape stays what #996 added it for: history in Grafana.)
- **Loki Reachable** (a fixed `/loki/api/v1/labels` probe: the root-cause GATE for the
  Loki-querying checks, the peer of Prometheus Reachable. When Loki is unreachable the
  `LOKI_DEPENDENT` checks (`loki_ingestion`, `swallowed_verdicts`, `kuma_notify_failures`) are
  **suppressed** (pushed `up` with a "skipped — Loki unreachable" `msg`) and only THIS monitor
  pages. One Loki outage would otherwise fire every dependent at once. Loki being UP but the log
  shipper (Alloy) not shipping is a different signal that Loki Log Ingestion surfaces. A test
  guards `LOKI_DEPENDENT` against the live `CHECKS`.)
- **WAN Reachable** (two provider URLs fetched by hostname, tried in order: the root-cause GATE for
  the internet-reaching checks, and the peer of Prometheus/Loki/B2 Reachable. An internet outage
  had no gate until a single WAN outage on 2026-09-18 turned 11 tiles red inside 90 minutes,
  including four host crons and `b2_reachable`, `r2_usage`, `discord`, `kuma_notify_failures`,
  `cloudflare-ip-drift`, `swallowed_verdicts` and `gitops_alive`. It gates `WAN_DEPENDENT`:
  `r2_usage`, `cloudflare_ips_drift`, `healthchecks_drift`, `discord`.
  **DOWN only when NEITHER endpoint answers.** One provider's outage is that provider's problem,
  and suppressing on it would turn Cloudflare's own tiles green during a Cloudflare outage. The
  endpoints are checked in order and the first answer stops the probe, so a healthy cycle costs
  one request.
  **By hostname, never an `anycast` IP.** An IP-only probe stays green through a DNS-only outage
  while every dependent fails, the storm the gate exists to suppress. Measured from daniel-server
  on 2026-09-27, three runs each: `cloudflare.com/cdn-cgi/trace` 0.095-0.100 s total and
  `www.google.com/generate_204` 0.074-0.122 s, both with 0.002-0.052 s of DNS.
  `b2_reachable` is the tile's PEER rather than a member: it is itself a gate, and
  `GATE_DEPENDENTS`' values are check names `run_once` iterates, not gates it evaluates. Two tiles
  for a WAN outage is the accepted cost of not building gate-of-a-gate for one caller.
  `kuma_notify_failures` and `swallowed_verdicts` are not members either: both read in-cluster
  Loki, so the Loki gate owns their source, and what failed for them was Kuma's own outbound send,
  a real fault the tile should report.
  **The four HOST crons are covered by a separate half** (#2793). `crowdsec-home-allowlist`,
  `github-ruleset-drift`, `release-staleness-check` and `docs-refresh` run on the host and push
  their own tiles, so this gate structurally cannot suppress them. Each consults `wan_reachable`
  in `ansible/roles/setup/initial_setup/files/kuma-push-lib.sh` on the failure path that reached
  the internet, and reports `skipped: WAN unreachable` as an `up` when neither provider answers.
  The probe classifies, not the error text: parsing curl exit codes and git stderr across four
  heterogeneous crons is the "green and inert" shape that library's header records paying for
  twice, and a GitHub 403 while the link is up makes the probe succeed, so that tile still pages.
  The endpoint list is `ansible/inventory/group_vars/all.yml:wan_probe_urls`, pinned equal to this
  gate's `WAN_PROBE_DEFAULT` by a test, because two halves disagreeing about what "the internet"
  means would leave a single-endpoint outage reported by nothing. The one wrong case is an
  HTTP-level failure coinciding with a WAN outage, reported as a skip; both tiles would have been
  red for one root cause anyway.
  `r2_usage` and `healthchecks_drift` left `STARTUP_GRACE` to join this set, as `pi_pressure` did
  when it gained the Prometheus gate (#2004): the two sets must stay disjoint so a graced check
  reaches the evaluation path every cycle, and the gate covers the post-boot transient the grace
  covered plus the outage it never could. Empty `WAN_PROBE_URLS` disables the gate.)
- **k3s Workload Health** (`kube_deployment_status_replicas_unavailable` from kube-state-metrics.
  It is the only monitor the seven routeless k8s workloads have, and the reason the metric is read
  at all: `registry`, both `cloudflare-ddns` copies, karakeep's `chrome`/`meilisearch`/`time-tagger`,
  and `n8n-runners`, which executes every workflow's code. Three expose only a ClusterIP
  (unreachable from daniel-server) and four expose no Service at all, so their health is a
  Kubernetes API property and nothing here can probe them directly.
  **Fails closed on an absent series.** `unavailable > 0` returns an empty vector both when every
  workload is healthy AND when there are no series at all, so the check `count()`s the series
  FIRST and reports `UNKNOWN, not OK` when the count is missing or below `K8S_MIN_WORKLOADS` (5).
  Reading the healthy meaning onto both is how a monitor goes green while blind. The floor also
  covers a partially loaded kube-state-metrics: its ClusterRole is deliberately scoped, so dropping
  `apps` would take every deployment series away while the pod stays up and Ready, which the
  reachability gate cannot see.
  **`K8S_WORKLOADS_CONSECUTIVE` (3) gates the unavailable-replica arm ALONE** (#1780). A rolling
  Deployment has one unavailable replica by definition, so that arm reported every ordinary
  rollout: of 48 DOWN episodes in 30 days, the replica ones were all single-cycle and named one
  workload (`unavailable replicas: uptime-kuma(1)`). A held cycle appends a `down streak n/N
  (rollout)` note to the tile rather than reading plain green. **Every other arm keeps no grace**:
  a crash loop is already a multi-cycle condition by the time `increase()` sees it, and a floor
  breach means the check is blind. `test_check_k8s_workload_replicas.py` proves the selectivity by
  running a crash loop and a rolling replica in ONE cycle, because a blanket streak would pass a
  naive accept/reject pair while delaying every crash-loop page.
  **A stalled rollout is its own arm** (#1783): `kube_deployment_status_replicas_updated <
  on(namespace, deployment) kube_deployment_spec_replicas`, gated by
  `K8S_ROLLOUT_STALL_CONSECUTIVE` (3). The unavailable arm counts the replicas a Deployment HAS,
  while this arm counts the replicas carrying the CURRENT spec. A ReplicaSet that never gets a pod
  while the old one keeps serving moves the second and not the first, so every other arm reads `N
  k8s workloads healthy` while the cluster runs the previous spec. It is the complement of the
  2026-09-10 Authelia stall (`updated=1, available=0, unavailable=1` for 11 minutes, the
  unavailable arm's shape). The gate came from the series: over 16.9 days of retention, `updated <
  spec` lasted 1-2 five-minute samples at a time and NEVER three consecutive ones, so 3 cycles had
  no false page, and 3 cycles is longer than the 300 s the playbook itself waits. The desired count
  comes from a SECOND query, because a PromQL `<` returns the left-hand series alone:
  `authelia(0/1)` reads as "zero of one replicas carry the new spec," where a bare `authelia(0)`
  would read as a different fault. The stall helpers and BOTH replica streak gates live in
  `checks/cluster_rollout.py`, because `checks/cluster.py` is at the 600-line module cap.
  **A Deployment at ZERO available replicas is its own arm** (#1802):
  `kube_deployment_status_replicas_available == 0 and on(namespace, deployment)
  kube_deployment_spec_replicas > 0`, gated by `K8S_ZERO_AVAILABLE_CONSECUTIVE` (2), in
  `checks/cluster_zero.py`. Zero available is down, not rolling, and the unavailable arm's
  15-minute grace held the two alike. The census that set 2 rather than 1: over 16 days of
  retention, every zero-available episode of two or more 5-minute samples was a crash loop the
  restart arm had paged on its first cycle, a 20-minute cold start after a node event (paged by
  the 3-cycle arm), or the 2026-09-05 outage. A routine Recreate swap is one sample, and at 1
  cycle this arm would page each of them. The streak is separate from the unavailable arm's, so a
  Deployment at zero is held with two notes (`cold start` and `rollout`), pages here on its second
  cycle, and is named again by the unavailable arm on its third. The `desired` count comes from the
  stall arm's second query, so the message reads `authelia(0/1)`.
  **A second arm covers DaemonSets**: `kube_daemonset_status_number_unavailable`, with its own
  `K8S_MIN_DAEMONSETS` floor (11) and the same fail-closed-on-absent-series logic. A
  Deployment-shaped census cannot see the Alloy log shipper, node-exporter or the `otel`
  collector, which run one pod per node and are the workloads a node problem takes out first.
  **A third arm reports crash-looping restarts**: `increase(...[K8S_RESTART_WINDOW]) >
  K8S_RESTART_MAX` (1h / 3), **and** a restart inside `K8S_RESTART_RECENT_WINDOW` (30m). The
  recency clause lets a RECOVERED pod leave the arm: `increase` is a pure lookback, so without it
  the restarts that already happened hold the monitor DOWN for the rest of the hour (zigbee2mqtt
  read `restarts in window: 9` after it recovered). The 30m floor is the worst observed
  inter-restart SPACING, not the 5-min backoff cap: homepage spaced 31 restarts ~15-19 min apart,
  and a window inside that spacing goes up in the gaps and flaps, which at `max_retries: 0` is a
  notification per transition.
  **A fourth arm watches extended resources** (PR #281): every name in `K8S_EXTENDED_RESOURCES`
  (comma-separated, default `devic.es/dri`) must still be advertised at non-zero quantity by at
  least one node, read from `kube_node_status_allocatable`. This is the blast radius of a wedged
  device plugin, not the plugin's own liveness. `dri-device-plugin` has no `readinessProbe`, and a
  container without one is Ready the instant it starts, so a plugin whose gRPC registration hangs
  keeps a Running, Ready, fully available DaemonSet while kubelet deregisters `devic.es/dri`,
  invisible to the DaemonSet arm. **A socket-stat probe is worse than nothing** (rationale in
  commit `1b2aa497`): the registration socket file persists through the wedge, so the probe reads
  green through the fault, and kubelet clears that directory on restart, so the same probe
  restart-loops a healthy plugin. `ksm_resource_label()` sanitizes the configured Kubernetes name
  into the label kube-state-metrics emits (`devic.es/dri` becomes `devic_es_dri`), because
  querying the unsanitised name matches no series and the arm would read it as a deregistered
  resource (the 2026-08-20 false page in **Traps** below). The arm needs kube-state-metrics'
  `nodes` collector: with no `kube_node_status_allocatable` series at all it reports **INERT** and
  names what it is not watching. It is folded into this monitor because a new Kuma monitor needs a
  new push token in SOPS and this arm answers the DaemonSet arm's question. A resource fault wins
  the message and keeps the workload arm's text after it. Pure `k8s_workloads_verdict()` and
  `extended_resource_verdict()` are unit-tested, and `PROM_DEPENDENT` is guarded against the live
  `CHECKS` and asserted disjoint from the other skip sets.)
- **Cluster Scrape Targets** (`up{origin!="daniel-server"}`, the complement of Scrape Targets'
  `origin="daniel-server"` pin, so every `up` series belongs to exactly one of the two. Same
  fail-closed floor (`CLUSTER_TARGETS_MIN`, 3): an emptied `up` reads as UNKNOWN rather than as
  nothing being wrong. `CLUSTER_TARGETS_CONSECUTIVE` (3) is the only hysteresis. A rolling
  workload drops its own `up` series for a scrape or two, and this check cannot tell that from a
  dead exporter, so every rollout opened a DOWN episode (66 in 30 days, the highest count in the
  estate, mostly one cycle naming the target that was rolling). 3 cycles = 15 min at
  `INTERVAL=300`, the value `LONGHORN_CONSECUTIVE` / `PVC_CLAIMS_CONSECUTIVE` /
  `SNAPSHOT_CAP_CONSECUTIVE` also carry: far longer than a rollout's scrape gap, far shorter than
  a dead exporter. The streak delays a page and does not suppress it. The floor arm rides the same
  streak, because an emptied `up` during a Prometheus roll is the same transient.)
- **Longhorn Volume Redundancy** (`files/checks/storage.py:check_longhorn_volumes`. Reads the
  one-hot `longhorn_volume_robustness` metric. `k3s_longhorn_replica_count` is 2, so a `degraded`
  volume is down to one copy and a `faulted` one has no healthy replica. The decision is
  `longhorn_robustness.SAFE_ROBUSTNESS` (`healthy`, `unknown`): any other state pages, so a state
  Longhorn adds later pages instead of passing a `degraded|faulted` selector. The runbook gates in
  `scripts/deploy_tools/runbook_gates_lib/gate_runner.py` call the same allow-list on the volume CRs (#3668).
  `unknown` is a detached volume and is not a fault (6 of 43 volumes read it on 2026-08-17,
  including game servers scaled to zero on purpose). The two longhorn-manager pods report disjoint
  volume subsets, so offenders are deduped by name. **An absent metric is a breach**, not health:
  `count(longhorn_volume_robustness{state="healthy"})` doubles as the input assertion that the
  longhorn scrape job is answering, and it reads "replica redundancy is UNMONITORED." A scrape gap
  and a degraded volume get different hysteresis labels, and both wait `LONGHORN_CONSECUTIVE` (3)
  cycles so a node drain or the Sunday restart does not page. **In `PROM_DEPENDENT`**, because its
  absent-metric branch pages when the longhorn job dies. Runbook: `longhorn-disaster-recovery`.)
- **k3s PVC Fullness** (`kubelet_volume_stats_available_bytes / _capacity_bytes` via the cluster
  Prometheus: the SPACE axis of the storage layer, where Longhorn Volume Redundancy is the replica
  axis. A Longhorn PVC is its own filesystem at a fixed capacity, so a 2 Gi claim can fill to 100%
  while Root Disk reports 620 GB free. `PVC_MAX_PCT` is **85**, not Root Disk's 90: a full PVC
  cannot be relieved by deleting something elsewhere, because the operator has to expand the
  volume, and on the smallest claim (973 MiB) 85% leaves 146 MiB of headroom against 97 MiB at 90%.
  **A percentage cannot warn before a step.** valheim-server sat at 79% for days and reached 100%
  inside one 15-minute updater cycle when a Steam update staged a fourth copy of the install
  (#1866). `PVC_MIN_FREE` (#1875) therefore declares a per-claim free-bytes floor,
  `<pvc>=<bytes>` (the SNAPSHOT_CAPS shape), and the check pages while a named claim's
  `kubelet_volume_stats_available_bytes` is below it, with no streak, whatever its percentage
  reads. The value is the claim's largest transient, declared by its own role
  (`valheim_k8s_server_update_transient_bytes`, 3 GiB against a 2.2 G copy) and pinned to this one
  by `tests/test_check_pvc_floors.py`. A rate signal was rejected: the updater's cycle is 15 min
  and the check runs every 300 s, so a stable window fires after the ENOSPC as often as before. A
  named claim reporting no free bytes is a breach, and the green summary names every floor held,
  so an arm that stopped evaluating shows as an absence.
  `PVC_EXCLUDE` drops `media-data`, the one claim backed by a `local` PV at `/srv/media` rather
  than by Longhorn: it IS the `/` filesystem Root Disk already watches, so scanning it here pages
  twice for one full disk. The query aggregates `max by (namespace, persistentvolumeclaim)`,
  because daniel-box's claims are scraped TWICE (k3s serves the kubelet registry on the
  supervisor's `/metrics` too, so the same series arrives under `job="kubernetes-kubelet"` and
  `job="kubernetes-apiserver"`, 43 + 27 series over 43 claims), and `sum` would report a
  double-scraped claim at twice its real fullness.
  **Fails closed on a thin claim census**, and `PVC_MIN_CLAIMS` (32) is DERIVED: the kubelet job
  alone reports all 43 claims and the apiserver job alone 27, so losing the apiserver job costs no
  coverage and the only hazard is a dead kubelet job, which leaves 27 claims answering, every one
  under the limit, while daniel-server's go dark. Any floor at or under 27 reads that as healthy.
  For the same reason the check has **no `EXPORTER_DEPENDENT` entry** keyed on the kubelet job:
  that would suppress the page on exactly the partial outage the floor exists to catch, the
  `node`-only mistake that blinded Host Temperature on two hosts of three. A fullness breach gets
  no grace, because it is monotonic, and the census arm rides `PVC_CLAIMS_CONSECUTIVE`.)
- **Longhorn Snapshot Headroom** (`longhorn_snapshot_actual_size_bytes` joined to
  `longhorn_volume_capacity_bytes` for the claim name, #1627: the SNAPSHOT axis, where PVC Fullness
  is the filesystem axis and Longhorn Volume Redundancy the replica axis. Snapshots live in the
  Longhorn backend, so a volume can fill its `spec.snapshotMaxSize` while its claim reads 12% full
  and every replica reads healthy. A cap that is REACHED does not prune, it refuses: Longhorn stops
  accepting new snapshots, and `k8s/volume-snapshot` snapshots before it prunes, so the first
  deploy past the cap fails and every later one fails identically until snapshots are deleted by
  hand (#1560). That role's own gate fires only during a deploy of the capped service. This arm
  watches a volume filling BETWEEN deploys, which a recurring Longhorn backup job does with nobody
  deploying.
  **`SNAPSHOT_CAPS` declares the caps**, `<pvc>=<bytes>`, because nothing exports the field:
  Longhorn's exporter publishes snapshot sizes and no cap, this pod runs with
  `automountServiceAccountToken: false`, and the Longhorn HTTP API answers only from the
  node-local manager. Ansible is the only writer of a cap. `roles/k8s/jellyfin/tasks/main.yml`
  patches the only one (jellyfin-config, 16 GiB = 2 x the PVC size, the smallest Longhorn
  accepts), and `tests/test_check_snapshot_headroom.py` derives the capped set from the tree, so a
  second capped volume missing from the declaration fails CI. `"0"` is Longhorn's UNCAPPED value
  and the fleet default, so the check drops it instead of reading a cap of zero, which would
  report every volume full.
  **Usage is a superset of what the deploy gate sums.** The gate skips `status.markRemoved`
  snapshots and the metric carries no such label (25 of 114 Snapshot CRs were `markRemoved` and
  every one still had a series on 2026-09-10). That errs safely, because those blocks are still
  held until Longhorn purges them, but it can overstate usage for a cycle after a prune, so a
  breach rides `SNAPSHOT_CAP_CONSECUTIVE`. The sum is deduped by (volume, snapshot), like PVC
  Fullness's `max by`, because both longhorn-manager pods are scraped independently. A declared
  cap whose volume has NO series is a breach, not green.
  `monitor_bridge_snapshot_headroom_push_token` is in SOPS, and both halves read it unguarded: the
  env-secret's `lookup('vars')` and the Kuma tile loop in
  `k8s/uptime-kuma/templates/static-monitors.yaml.j2`, both over its `check_table.py` row. The
  check first shipped inert with the token absent, the shape #1632 names: a gated monitor with an
  unset variable reads green while watching nothing.)
- **Kubelet CSI Mount Read-Only** (`node_filesystem_readonly{mountpoint=~"/var/lib/kubelet/
  plugins/.*"} == 1`, #1243. A reclaim stall dropped Longhorn's iSCSI sessions, `replacement_timeout`
  expiry aborted several ext4 journals and remounted them read-only, and every other monitor stayed
  silent for 50 minutes because Volume CRs read `attached healthy` throughout. Longhorn's own state
  is structurally blind to a filesystem-level fault under it. node-exporter's
  `mount-points-exclude` hid the metric under a wholesale `var/lib/kubelet` exclusion, so the
  exclusion is `var/lib/kubelet/pods` (`daemonset.yaml.j2`). The per-pod bind mounts stay excluded
  (unbounded cardinality), and the CSI global mounts under `plugins/` do not (~40 volumes x ~7
  series, ~300 total).
  **No grace**, unlike Longhorn Volume Redundancy: a read-only remount does not self-heal the way
  a replica rebuild or a kubelet restart does, so a streak would only delay a real page.
  **`host_metric_sel`, not `origin_sel`**, for the reason `check_disk` and `check_mem` use it:
  `PROM_ORIGIN` resolves to `origin="daniel-server"` in the deployed env, and pinning would hide
  the identical fault on daniel-box behind a green tile. An absent series reads as healthy (no CSI
  global mount is read-only) rather than as blind, because node-exporter being entirely down is
  `check_targets_down`'s and `check_disk`'s job. The `node-exporter-scrapes-csi-global-mounts` row
  of `ansible/tests/k8s/_config_property_rows.py` guards the exclusion regex directly, since this
  check's empty-is-healthy logic cannot tell a real all-clear from a re-widened exclusion hiding
  the fault again. **In `PROM_DEPENDENT`**: `prom_vector` raises on an unreachable Prometheus,
  which `_evaluate` turns into a `down`, so without the gate a Prometheus restart pages this
  monitor a second time for one root cause.)
- **etcd DB Size** (`max(apiserver_storage_size_bytes)` against `ETCD_DB_QUOTA_BYTES`, down at
  `ETCD_DB_MAX_PCT` = 80%, `files/checks/cluster_etcd.py`, #2403. etcd goes READ-ONLY at its
  backend quota and the control plane stops accepting writes, the failure the restore runbook
  exists for. **Why the proxy and not etcd's own series:** `k3s_etcd_expose_metrics` is off, so
  `etcd_mvcc_db_total_size_in_bytes`, `etcd_server_leader_changes_seen_total` and
  `etcd_disk_wal_fsync_duration_seconds_*` return no data. The `DECIDED:` marker at that switch in
  `group_vars/all.yml` records why it stays off: arming it restarts k3s and re-encrypts every
  Secret (#2294) from a manual-plane playbook, and opens an unauthenticated `:2381`.
  `apiserver_storage_size_bytes` is scraped unconditionally and matches the etcd snapshot's size
  (#2403: 53,768,192 on 2026-09-24, 2.6% of the quota).
  **`max(...)` over the bare series, no `by` and no `job` selector.** k3s serves the apiserver and
  the kubelet from one process, so the series is scraped TWICE with the same value, under
  `job="kubernetes-apiserver"` and `job="kubernetes-kubelet"`. Two identical series would make
  `prom_scalar`'s `result[0]` an arbitrary pick, and selecting on `job` would tie the check to a
  scrape-job name rather than to the metric. **2 GiB is etcd's OWN default** and is what is in
  force: `k3s_server_args` carries no `--etcd-arg=quota-backend-bytes`, so adding one there moves
  `ETCD_DB_QUOTA_BYTES` with it or the check measures against a quota the cluster does not have.
  **No grace**, for the reason CSI Read-only Mounts has none: a DB near its quota does not shrink
  on its own. An absent series reads as healthy and points at Scrape Targets. PVC Fullness fails
  closed because 27 of 43 claims survive a dead kubelet job, but one series carried by two jobs
  has no partial-coverage case, so empty means total scrape loss, which `targets` and
  `cluster_targets` already page on. **In `PROM_DEPENDENT`**, because it reads Prometheus.)
- **etcd Restore Drill** (`files/checks/service.py:check_etcd_restore_drill`. Reads the
  `last-success-list-only` stamp that the weekly etcd restore drill cron on daniel-box writes
  under `ETCD_DRILL_STATE_DIR` (`/etcd-drill-state`, a hostPath), and checks its `epoch=` line
  against `ETCD_DRILL_MAX_AGE_DAYS` (8 days). It never reads `last-success-full`, because only the
  list-only leg is scheduled: accepting either file would report the object-graph restore as
  proven when nothing here has proven it. The check fails closed, and each way the stamp can be
  missing has its own message. **Absent** reads "no etcd restore drill has ever passed."
  **Unreadable** reads "unreadable by this uid (needs 0644)," because the stamp was first written
  0640 root:root while this pod runs as uid 1000. A stamp whose `epoch` does not parse reads "no
  readable epoch." An **old** stamp reads "last passed N days ago (weekly cadence)." etcd carries
  the Longhorn `Backup` CRs that locate the volume backups, so a restore nobody can prove puts the
  rest of the recovery chain at risk. No gate and no streak: a file read has no upstream to wait
  on, and a stale stamp does not heal by itself. Runbook: `k3s-etcd-restore`.)
- **Loki Log Ingestion** (three-arm LogQL freshness against the cluster `loki-homelab` through its
  in-cluster Service. `down` if ANY arm is silent: a dead Alloy→Loki pipeline (a relabel
  regression, positions-file corruption, a stale `/var/log` mount) that Loki's `/ready` Kuma probe
  stays green through. `templates/env-secret.yaml.j2` renders every selector from the
  `loki_streams` label owner in `group_vars/all.yml` (#3740).
  **Arm 1, cluster file-tail union** `sum(count_over_time({job=~"authlog|syslog", machine!="daniel-pi"}[3h]))`
  (`LOKI_STREAM`). It counts the cluster hosts' file-tailed streams together, so if Alloy's file
  sources die they fall silent together while syslog's routine volume keeps a quiet night alive.
  The window is tolerant because file-tail volume is low and dips overnight (a lone
  `{job="syslog"}` over 10m false-paged on a 15m35s idle gap). The arm EXCLUDES the pod streams,
  because they dwarf the file-tail streams and a healthy pod stream would mask a total file-tail
  outage. It also EXCLUDES `machine="daniel-pi"`: the Pi's health crons ship under the same
  `job="syslog"` (~72 lines per 3h), so without the filter a total cluster file-tail outage never
  reaches zero (#3739).
  **Arm 2, cluster pod streams** `sum(count_over_time({job="k8s"}[30m]))` (`LOKI_DOCKER_STREAM`):
  every pod's stdout, the stream arm 1 excludes. A pod-source break silences every container log
  while the file-tail streams keep flowing, and the tight window catches a total Alloy death fast.
  The selector is `{job="k8s"}` and not `{container=~".+"}`, which also matched the Pi's container
  streams (~1,940 lines per 30m), so the Pi alone kept the arm non-zero (#3739).
  **Arm 3, daniel-pi** `sum(count_over_time({job="pi"}[3h]))` (`LOKI_PI_STREAM`): arms 1 and 2
  count cluster streams only, so the Pi's own Alloy could die with both arms green. It uses the
  tolerant window (`LOKI_FILETAIL_WINDOW`, same as arm 1) because the Pi is a Zero 2 W running
  five LAN-only containers, so its log volume is low and bursty. Selectors and windows are tunable
  via `LOKI_STREAM`/`LOKI_FILETAIL_WINDOW`/`LOKI_DOCKER_STREAM`/`LOKI_WINDOW`/`LOKI_PI_STREAM`.
  `test_cluster_arm_counts_no_pi_stream` holds arms 1 and 2 off every Pi stream. Pure
  `loki_ingestion_fresh()` and `loki_count()` are unit-tested.)
- **Log Shipper Dropped Entries** (three arms, #993. Two log-pipe arms, and `down` fires on
  whichever counted MORE. **Arm 1, client-side:**
  `sum(increase({__name__=~"loki_write_dropped_entries_total"}[1h]))` from Prometheus, which
  scrapes the cluster Alloy DaemonSet (`job=alloy`, port 12345) and daniel-pi's Alloy container
  (`job=alloy-pi`), across ALL drop reasons (`ingester_error`, `rate_limited`, `stream_limited`,
  `line_too_long`; review M2). It sees only what a shipper itself gave up on. **Arm 2,
  server-side:** `sum by (reason) (increase({__name__=~"loki_discarded_samples_total"}[1h]))`
  read from Loki's own distributor (`job=loki-homelab`): entries Loki rejected that no shipper
  attributed to itself. The client-side arm alone understated the server-side total by ~150x
  (2026-09-03: ~161k samples discarded server-side under `reason="too_far_behind"` in 24h, against
  1,027 client-side, all `job=alloy-pi` under `reason="ingester_error"`).
  `down` when either arm's total exceeds `SHIPPER_DROPPED_MAX` (3000) over the window. The message
  names which reason fired when the server-side arm dominates: `too_far_behind` is a
  clock/backfill problem, and every other reason is throughput or limits. 1000 held a 1020-entry
  hour red for 51 minutes, and the client counter is non-zero in 26 of ~336 hours with NOTHING
  between 1020 and 6261; the derivation and the rejected streak are at the `DECIDED:` marker in
  `bridge/config_io.py`. Where **Loki Log Ingestion** catches TOTAL silence, this surfaces PARTIAL
  loss, shipper-side or Loki-side. `increase()` handles counter resets, and no series on either
  arm gives 0 and `up` (a dead shipper scrape is Scrape Targets' page). **Prom-dependent**, so the
  Prometheus gate suppresses it. Pure `shipper_dropped()` is unit-tested.
  `SHIPPER_DROPPED_WINDOW`/`SHIPPER_DROPPED_MAX` tune both arms, and
  `SHIPPER_DROPPED_METRICS`/`SHIPPER_DROPPED_SERVER_METRIC` pick the counters. Both are queried by
  `__name__` regex, so a counter rename cannot read as "0 dropped forever."
  **Reading a `too_far_behind` page.** A shipper's FIRST start re-tails every current file from
  offset 0, and Loki rejects the already-ingested history as "too far behind" (193,348 at the
  2026-09-02 cluster Alloy cutover, nothing lost, counted client-side under
  `reason="ingester_error"`). The client-side counter appears to fold `too_far_behind` into
  `ingester_error`, since that label only surfaces as `too_far_behind` on the server-side arm.
  That mapping is INFERRED from the observed labels and not read from Alloy's source. A shipper
  restart in the window is consistent with a benign re-tail, not confirmation of one, so
  correlate `time() - process_start_time_seconds{job=~"alloy.*"}` against the alert window before
  treating a page as data loss. A benign re-tail must still page, because it means "logs land in
  Loki promptly" briefly broke. Land a shipper change more than an hour before pointing this
  check at its counters, so the cutover's own re-tail does not page the deploy.
  **The weekly reboot is the one `too_far_behind` exemption** (#2783). Alloy ships its backlog for
  hours afterwards and Loki discards the late part, which held this tile red for 4.4h on
  2026-09-27 against a 1h rolling window, so the discards ran ~3.4h past boot. Inside
  `SHIPPER_BACKLOG_GRACE_S` (21600 s, ~1.75x that) of the NODE's boot (`bridge/common.py:host_uptime_s`,
  never this pod's age, because a deploy restarts the bridge without rebooting anything) the
  `too_far_behind` reason alone is dropped from the server-side total and named in the `up`
  message. Every other reason and the whole client-side arm stay live, so a throughput fault on a
  reboot morning still pages. Suppressing the check outright would be six hours of weekly
  blindness on partial log loss.
  **The reboot also loses client-side entries, and the lookback bounds that loss instead of a
  grace** (#3490). While Loki is down for the restart, daniel-pi's Alloy keeps shipping and drops
  what Loki refuses (179,396 `reason="ingester_error"` entries under `job="alloy-pi"` on
  2026-10-04, from a node that did not reboot). The Pi pushes through Traefik on daniel-box, the
  node this pod runs on, so the loss cannot outlast that node's boot. Both shipper queries
  therefore follow `check_swallowed_verdicts`: inside `BOOT_SETTLE_S` (1200 s) of the node's boot
  the shipper arms are skipped with an `up` message, and after it the range is the time since the
  settle window ended, growing back to `SHIPPER_DROPPED_WINDOW`. A drop after the settle window is
  inside that range and still pages. Raising the maintenance window's recovery allowance to ~70
  minutes was rejected: a window over 60 minutes covers every minute of the hour, and the status
  page sync (`20 * * * *`) would have to skip hour 8.
  **Arm 3, the collector's export failures** (#3094), folded in from the `observability` role's
  `telemetry-health.sh` host cron: `sum(increase({__name__=~"otelcol_exporter_send_failed_.*"}[15m]))`,
  `down` above `OTELCOL_SEND_FAILED_MAX` (0). The collector exports Claude Code's logs, metrics and
  traces to Loki, Prometheus and Tempo. A failed export writes nothing to Loki or Tempo while the
  collector's receiver counters keep climbing, so a stack dropping 100% of its data reads like a
  healthy one from every other angle. The arm has its own window and threshold because there is no
  ordinary rate of giving up. The selector is a `__name__` regex because the family is one counter
  per signal (`_log_records`, `_metric_points`, `_spans`), and naming one would read the other two
  as 0 forever. The send-failed family has NO live series until an export fails (measured on
  daniel-box 2026-10-01), and its `sent` twins (`otelcol_exporter_sent_log_records`,
  `_sent_metric_points`, `_sent_spans`, none `_total`-suffixed) are live and pin the naming. An
  absent family returns no series, which `prom_scalar` reads as None and the verdict counts as 0,
  so the arm is clean until the first failure. Pure `otelcol_export_failures()` is unit-tested, and
  an empty `OTELCOL_SEND_FAILED_METRICS` disables the arm. The OTLP hostPort probe did NOT fold:
  that door is a CNI portmap DNAT on the node's 127.0.0.1, so no in-cluster pod can reach it, and
  `telemetry-health.sh` survives as that one TCP connect. The `DECIDED:` marker at the top of the
  template carries the rejected alternatives.)
- **Swallowed Push Verdicts** (a host cron's DOWN verdict that `kuma-push-lib.sh` logged and then
  lost, #1869. The library returns 0 after a failed push by design, so the cron does not fail, and
  the verdict reaches nobody until the tile's heartbeat deadline (a day and an hour later for the
  daily drift producers). `check_swallowed_verdicts` reads the crons' push-outcome lines out of
  Loki over `SWALLOWED_VERDICTS_WINDOW_S` (3h): the cron's own `status=<up|down>` line and the
  library's final `push failed (` line, with the transient-retry line excluded in the LogQL. It
  uses `src.loki_lines`, a range query, because the newest line per tag decides and no metric
  query returns a line's timestamp.
  `down` when some tag's newest line is a swallowed `status=down` AND some other tag landed a push
  in the window. When nothing landed, the loss is fleet-wide (Kuma unreachable, a total-404 edge,
  the host that runs Kuma down), and the message names the edge/host tiles as the owner instead of
  paging a second time for one cause. That gate separates this check from the plain count of push
  failures that `uptime-kuma/CLAUDE.md` measured and rejected. A swallowed `up` is not counted:
  its tile goes red at the deadline for a cron that ran, which is the library's retry case
  (#1010).
  **It never reads back past the node's boot** (#2783). uptime-kuma's Service has no endpoint for
  ~16 minutes of the weekly restart, so every host cron whose slot falls in there loses its push
  for one known cause, and a daily producer's lost verdict stays the newest line for its tag until
  the next day. Inside `BOOT_SETTLE_S` (1200 s) the cycle is skipped and says so. After it, the
  lookback is the smaller of the configured window and the time since the settle window ended, so
  the full window returns on its own and the first verdict lost for any other reason pages.
  `BOOT_SETTLE_S=0` restores the plain window.
  One push is counted whatever its status and company: one Kuma REJECTED, marked `by=kuma` in the
  http/rc pair, which the library appends when the 404 came back as Kuma's own
  `application/json` rather than Traefik's `text/plain` page (#1803). That is a token no live
  monitor holds. The edge and Kuma are both up so no other tile pages, and no tile reaches a
  deadline, so the verdicts are lost for as long as the cron and the static monitors carry
  different tokens. A burst of `http=404` lines from Traefik rejecting every router belongs to
  Traefik 404 Flood (#1322).
  In `LOKI_DEPENDENT`. A fetch that hits the 5000-line cap says so in the message. Two pushers
  bypass the library's shape and are read by a second selector (#1943): the CrowdSec
  home-allowlist cron keeps its own curl but logs the library's line shape, and pi-peer-backup's
  CronJob, a pod with no `logger`, echoes its `status=` and `push failed (` lines in the syslog
  prefix shape to stdout. `SWALLOWED_VERDICTS_POD_LOGQL` (`{container="pi-peer-backup"}`) reads
  them, and the two fetches are merged before the verdict. The selector is not
  `job=~"syslog|k8s"`, because the cap was sized against the syslog stream alone.)
- **Kuma Notification Delivery** (a notification Kuma tried to send and dropped, #1891. Kuma logs
  `Cannot send notification to <name>` and does not retry, so the transition behind that line
  reached nobody (both observed cases were Discord HTTP 429s during the qbittorrent lockout).
  Discord Delivery GET-verifies the webhook and cannot see a dropped POST.
  `check_kuma_notify_failures` reads Kuma's line out of `{container="uptime-kuma"}` over
  `KUMA_NOTIFY_FAILURES_WINDOW_S` (3h) through `src.loki_lines`, and `down` names each
  notification with its drop count and the reasons Kuma recorded (`reasons: HTTP 429 Too Many
  Requests x2`). The reason is Kuma's NEXT line, also at ERROR level (#1895: all 74 drops in 14
  days had one, 69 x 429 and 5 x 400), so the LogQL fetches both lines and counts reasons as a
  set beside the drops rather than joining them one to one. A drop with no reason in the window
  reads `reason not logged`. An HTTP reason is reduced to its status before it reaches the tile,
  because the axios message can carry the request URL and Discord's is the webhook secret.
  Raising Kuma's log level is neither needed nor safe (uptime-kuma/CLAUDE.md, the debug-logging
  trap).
  A 400 is an oversized push `msg`: Kuma puts a push monitor's `msg` into a Discord embed field
  capped at 1024 chars and never truncates it. `bridge.net.push` and `kuma-push-lib.sh` therefore
  cap the `msg` at 900 chars (`PUSH_MSG_MAX`, keeping a trailing `(N cycles)`, #2013), and the
  release-staleness-check producer pushes names only.
  The window is the whole hysteresis: a drop pages for 3h and clears on its own. The tile
  notifies EMAIL as well as Discord, on purpose, because a page for a dropped Discord send that
  goes only over the same webhook is the failure it reports. In `LOKI_DEPENDENT`; a fetch that
  hits the 500-line cap says so in the message.)
- **Discord Delivery** (GET-verifies **all five** Discord notification webhooks: Kuma's own
  `monitor_discord_webhook_url`; CrowdSec's `crowdsec_discord_webhook_url`, which CrowdSec posts
  ban alerts to *directly*, not via Kuma; `gitops_deploy_discord_webhook`, which delivers the
  `gitops-deploy` rollback alert AND every `renovate_notify` digest (the Renovate Notifier — Alive
  marker greens even when the POST fails); `arr_discord_webhook_url`, which Sonarr/Radarr/Prowlarr
  POST their own onHealthIssue alerts to through in-app Discord Connect (the config lives in the
  app DBs, and the Arr Queue check covers stuck downloads, not indexer or download-client
  health); and `healthchecks_discord_webhook_url`, the retired self-hosted healthchecks app's
  check-down/up webhook (#2806), which stays verified because nothing in the repo records whether
  the healthchecks.io account's own integrations post to it. The latter four have no Kuma
  backstop. `down` if ANY is invalid, naming which; each empty URL is skipped. A rotated, revoked
  or deleted webhook makes those alerts fail silently while every monitor stays green in the Kuma
  UI. No other monitor, not even the off-box UptimeRobot host dead-man, exercises this delivery
  hop. A webhook GET returns Discord's metadata (200) when valid and 404 once gone, and posts no
  message, unlike a test POST.
  **It also probes the alert-EMAIL second channel** (`email_backstop`): the Gmail SMTP
  notification attached only to THIS monitor as the escape hatch when the Discord webhook is dead.
  The probe is a throttled SMTP login with the creds Kuma uses (`SMTP_USER`/`SMTP_PASSWORD`), so a
  revoked app-password flips this monitor down and still pages over the working Discord channel.
  It runs at most once per `EMAIL_PROBE_INTERVAL_S` (6h, because Gmail flags frequent `AUTH`
  commands): a success is cached and a failure re-probes every cycle. Empty `SMTP_PASSWORD`
  disables that probe.
  The webhooks and SMTP reach the public internet, so `DISCORD_CONSECUTIVE` (2) adds the streak
  hysteresis of the HA heartbeat: one transient non-200 or network failure pushes `up` with a "down
  streak n/N" `msg`, and the second straight failure pages. Empty `DISCORD_WEBHOOK_URL` disables
  the check (stays up). The check verifies that the webhook is DELIVERABLE (it catches a rotated or
  revoked URL). It does not assert that Kuma still has the notification *attached* to each
  monitor, which AutoKuma re-applies on every deploy through the `kuma()` macro's
  `notification_name_list`. Pure `discord_webhook_ok()`, `email_backstop()`'s throttle and the
  streak wrapper are unit-tested.)

## Retired and moved checks

Checks that left `CHECKS` push their monitors from elsewhere, with the same tokens, so each
monitor keeps its history. The direct pushers are the `pi-peer-backup` k8s CronJob (WG Pi Peer
Backup), the `crowdsec` role's allowlist cron (CrowdSec Home Allowlist),
`crowdsec-appsec-verify.sh.j2` (CrowdSec AppSec), the `fake_remux` setup role's
`fake-remux-health.sh` (Fake Remux Scan / Replace), `renovate-notify`'s `ExecStartPost`
(Renovate Notifier — Alive), and the configarr and janitorr roles' health crons (Configarr
Sync, Janitorr Errors). Disk Autoprune retired with the Docker daemon and has no successor.
Cluster Prometheus Reachable retired as a gate (#2825), and its four members are in
`PROM_DEPENDENT`. Each check's retirement note is in the repository history.

## Push-monitor mechanics, and what set each number

- The restart/OOM/cpu/target/5xx/cert checks use `prom_vector()` (keeps series labels) so the
  alert names *which* container / target / route / certificate is failing; the others use
  `prom_scalar()`.
- An explicit `down` gives a fast, descriptive alert. The push monitor's heartbeat interval
  (`uptime_kuma_k8s_bridge_push_interval`, 1200 s = 4x the loop) is the backstop for "the bridge
  itself died", the same dead-man's-switch idea as `cloudflare-ddns` and the
  `kuma(..., monitor_type='push')` macro. At 1200 s the monitor absorbs three missed pushes, so a
  host restart does not page. At 600 s one restart (8m35s on 2026-08-30) flipped nine bridge-fed
  tiles DOWN for an event that had already ended. The knob is the interval and not `max_retries`,
  which buys the same tolerance and costs the descriptive message (next bullet).
- **All push monitors set `max_retries=0`.** With retries, Kuma parks a pushed `down` in PENDING,
  and the 60 s watchdog, which only `up` pushes satisfy, crosses `maxretries` first. Every DOWN
  event then reads "No heartbeat in the time window" instead of the check's named-offender
  `msg`. With zero retries the bridge's own push flips the state, so the descriptive `msg` lands
  in the event and the Discord notification. A dead bridge therefore pages after one missed
  heartbeat window, which is the dead-man's switch working.
  **Post-boot flapping is fixed by widening the window, never by adding retries.** A wider window
  costs only detection latency, while a retry hands the Discord message back to the watchdog.
  `test_push_monitors_never_retry` in `ansible/tests/services/test_kuma_static_monitors.py` is the
  guard.
- **`STARTUP_GRACE` holds the reach-out checks that have no reachability gate and no per-check
  hysteresis**: **n8n Prod Workflows**, **Bazarr Health**, **Prowlarr Indexers** and **SMART Data /
  Health**. It is a hysteresis, not a suppression, and it is the peer of
  `PROM_DEPENDENT`/`LOKI_DEPENDENT`. The cause is the weekly Sunday 07:30 host reboot: the
  bridge's first cycle runs before those heavy apps finish starting, so each un-graced
  `max_retries=0` monitor flipped DOWN for one cycle (`Connection refused`, n8n `HTTP 404`) and
  recovered on the next. `apply_startup_grace()` in `run_once` holds each `up` for the first
  `GRACE_CYCLES`-1 (default 2-1 = 1) consecutive down cycles, with the "down streak n/N" idiom of
  `HA_CONSECUTIVE`. Only the `GRACE_CYCLES`'th straight down pages, one `INTERVAL` later, and one
  `ok` resets the streak. The set is **disjoint from every `run_once` skip set**, so a graced
  check reaches the evaluation path each cycle and its streak advances. A test against `CHECKS`
  guards both invariants. `GRACE_CYCLES` is env-tunable, and pure
  `bridge.streaks.apply_startup_grace()` is unit-tested.
- Thresholds are env-tunable in `templates/env-secret.yaml.j2`, the only place each value is
  written. *Threshold defaults* below tabulates the numeric ones from that template, and the
  per-check prose above gives the reason for a number. A failed query or unreachable source makes
  that monitor `down` with an explanatory `msg`, so a broken exporter is surfaced and not silently
  green.

### Threshold defaults

Every value below is a literal in the template, so a change there moves this table on the next
`gen_doc_fragments.py` run. A key rendered from a role variable or a secret is not listed.
`*_CONSECUTIVE` is a count of cycles at `INTERVAL` seconds each.

--8<-- "assets/generated/fragments/bridge-thresholds.md"


## Traps: the incidents behind the rules

The rule each of these produced is in the role `CLAUDE.md`'s *Traps* section; this is the
evidence.

### kube-state-metrics sanitizes resource names into labels
`kubectl describe node` prints `devic.es/dri`. kube-state-metrics emits
`kube_node_status_allocatable{resource="devic_es_dri"}`, because every character outside
`[a-zA-Z0-9_]` becomes `_`. A query written with the Kubernetes name matches no series.

A check that fails closed on an absent series cannot tell "the resource is deregistered" from "I
asked the wrong question," since both return an empty vector. On 2026-08-20 the extended-resource
arm paged for 24 minutes with `extended resource(s) advertised by no node: devic.es/dri`, while
both nodes advertised the resource at capacity 4. Fail-closed is right: a typo pages instead of
going green.

The arm sanitizes the configured name at query time, keeps the operator-facing name the one
`kubectl` prints, and names both forms in the fault message, so the next mismatch is diagnosable
from the alert alone. Before trusting a metric-backed check, run its exact query against live
Prometheus and confirm it returns rows. The unit tests mock the payload, so they prove the verdict
logic and nothing about the selector. Guarded by `ksm_resource_label` in
`files/verdicts/cluster.py` and its test in `tests/test_check_longhorn.py` (PR #286).

### The k8s log streams have no `app` label
`kubectl` selects pods with `-l app=home-assistant`, so a LogQL selector written from that habit
reads naturally and matches nothing. The k8s stream that Alloy ships carries `container` / `pod` /
`job` / `machine` / `namespace` / `service_name` / `stream` / `filename`, and no `app`.

`HA_BAN_SELECTOR` once shipped as `{namespace="homelab",app="home-assistant"}`, matched no stream,
and pushed `no ip_ban events in 1h` through a window that contained `Banned IP 10.42.0.1 for too
many login attempts`. The arm fails open by design, so a wrong question and a clean bill of health
are the same output. **A fail-closed check pages on a typo, and a fail-open check goes green on
one.**

Unit tests cannot catch this, because they mock the payload. Run the selector against live Loki
over a window containing a KNOWN event, which is the only check that separates "nothing happened"
from "nothing matched." A green first cycle is not evidence. `LOKI_STREAM_LABELS` and
`test_loki_selectors_use_real_stream_labels` in `test_check_loki.py` pin the vocabulary for all
three Loki selectors.

### The runtime stamps the log lines; `bridge.common.log` does not
`bridge.common.log` prints the bare message (`DOWN b2_reachable ...`) and the container runtime
supplies the time. Pass `--timestamps` to `kubectl logs` when building a timeline, and read that
prefix as UTC.

`bridge.common.log` once printed its own bracketed stamp, and that stamp was the trap:
`time.strftime` with no offset rendered the container's local America/Chicago wall clock as if it
were an ISO instant. A bracketed `07:26:57` paired with kubectl's `12:26:57Z` (2026-08-16), and
reading the brackets as UTC placed a B2 cap breach 5 hours early. The stamp was dropped rather
than made offset-aware, because two stamps that disagree are the fault.

A log line archived with the bracket is Central time. Any homelab container that sets
`TZ=America/Chicago` and stamps its own lines has the same problem, so cross-check one line
against `date -u` before anchoring an incident timeline on it.
