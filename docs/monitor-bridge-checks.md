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
  surfaces separately as `ScrapeTargetDown`. The `prometheus` gate's members derive from the `gate="prometheus"`
  rows, so neither a count nor an enumeration of it is written anywhere else. The role CLAUDE.md
  once carried a hardcoded count of ten while the set held more (#1359), and then an enumeration a
  test pinned, until the column made both redundant (#3788).)
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
  they reset on a bridge restart, which the `startup_grace` hysteresis rides out. Pure
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
  `arr_queue`'s `startup_grace` membership** rather than stacking on it, since that grace covered
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
  (90060 s: the larger of `monitor_bridge_builder_max_lag_s`, 7200 s, and the Pi canary's longest
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
  (`docs/archive/k3s-migration/backup-consolidation-longhorn.md`). The `b2_reachable` gate's members are
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
  `test_check_b2_gate.py` does this. `test_check_table.py` derives each gate's members from the live
  `CHECKS`.
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
  failure is more often a 5xx. Empty credentials disable the check. **Behind `b2_reachable`**: a
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
  `speedtest` is also `startup_grace`. This is the same split as `check_ha_heartbeat`.
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
  Notification Delivery tile, `cloudflare-ip-drift`, `swallowed_verdicts` and `gitops_alive`. It gates the `wan_reachable` rows:
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
  a row names at most one gate, and a gate row has none of its own. Two tiles
  for a WAN outage is the accepted cost of not building gate-of-a-gate for one caller.
  `swallowed_verdicts` is not a member either: it reads in-cluster Loki, and what failed for it
  was Kuma's own outbound send, a real fault the tile should report.
  **The HOST crons are covered by a separate half** (#2793). `release-staleness-check`,
  `docs-refresh` and, until M6 batch B3's Cutover-2, `github-ruleset-drift` run on the host and push their own tiles, so this
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
  `r2_usage` and `healthchecks_drift` left `startup_grace` to join this set, as `pi_pressure` did
  when it gained the Prometheus gate (#2004): the two sets must stay disjoint so a graced check
  reaches the evaluation path every cycle, and the gate covers the post-boot transient the grace
  covered plus the outage it never could. Empty `WAN_PROBE_URLS` disables the gate.)
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
Root Disk, Memory, Host Temperature and UPS Battery Health (M6 batch A1) retired into the
`hosts` rule group in observability, and their tiles, rows, check bodies, verdicts and env keys
are gone. The thresholds, floors and `for:` durations those
rules carry are pinned in `ansible/tests/services/test_host_check_rule_literals.py`, where the
`DECIDED:` markers for the 1h temperature hold, the sanity-bounded hwmon limits, the Ryzen
maximum junction temperature, the nut-only UPS source and the retired-uid note now sit. That file also holds the
`ClaudeCgroupNotReporting` host and cgroup pairs to the inventory, and
`test_claude_cgroup_consumers_agree_with_the_writer.py` holds the rules' `claude_cgroup_*`
selectors to the series the writer emits. The reasoning behind each rule (the `up == 1` start of
every missing-data arm, the per-host copies, the `unless on ()` that lets mains loss outrank the
UPS runway arms) is in `ansible/roles/k8s/observability/files/rules/hosts.yaml`.
Pi Pressure (M6 batch B8) retired into `PiLoadHigh`, `PiMemoryLow`, `PiDiskFull`,
`PiFilesystemReadOnly` and `PiMetricsMissing` in `hosts.yaml`, and into `PiPortDead`. Its row,
tile, check body, verdicts, `PI_*` keys and push token are gone, and the 14 hand-written Kuma
probe tiles retired into the `extra-probes` group beside it. The read-only arm (#2668) has no
grace, like `KubeletCSIMountReadOnly`, because a read-only remount does not self-heal. The
filesystems are keyed by device, because the SD card is mounted twice (`/` and `/var/hdd.log`),
and tmpfs is out, because log2ram's 128 MiB `/var/log` fills and flushes by design. The Pi's own
rules exist because the `hosts.yaml` disk and memory rules name daniel-box and daniel-server: a
90% memory floor on a 456 MB box would fire at 45.6 MB available, where `PiMemoryLow` fires at
50 MB. The published-port arm is `PiPortDead`: one `tcp_connect` target per Pi container with a
`port` in the `blackbox-pi-ports` job, held for 10 minutes, which is the bridge's two cycles. After a
Pi restart a container can come back attached to no Docker network while it still reports
`Up (healthy)`, because its healthcheck curls loopback inside its own netns. Its published port
stops listening, and only a recreate restores it, because autoheal's restart loop re-enters the
same empty sandbox. The rule's description carries that hint, and `ssh daniel-pi docker ps` tells
a stopped container from a detached one. `PiHostDown` inhibits `PiPortDead`, as
`EXPORTER_DEPENDENT["node-pi"]` suppressed the port arm while the Pi's exporter was down.
`udp_port` is not probed, because a UDP port has no TCP-connect equivalent. The arm adds a named
port and no reachability coverage: Kuma HTTP-monitored wg-easy and `ScrapeTargetDown` covers
`alloy`, but nothing said which port went quiet, and the 2026-08-08 sweep across four monitors for
that answer missed dozzle. The Pi limits are pinned in
`ansible/tests/services/test_host_check_rule_literals.py`, the port set in
`ansible/tests/services/test_pi_publishing_containers.py`, and `PiPortDead`'s hold and recreate
hint in `ansible/tests/services/test_extra_probe_targets.py`. A fourth node-exporter host needs
rules added to `hosts.yaml`: its per-host copies name the hosts, and
`HostTemperatureCoverageShortfall` holds a literal floor of 3 hosts, so a fourth host makes that
floor satisfiable by any three of four and one host can go dark silently.
`test_every_node_exporter_job_is_mapped_in_exporter_dependent` (test_check_exporters.py)
forces the decision for a host under a new scrape job, which is how daniel-pi arrived.
Workload Log Errors (M6 batch A7) retired into the `WorkloadLogErrors` rule in
`ansible/roles/k8s/loki-homelab/files/rules/homelab-logs.yaml`, and its tile, row, check body,
`with_log_errors`, the `log_error_verdict` verdict and the `LOG_ERROR_*` env keys are gone. The
rule counts, per container, the lines matching `panic:`, `fatal`, `traceback` or `out of memory`
over 1h in the cluster pod and daniel-pi container streams, and fires above 20 lines. Its
`for: 5m` replaces the row's `startup_grace`. The `DECIDED: stays at 20` marker, which records
the two home-assistant bursts that held the tile red for 46 and 43 minutes, sits above the rule.
The rule is a Loki rule, so its alert never reaches Prometheus `ALERTS` and `probe.py health`
cannot see it; `loki_prometheus_rule_*` and the ruler's notification counters are the live read.
Cluster Prometheus Reachable retired as a gate (#2825), and its four members are in
the `prometheus` gate's members. Each check's retirement note is in the repository history.

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
- **`startup_grace` holds the reach-out checks that have no reachability gate and no per-check
  hysteresis**: **n8n Prod Workflows**, **Bazarr Health**, **Prowlarr Indexers** and **SMART Data /
  Health**. It is a hysteresis, not a suppression, and it is the peer of
  the `prometheus` gate's members. The cause is the weekly Sunday 07:30 host reboot: the
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
