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
  surfaces separately as `ScrapeTargetDown`. `PROM_DEPENDENT` derives from the `gate="prometheus"`
  rows, so neither a count nor an enumeration of it is written anywhere else. The role CLAUDE.md
  once carried a hardcoded count of ten while the set held more (#1359), and then an enumeration a
  test pinned, until the column made both redundant (#3788).)
- **Root Disk** (`node_filesystem_*` for `/`, `/boot` **and `/boot/efi`**: old kernels filling
  /boot quietly break upgrades, and a full ESP breaks firmware/bootloader updates the same way.
  Server-only; the Pi's disk lives in the Pi Pressure check. Root Disk pages without remediating:
  containerd's image GC owns image cleanup since the Docker daemon retired, and no autoprune cron
  replaced the old one.)
- **Memory** (host `node_memory_*` pressure only)
- **Host coverage floor** — not a monitor of its own, but a second arm inside **Root Disk** and
  **Memory** (`_host_origin_shortfall`). Both checks group by `origin`, and node-exporter is a
  DaemonSet on both nodes, so a vector covering fewer than `HOST_ORIGINS_MIN` (2) distinct hosts
  has **lost** a host rather than measured a healthy estate, and the surviving node's numbers
  would be reported as the estate's. This happened once: a one-directional UFW rule left
  daniel-box's node-exporter unreachable for 5.4h (2026-08-23), and both checks pushed OK off
  daniel-server alone while daniel-box's host memory and `/boot` went unwatched.
  **Why not lean on `ScrapeTargetDown`:** that keys on `up`, and node-exporter's normal failure is
  PER-COLLECTOR. A filesystem or meminfo collector can fail with `up == 1`, leaving `ScrapeTargetDown`
  green while the host silently drops out of these two checks. This is the shape of `check_ups`'s
  partial-absence arm: never monitor the survivor silently.
  `HOST_ORIGINS_CONSECUTIVE` gives it hysteresis, for the reason `UPS_CONSECUTIVE` exists: the
  weekly Sunday restart takes a node's exporter away against a 1m scrape and a 5m loop. The
  shortfall is reported AFTER each check's own breach scan, because a reporting host that is
  genuinely out of memory outranks a complaint about the absent one. `HOST_ORIGINS_MIN` is
  rendered in `templates/config/env.yaml.j2` so a planned single-node maintenance window can lower
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
  `CLAUDE_CGROUPS` (rendered in `templates/config/env.yaml.j2`, `claude-rc,fleet`) is the set
  whose ABSENCE is a fault, not the set that is judged. The queries filter by metric, so
  `user-1000-slice` is watched whenever it exists and its absence never pages, because that cgroup
  only exists once somebody has logged in since boot. **Empty disables the whole arm.** The full
  threshold derivation, settled against 5.8 days of history that included both cgroups hitting
  their MemoryHigh caps (#1288), is at `CLAUDE_CGROUP_STALL_MAX_PCT` in `bridge/config_host.py`.
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
  2026-09-11 were a fetch error co-timed with a k3s Workload Health `radarr(1)` episode, one rollout
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
  cap and was OOM-killed, and the only signal was the `k3s Container OOM` tile (now `ContainerOOMKilled`), which clears one
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
- **Host Packages — Status** (`host_packages`, reads the host-package builder's two textfile series
  through daniel-box's node exporter, the `node` job. It is a row of its own, because
  `gitops_status` reads the deployer's state directory outside the Prometheus gate, and a
  Prometheus-sourced arm there would page on every Prometheus outage. The row is `gate="prometheus"`
  and sits under `EXPORTER_DEPENDENT["node"]`, so a dead exporter pages once through `ScrapeTargetDown`
  instead of also as an absent builder series. It goes `down` on either arm. The **fetch arm**:
  `homelab_builder_fetch_timestamp_seconds` is older than `BUILDER_MAX_FETCH_AGE_S` (1800 s,
  `monitor_bridge_builder_max_fetch_age_s`) or absent, which covers a dead builder timer, a missing
  token and a fetch that keeps failing, because a failed fetch writes nothing. The **lag arm**:
  `homelab_builder_unbuilt_since_timestamp_seconds` is non-zero and older than `BUILDER_MAX_LAG_S`
  (11760 s: the larger of `monitor_bridge_builder_max_lag_s`, 7200 s, and the Pi canary's longest
  gap between updater fires plus the timer's delay, the run timeout and 600 s of slack, which
  `filter_plugins/updater_lag_grace.py` derives from the unit files, #6530), which covers a trust, keyring or tools refusal, a
  rewritten master, an empty CI context list, a CI context that no longer matches, a builder hold,
  a rollout gate the canary never closes and a master that stays red. In each lag case the builder
  runs and exits 0 while no host receives a change, so no unit failure reports it. The bound
  exceeds the ~103 s master CI run many times over. An absent unbuilt-since series pages too, and
  the oldest non-zero series is judged when a label change leaves two. Every message names the
  series' `reason` and `sha` labels, and `docs/host-packages.md`, *The builder's pages*, has the
  fix for each reason. The query carries no `origin` matcher: `origin_sel()` pins daniel-server,
  and only daniel-box runs the builder. Pure `host_packages()` and the arm pairs are unit-tested.)
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
  `CLOUDFLARE_IPS_EXPECTED`, the `cloudflare_ips` allowlist traefik trusts and whose IPv4
  half netpol-baseline admits. A success is cached for `CLOUDFLARE_IPS_PROBE_INTERVAL_S` (a day). Drift, a page with
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
  condition. `ContainerCPUThrottled` sees CFS throttling, which is a cgroup limit rather than heat,
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
    that gate is not affirmatively up (`ScrapeTargetDown` owns a dead scrape, and an
    unqueryable gate defers too), and pages while the Pi IS scraping, because the sensor was
    renamed or the collector went blind.
  - **CPU thermal throttling** (`_thermal_throttle_arm`, `THERMAL_THROTTLE_QUERY` =
    `node_cooling_device_cur_state{type="Processor"}`). A non-zero `cur_state` means the kernel is
    derating the CPU now. It is a different fault from `ContainerCPUThrottled` (CFS throttling, a
    cgroup quota, not heat) and from the temperature arm, since the firmware can enforce a limit
    below the one the driver declares. `type="Processor"` is load-bearing: unfiltered, the metric
    also carries PCIe link-speed and `intel_powerclamp` devices, which throttle for reasons that
    are not heat. The arm has its own source gate, `THERMAL_THROTTLE_UP_QUERY` = `up{job="node"}`
    (`node` rather than `node-pi`, because the Pi publishes none of these series). A fully empty
    vector therefore means both amd64 exporters went quiet, which `ScrapeTargetDown` owns, and
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
  `up{job="nut"} == 0` and `ScrapeTargetDown` already pages for. HA's `hass_*` UPS series stay in the
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
  the nut scrape is down defers to `ScrapeTargetDown`'s page. That covers a dead upsd as well as a dead
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
  the retired Kubelet CSI Mount Read-Only check, because a read-only remount does not self-heal. Filesystems are keyed
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
  Prometheus scrape target, `job=alloy-pi`, that `ScrapeTargetDown` covers). What nothing said was
  *which* port went quiet, and the 2026-08-08 sweep across four monitors for that answer missed
  dozzle entirely. Do not re-justify this arm as filling a monitoring gap.)
- **Home Assistant Automations** (HA's REST API `/api/states/input_datetime.ha_heartbeat` over
  `apps`, Bearer `HA_TOKEN`. An HA `time_pattern:/1min` automation stamps that helper with
  `now()`, so its `last_changed` is fresh ONLY while HA's automation *scheduler* is executing.
  `down` once it is older than `HA_HEARTBEAT_MAX_AGE` (300 s): a wedged-but-running HA (HTTP
  `:8123` up, scheduler stuck) that the container healthcheck cannot see. **`HA_CONSECUTIVE` (2)
  adds consecutive-cycle hysteresis** (the idiom of `UPS_CONSECUTIVE`): a planned redeploy takes
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
  **`ha_heartbeat` is deliberately not gated on Loki**: a Loki gate would suppress the WHOLE check
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
- **WAN Reachable** (two provider URLs fetched by hostname, tried in order: the root-cause GATE for
  the internet-reaching checks, and the peer of Prometheus and B2 Reachable. An internet outage
  had no gate until a single WAN outage on 2026-09-18 turned 11 tiles red inside 90 minutes,
  including four host crons and `b2_reachable`, `r2_usage`, `discord`, the since-deleted Kuma
  Notification Delivery tile, `cloudflare-ip-drift`, `swallowed_verdicts` and `gitops_alive`. It gates `WAN_DEPENDENT`:
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
  `swallowed_verdicts` is not a member either: it reads in-cluster Loki, and what failed for it
  was Kuma's own outbound send, a real fault the tile should report.
  **The HOST crons are covered by a separate half** (#2793). `github-ruleset-drift`,
  `release-staleness-check` and `docs-refresh` run on the host and push their own tiles, so this
  gate structurally cannot suppress them. `crowdsec-home-allowlist` was a fourth until H2.4c moved
  it into the cluster. Its `crowdsec-home-ips` CronJob, on daniel-box's host network, probes
  the same endpoints in `home_ips_discover.py` and marks a failed lookup `WAN unreachable`.
  The CrowdSec sidecar's job reports that as an `up` skip. Each host cron consults `wan_reachable`
  in `ansible/roles/setup/initial_setup/files/kuma-push-lib.sh` on the failure path that reached
  the internet, and reports `skipped: WAN unreachable` as an `up` when neither provider answers.
  The probe classifies, not the error text: parsing curl exit codes and git stderr across
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
- **Workload Log Errors** (the log-pattern arm of the former k3s Workload Health monitor. It
  counts, per container, the lines matching `LOG_ERROR_PATTERN` (`panic:`, `fatal`, `traceback`,
  `out of memory`) over `LOG_ERROR_WINDOW` (1h) in the `LOG_ERROR_SELECTOR` streams, and pages
  when one container exceeds `LOG_ERROR_MAX` (20). A container that is Ready while failing at its
  job reads healthy to every Kubernetes-state check, which is why this arm exists. It keeps the
  tile and push token the whole monitor had (`kuma_id` `monitor-bridge-k8s-workloads`), so the
  tile's history survives its rename.
  **The Kubernetes arms are gone from the bridge.** Unavailable replicas, a stalled rollout, zero
  available replicas, DaemonSets, crash loops and a deregistered `devic.es/dri` are the
  `k8s-workloads` rules in observability (`files/rules/k8s-workloads.yaml`), whose comments carry
  the hold, floor and window reasoning that used to live here.
  **The row's gate is `startup_grace`**, not `prometheus`: it reads no Prometheus, and the arm
  fails open on a Loki error by itself (`with_log_errors`), so it names no reachability gate. The
  cost of the grace is one held cycle, so the row pages on its second consecutive bad cycle. A
  burst stays in the 1h window far longer than that: the two bursts the `DECIDED: stays at 20` (now in `homelab-logs.yaml`)
  marker records held the tile red for 46 and 43 minutes.
  The row is temporary. M6's batch A7 ports the arm to a LogQL rule on `loki-homelab` and deletes
  the row, `check_workload_log_errors` and `with_log_errors`.)
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
  `application/json` rather than Traefik's `text/plain` page (#1803). The edge and Kuma are both
  up, so no other tile pages. Kuma answers 404 for any exception in its push handler, so the
  rejection is either a token no live monitor holds or a Kuma-side error, such as the 2026-10-11
  `SQLITE_CONSTRAINT` on `stat_hourly` that a redeploy did not clear (#4481). The library logs
  Kuma's own `msg` on a `push rejected by kuma (http=<code>): <msg>` line just before the
  failure line, with the push token redacted, and the page names that line. The `crowdsec-jobs`
  sidecar writes the same line to its container log, as pi-peer-backup does to its pod log. A token mismatch
  reads `Monitor not found or not active.`, and its verdicts are lost for as long as the cron
  and the static monitors carry different tokens. A burst of `http=404` lines from Traefik rejecting every router belongs to
  `TraefikEntrypoint404Flood` (#1322).
  Gated by `startup_grace`, and it fails open on a Loki error. A fetch that hits the 5000-line cap says so in the message. Two pushers
  bypass the library's shape and are read by a second selector (#1943): the `crowdsec-jobs`
  sidecar of the CrowdSec engine pod and the pi-peer-backup, configarr-health and
  janitorr-health CronJobs (#5569). No such pod has a `logger`, so each
  echoes its `status=` and `push failed (` lines in the syslog prefix shape to stdout.
  `SWALLOWED_VERDICTS_POD_LOGQL`
  (`{container=~"pi-peer-backup|crowdsec-jobs|configarr-health|janitorr-health"}`) reads
  them, and the two fetches are merged before the verdict. The selector is not
  `job=~"syslog|k8s"`, because the cap was sized against the syslog stream alone.)
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
Backup), the `crowdsec-jobs` sidecar in the CrowdSec pod (CrowdSec Home Allowlist, CrowdSec
Remote Allowlist and CrowdSec AppSec), the `fake_remux` setup role's
`fake-remux-health.sh` (Fake Remux Scan / Replace), `renovate-notify`'s `ExecStartPost`
(Renovate Notifier — Alive), and the configarr and janitorr roles' health crons (Configarr
Sync, Janitorr Errors). Disk Autoprune retired with the Docker daemon and has no successor.
Scrape Targets, Cluster Scrape Targets and Log Shipper Dropped Entries (M6 batch A3) retired
into the `scrape-targets` and `log-pipeline` rule groups in observability, and their tiles,
rows and env keys are gone. The numbers those groups carry are pinned in
`ansible/tests/services/test_scrape_target_rule_jobs.py`, and the `DECIDED:` marker for the
3000-entry threshold sits at `LogShipperDroppingEntries` in
`ansible/roles/k8s/observability/files/rules/log-pipeline.yaml`.
TLS Cert Expiry, Traefik 5xx, Traefik Latency, Traefik 404 Flood and Traefik 421 (M6 batch A4)
retired into the `edge` rule group in observability, and their tiles, rows and env keys are
gone. The numbers that group carries are pinned in
`ansible/tests/services/test_edge_rule_literals.py`. The `DECIDED:` markers for the exempt
stream services, for bazarr and prowlarr staying measured, and for jellyfin staying measured
(#6649) sit there too, and the `DECIDED:` marker for the 5-of-10-minute latency count sits at
`TraefikBackendSlow` in `ansible/roles/k8s/observability/files/rules/edge.yaml`.
k3s Container Restarts, k3s Container OOM and k3s CPU Throttling (M6 batch A2) retired into the
`container-checks` rule group in observability, and their tiles, rows, streak counters and env
keys are gone. The 20-pod census floor behind `CAdvisorCoverageShortfall` is pinned in
`ansible/tests/services/test_container_checks_rule_literals.py`, where its `DECIDED:` marker
now sits. The CPU rule's 3-cycle streak became `for: 15m` over a `max_over_time(...[5m:1m])`
smoothing, whose reasoning is at `ContainerCPUThrottled` in
`ansible/roles/k8s/observability/files/rules/container-checks.yaml`.
Longhorn Volume Redundancy, k3s PVC Fullness, Longhorn Snapshot Headroom, Kubelet CSI Mount
Read-Only and etcd DB Size (M6 batch A5) retired into the `storage` rule group in
observability, and their tiles, rows and env keys are gone. The thresholds those rules carry
are pinned in `ansible/tests/services/test_storage_rule_literals.py`, which also holds
`PVCBelowFreeFloor` to the transient valheim declares, `LonghornSnapshotNearCap` to the
jellyfin snapshot cap, and `LonghornVolumeDegraded` to `longhorn_robustness.SAFE_ROBUSTNESS`.
The `DECIDED:` marker for the 3 GiB free-bytes floor sits there too.
`ansible/tests/setup/test_etcd_quota_and_its_monitor_agree.py` holds `EtcdDBNearQuota`'s
quota to the one k3s runs. The reasoning behind each rule (the one-hot `state` label, the
`max by` that reads a doubled scrape once, the claim census, why an absent etcd series is
`ScrapeTargetDown`'s) is in `ansible/roles/k8s/observability/files/rules/storage.yaml`.
Cluster Prometheus Reachable retired as a gate (#2825), and its four members are in
`PROM_DEPENDENT`. Each check's retirement note is in the repository history.

## Push-monitor mechanics, and what set each number

- The pod-count, target and route checks use `prom_vector()` (keeps series labels) so the
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
  `PROM_DEPENDENT`. The cause is the weekly Sunday 07:30 host reboot: the
  bridge's first cycle runs before those heavy apps finish starting, so each un-graced
  `max_retries=0` monitor flipped DOWN for one cycle (`Connection refused`, n8n `HTTP 404`) and
  recovered on the next. `apply_startup_grace()` in `run_once` holds each `up` for the first
  `GRACE_CYCLES`-1 (default 2-1 = 1) consecutive down cycles, with the "down streak n/N" idiom of
  `HA_CONSECUTIVE`. Only the `GRACE_CYCLES`'th straight down pages, one `INTERVAL` later, and one
  `ok` resets the streak. The set is **disjoint from every `run_once` skip set**, so a graced
  check reaches the evaluation path each cycle and its streak advances. A test against `CHECKS`
  guards both invariants. `GRACE_CYCLES` is env-tunable, and pure
  `bridge.streaks.apply_startup_grace()` is unit-tested.
- Thresholds are env-tunable in `templates/config/env.yaml.j2`, the only place each value is
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

The `K8sExtendedResourceMissing` rule in `k8s-workloads.yaml` writes the sanitised label, and
its comment carries this trap. Before trusting a metric-backed check, run its exact query against live
Prometheus and confirm it returns rows. The unit tests mock the payload, so they prove the verdict
logic and nothing about the selector. Guarded by
`ansible/tests/services/test_dri_resource_name_agrees.py`.

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
from "nothing matched." A green first cycle is not evidence. `LOKI_STREAM_LABELS`, which
`ansible/tests/_loki_streams.py` derives, and `test_loki_selectors_use_real_stream_labels` in
`test_check_loki.py` pin the vocabulary for all three Loki selectors. The Loki ruler's rules
are held to the same vocabulary by `ansible/tests/services/test_loki_stream_labels.py`.

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
