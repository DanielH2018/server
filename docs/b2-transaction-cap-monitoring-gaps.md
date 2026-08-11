# B2 transaction cap, 2026-08-02 — what the monitoring did and didn't say

Backblaze B2 hit an **account transaction cap** on 2026-08-02. Every backup tier stopped
working: Kopia could not read its own repository blob, and Longhorn could not list its backup
store. Storage was never the constraint — the bucket sat at 6.05 / 10 GB (60%).

```
AccessDenied Transaction cap exceeded, see the Caps & Alerts page to increase your cap
403  Bucket: "daniel-server-kopia"  Prefix: "longhorn/"
```

The alerting was **not silent** — one monitor paged within nine minutes and stayed down. The
problems are narrower and more specific than "nothing caught it", and worth fixing individually.

## Timeline (UTC)

| Time | Event | Source |
|---|---|---|
| 10:02 | Longhorn `backuptarget/default` → `available=false`, error names the cap | `.status.conditions[].message` |
| 10:11 | `backup` Kuma monitor → DOWN, `backup check error: timed out` | `probe.py alerts --days 1 --check backup` |
| 10:11–19:41 | 110 consecutive down cycles, ~9.5 h, unactioned | same |
| ~19:31+ | Kopia server begins panicking on the failed refresh path | `docker logs kopia` |
| 19:41 | Investigated while proving the Longhorn→B2 backup path | this session |

Kopia's own log carries the exact cause:

```
unable to refresh repository: error refreshing content index: mutable parameters:
unable to read format blob: error getting kopia.repository blob: unable to complete
GetBlob(kopia.repository,0,-1) despite 10 retries: Transaction cap exceeded
```

and then, repeatedly:

```
http: panic serving 172.31.0.2:51260: runtime error: invalid memory address or nil pointer dereference
```

## What worked

The `backup` (Backup Freshness) monitor detected the outage nine minutes in and has been down
ever since. The detection path is fine. `STARTUP_GRACE` delayed it by exactly one cycle on the
post-redeploy re-observation, which is the hysteresis behaving as designed.

## Gaps

### G1 — The Longhorn backup plane has no monitor at all (high)

`grep -ic longhorn` over `monitor-bridge/files/check.py` returns **0**. The k3s backup target went
unavailable at 10:02 and nothing anywhere watches it. Slice 0 explicitly warned that
`available: true` proves reachability and not that an object was ever written — but there is no
monitor on even the weaker of those two claims.

This is the gap that matters most for the k3s migration: as services move to the cluster, their
only backup path is the one with no alerting.

### G2 — B2 is monitored on the wrong axis (high)

`b2_usage` and `b2_trend` are the two monitors pointed at B2, and both track **storage bytes**.
The cap that fired is on **transactions** (Class B/C API calls), a separate and independently
capped dimension. Both monitors stayed green throughout — `B2 6.05/10GB billable (60% of plan)` —
while B2 was refusing every request.

Worse than absent: they actively contradicted the one true alert. An operator triaging
`backup check error: timed out` who checks the B2 monitors is told B2 is healthy.

### G3 — The alert names a symptom, not a cause (medium)

The page said `backup check error: timed out`. That reads like a transient Kopia hiccup. The
actual cause — a billing cap denying every request — was sitting in `docker logs kopia` as a
plain-text string the whole time. Nine and a half hours passed unactioned, and an alert that had
said "B2: Transaction cap exceeded" would very likely not have.

### G4 — State-file monitors give days of false reassurance (medium)

`verify`, `content_verify`, `maintenance` and `b2_usage` all read from state files written by
periodic crons. During the outage they reported, respectively, 4.7 d, seeded, 0.7 d and 60% —
all green, because they are the *last successful run*, not current health. Their staleness
thresholds are 10 d / 100 d / 2.5 d / 2.5 d, so the backup plane can be dead for days while four
of six backup monitors look fine.

## Proposed remediation

The repo already has the right idiom for most of this: **reachability gates**. `Prometheus
Reachable` and `Loki Reachable` each front a `*_DEPENDENT` set whose members are suppressed
(pushed up with a "skipped" message) when the shared dependency is down, so one root cause pages
once instead of storming — and, critically, so dependents cannot report green through an outage.

1. **Add a `B2 Reachable` gate** — one cheap authenticated B2 call per cycle, reporting the real
   error text on failure. Fixes G3 directly (the message would have named the cap) and, with a
   `B2_DEPENDENT` set covering `backup`/`verify`/`content_verify`/`maintenance`/`b2_usage`/
   `b2_trend`, fixes G4: those monitors stop claiming health when the thing they describe is
   unreachable. This is the same shape as the two existing gates, guarded by the same
   drift test.

2. **Monitor the Longhorn backup target** (G1). The obstacle is that monitor-bridge runs on
   daniel-server and the target lives in the k3s API on daniel-box, so the established
   state-file-over-bind-mount pattern does not reach. Two options, and this needs a decision
   rather than a default:
   - a cron on daniel-box that reads `backuptarget/default` and pushes to Kuma directly — simple,
     but the repo deliberately moved *away* from direct Kuma pushes when `gitops_deploy` stopped
     pushing its own liveness;
   - give monitor-bridge read access to the k3s API. The read-only kubeconfig from PR #55 already
     exists and could be scoped and mounted, keeping all monitors in one place.

3. ~~**Track the transaction dimension, not just bytes** (G2).~~ **Addressed 2026-08-08**, after
   the cap fired a second time. B2 exposes no cap-headroom API, so nothing can measure the
   remaining allowance — what is measurable is the input that grew. `longhorn-backup-health.sh`
   gained a fifth arm that counts Longhorn backups created in the last 24 h against
   `k3s_longhorn_daily_backup_budget`, and reports the count in the green message so the number is
   visible before it breaches.

   Stated plainly because it bounds the check's worth: it is a **proxy on one consumer**. It cannot
   see kopia's or the b2-usage rclone's share of the same account-wide cap, and Longhorn's
   per-backup transaction cost was never measured — so the budget is a tripwire on growth, not a
   derived ceiling. It does **not** assert that today's 15 backups are within capacity.

## Not established

- **What consumed the transactions.** 2026-08-02 saw Longhorn's 03:30 daily backup of two volumes
  plus repeated PVC create/delete cycles during the Authelia and Traefik work, each of which makes
  Longhorn re-list the backup store. That is a plausible contributor, not a measured one — the
  split needs the B2 console. Deliberately not investigated further from this side to avoid
  spending more of the capped budget.
- ~~**Whether the cap is daily-resetting or account-lifetime.**~~ **Answered by the second
  occurrence: it resets.** Backups ran normally 2026-08-03 through 2026-08-07 after the 08-02
  breach, and the caps refused again on 08-08. So an outage self-clears at the UTC day boundary
  without intervention — what does not self-clear is the capacity problem underneath it.
- **Whether Kopia recovers on its own** once the cap lifts, given it is now panicking on the
  refresh path. It may need a restart.

---

## Second occurrence — 2026-08-08

Class B exhausted at 08:49 UTC, Class C at 10:09. The monitoring behaved as this document's
recommendations intended: `B2 Reachable` reported `transaction_cap_exceeded` verbatim, the five
`B2_DEPENDENT` checks were suppressed to a single alert, and `Backup Freshness` — deliberately
un-gated — paged on its own. Nothing had to be inferred from a timeout this time.

**Longhorn's hourly poll did not cause it.** `backuptarget/default` recorded
`lastSyncedAt: 09:47:25Z` — a *successful* sync an hour after Class B was already gone — and only
went `Unavailable` at 10:47:26Z, 38 minutes after Class C. The 2026-08-02 write-up named Longhorn's
re-listing as a plausible contributor; on this occasion it was demonstrably downstream.

**What Longhorn does do is amplify.** Once the target fails, the backup-target controller retries
on error at roughly **260 requests/minute** (1176 errors in a 12-minute window; two independent
60-second samples gave 256 and 263). Setting `backupstore-poll-interval` to `0` did **not** stop it
— the CR accepted `pollInterval: "0s"` and the rate was unchanged — because the retries come from
the controller's error requeue, not the poll timer. That lever was reverted to 3600.

Since the loop is error-driven, it should end at the UTC reset: the first list that succeeds stops
the requeue. That is an inference from the observed behaviour (the storm began only when the target
started failing, and the controller was quiet at one poll per hour before that), **not** something
observed — the test is whether the error rate drops to ~0 shortly after 00:00 UTC.

**The load that grew is the k3s migration's.** Longhorn's daily run went **4 → 7 → 15** backups on
2026-08-06/07/08 as slice 4 moved config PVCs into the cluster; the 08-08 run covered 3.79 GB
(~1806 2 MiB blocks). Incremental backups bound *bandwidth*, not *transactions*, which is why
`B2 Storage Usage` sat at 6.22 GB of 10 GB — 62%, green — throughout both outages. That asymmetry
is the whole of gap G2.

Kopia was a victim rather than a driver: the container stayed `healthy` with 2 cap errors in 10 h,
while `Backup Freshness` reported `kopia:51515: timed out`.

**Still not established:** the specific Class B consumer before 08:49. longhorn-manager logs had
rotated past it, and cluster pod logs do not reach Loki (the `pod` label has 0 values), so the
window is unrecoverable from this side.

### Remediation taken 2026-08-08: eight slice-4 config volumes left the backup set

Operator's decision, after the second breach, to cut Longhorn's daily run rather than raise the
cap. `sonarr`, `radarr`, `prowlarr`, `bazarr`, `qbittorrent`, `jellyfin`, `tdarr-server` and
`tdarr-configs` moved from the `default` recurring-job group to `no-backup`, taking the daily run
from **15 back to 7** — the level that was working on 08-07.

**It could not be done by changing the StorageClass.** A PVC's `storageClassName` is immutable, and
these were provisioned on `longhorn`; reclassifying would mean destroying and re-seeding each
volume. What actually binds a volume to `daily-backup` is the
`recurring-job-group.longhorn.io/default` **label on the Longhorn Volume CR**, which is mutable —
so the k3s role gained a reconcile driven by `k3s_longhorn_nobackup_volumes` (naturally idempotent:
a volume that has moved no longer matches the query that finds candidates). The roles'
`*_k8s_storage_class` defaults were switched too, so a volume rebuilt from scratch lands in the
right group without needing the list.

**What the eight still have.** Retention is enforced by the recurring job, and there is no longer a
job — so each keeps the single 2026-08-08 03:30 backup it already had, unpruned and unrefreshed. A
frozen restore point that ages from here, not nothing, and not protection. kopia's
`containers/<svc>/config` trees are a day older still, frozen at the 08-07 cutover.

**The coverage check had to follow the same signal.** Check 4 selected PVCs by
`storageClassName == "longhorn"`, which until now agreed with the backup set. Because the class is
immutable it now says `longhorn` for all eight, so class-based filtering would have paged every one
of them as uncovered, permanently. It selects on the recurring-job label instead — the class is a
creation-time proxy, the label is what `daily-backup` actually reads. Verified after the move:
`CHECKED=7`, none of the eight flagged.

`k3s_longhorn_daily_backup_budget` dropped 18 → 10 to match the smaller set. It reads red until the
08-08 03:30 run rolls out of the 24 h window, which is correct — 15 backups did happen today.

## Open at 2026-08-08 19:26 UTC

**The retry storm is still running, and its self-termination is untested.** Longhorn's backup-target
controller has hot-retried against a capped B2 since the breach; `backupstore-poll-interval: 0` did
not stop it (the CR accepted `pollInterval: "0s"` and the rate held), because the loop is the error
requeue, not the poll. Baseline taken now, so the post-reset check is a comparison and not an
impression:

```
kubectl -n longhorn-system logs -l app=longhorn-manager --since=2m --tail=-1 \
  | grep -c "Failed to get info from backup store"
```

**384 over 2 minutes — 192/min**, across the manager pods, all from
`BackupTargetController.reconcile`. The expectation is that the first successful list after B2's
00:00 UTC reset clears the error condition and the rate falls to ~0. That is an inference from where
the retry originates; it has not been observed. Re-run the same command after the reset. If the rate
holds, the storm is not cap-driven and the poll interval was never the lever.

**Eight config volumes are unprotected, and the cost of fixing it cannot be measured.** The
exclusion was the right call under a live cap, but it is a standing gap, not a resolution:
`sonarr`/`radarr`/`prowlarr`/`bazarr`/`qbittorrent`/`jellyfin`-config plus `tdarr-server`/
`tdarr-configs`, ~2 GB actual, one frozen restore point each.

The obvious question — would re-adding them cost what the first run cost? — has no measurable
answer here. Longhorn's first backup of a volume is a full and later ones ship changed blocks only,
so the 08-08 spike was plausibly a one-time migration cost that will not recur. But nothing exposed
by the cluster measures it: `Backup.status.size` is the backup's LOGICAL volume size, not blocks
uploaded (traefik-acme reports 35 → 48 MB across daily backups of a 25 MB volume), and B2 publishes
no cap-headroom endpoint — which is gap G2's whole premise. Treat "incrementals are cheap" as
documented upstream behavior that is unverified on this repo, against a cap that has now fired
twice. Deciding it needs a decision about the cap itself, per the note on
`k3s_longhorn_daily_backup_budget`.

### Done at 19:34 UTC, and the one step that could not be

The eight are back in the `default` recurring-job group, and the list is declarative in both
directions now — the reconcile was one-way, and the defaults file claimed the opposite. The reverse
reads intent from the **StorageClass**, not from absence in the list: most no-backup volumes
(prometheus, loki, registry, tempo, karakeep-meili, grafana, livesync, speedtest,
prowlarr-flaresolverr) were never in the list, and sweeping those in is the exact blowup being
guarded against. Verified against live state before applying — 15 PVCs on class `longhorn`, 17
volumes labelled no-backup, intersection exactly the eight — then confirmed idempotent on a re-run.

**`--tags longhorn_backup` cannot complete until the cap resets.** It renders the health script, but
it sits behind *Check whether Longhorn can reach the backup target*, which polls
`backuptarget/default .status.available` and gives up after 12 attempts — `false`, because B2 is
still refusing. The gate is right and should not be worked around; it is the same signal
`k3s Longhorn Backup` is pushing (`backup target unavailable: failed to list backup volumes in
s3://…`). The consequence is that the **deployed script still carries `budget 10`** while the group
is back to 15, so the monitor stays red on an expected condition.

**After 00:00 UTC, before 03:30 UTC** (the `daily-backup` cron is `30 3 * * *`, so there is a
three-and-a-half hour window):

1. Re-check the storm against the 192/min baseline above.
2. `uv run ansible-playbook ansible/k3s-bringup.yml --tags longhorn_backup -e target=daniel-box` —
   now that the target is reachable it gets past the gate and deploys `budget 17`.
3. After 03:30, read **B2 Reachable**. Green means 15 volumes/day fits and the exclusion was
   avoidable; `transaction_cap_exceeded` again means incrementals are not the cheap part, the eight
   names go back into `k3s_longhorn_nobackup_volumes`, and the answer is a staggered weekly group.

### Both error loops quieted at 20:00 UTC — and one of them may be why the cap looked self-sustaining

Reviewing every red monitor turned up a second consumer nobody was counting. **kopia was
crash-looping**: 275 restarts, ~58 connect attempts an hour, each one a B2 call that 403s. Its
wrapper also logs `Repository not found in bucket, creating...` on every pass — it infers "absent"
from a read that merely *failed*, which is a bad inference to leave running against a bucket holding
the only backups. (`kopia repository create` refuses a bucket that already has a format blob, so
this is a hazard to note, not damage taken.)

Together with Longhorn's ~192 failed list calls/min, that is on the order of 11,500 calls an hour
against a **2500/day** cap. If B2 counts a 403'd call as a transaction — unverified, and the reason
to act rather than a thing established here — the reset allowance is gone within minutes of 00:00
UTC every day, before the 03:30 run, and the outage sustains itself. Both were doing zero useful
work: neither can back up while the cap is on.

**And blanking the target is the lever the poll interval was not.**

```
k3s kubectl -n longhorn-system patch settings.longhorn.io backup-target --type=merge -p '{"value":""}'
```

**192/min to 0 in 75 seconds.** The controller has nothing to poll, so the error requeue has nothing
to requeue on — which is consistent with the earlier finding that `backupstore-poll-interval: 0`
changed nothing, since that treats a timer that was never driving the loop. Run it through Ansible
with `become`; the session kubeconfig is read-only and cannot patch. `docker stop kopia` handled the
other one.

**Revised sequence, after 00:00 UTC and before 03:30 UTC:**

1. `uv run ansible-playbook ansible/k3s-bringup.yml --tags longhorn_backup -e target=daniel-box` —
   restores the backup target *and* deploys `budget 17`, now that the availability gate can pass.
2. `ssh daniel-server docker start kopia` — it should connect on the first try rather than loop.
3. Re-check the storm; it should stay at 0 now that reconciles succeed.
4. After 03:30, read **B2 Reachable**. Green means 15 volumes/day fits. Red means the eight names go
   back into `k3s_longhorn_nobackup_volumes` and the answer is a staggered weekly group.

Note that while the target is blank Longhorn lists no backups — the B2 data is untouched, and step 1
re-syncs it.

## Verdict — 2026-08-09, the first post-reset daily run

**Green, decisively.** The revised sequence ran in the window (target restored 00:01, kopia
connected first try, storm 0/min throughout), and the 03:30 daily-backup completed **17/17
volumes in four minutes** (03:34–03:38) with the backup target `available: true` and zero
`transaction_cap_exceeded` — the set even included the two Longhorn volumes slice-5 added that
same night (zigbee2mqtt, home-assistant). Incrementals ARE the cheap part: the 08-08 cap hit
was the PVC-migration full-upload day plus the 403 retry storm feeding itself, not the daily
volume count. The eight config volumes stay in the backup set, `k3s_longhorn_nobackup_volumes`
stays empty, and slice-5's D6 resolves to `longhorn` for the z2m/HA claims (mosquitto stays
nobackup on its own merits — regenerable retained state).

## Recurrence — 2026-08-09, cap blown again by ~05:49 UTC

The verdict above held for the *daily-backup run* but not for the *day*: `b2_reachable` flipped
DOWN at 05:49 UTC (00:49 local) with `transaction_cap_exceeded` — about two hours after the
03:38 green run — and stayed down. So something in the 03:38–05:49 window consumed the rest of
the daily cap; the Longhorn daily itself is exonerated (17/17, zero cap errors). Kopia's
nightly window on daniel-server is the prime suspect, but per-class usage can't be read while
capped (the B2 API itself 403s) — pull the Caps & Alerts page, or tomorrow's 02:30
`kopia-b2-usage` probe, before concluding.

Containment applied (same shape as 08-08): `docker stop kopia` on daniel-server at 12:49 UTC
(its connect loop misreads the 403 as "repository not found" and retries creation — creation
also 403s, so no damage, but it's a steady burn), and the Longhorn backup target needs
blanking to kill the ~150/min reconcile storm. Restore after 00:00 UTC with the revised
sequence above.

Open question for the next window: what does kopia's nightly actually cost in class B/C now
that Longhorn shares the account cap, and does the cap need raising for the two to coexist —
the 08-08 analysis priced the *Longhorn* side only.

## Recurrence — 2026-08-10, Class B AND Class C caps blown (operator emails confirm both)

Timeline: the Longhorn backup target went unavailable at 05:01 UTC and the reconcile storm
ran unattended — an errored `backups.longhorn.io` object every ~7.5 min until containment.
`b2_reachable` 403'd all morning; kopia's API hung on the 403s (~04:00 UTC on), taking the
Backup Freshness check down with it. New load that night, and the likely first-cause: the
Phase D Kuma cutover enrolled TWO first-backup volumes into the `longhorn` backed-up class
(uptime-kuma-data 4 Gi seeded at 1.5 G, code-server-config 10 Gi seeded at 6.2 G) — a
full-upload night on top of kopia's nightly, before the storm amplified it. Class B blowing
too is new and unexplained by lists alone; per-class attribution still can't be read while
capped (the open question below stands, now for both classes).

Containment (12:47–12:55 UTC): backup target blanked (`backupTargetURL: ""` — the 08-08
lever), kopia stopped AND `docker rm`'d — its connect loop misreads the 403 as "repository
not found" and retries creation at a steady burn, and a removed container leaves the
docker-fleet check's restart-policy scope instead of paging red for 11 hours.

Re-arm after 00:00 UTC: `deploy.yml --tags kopia` on daniel-server (recreates the
container), then re-set `backupTargetURL` to `s3://daniel-server-kopia@us-east-005/longhorn`
and confirm the target goes `available: true` without a 403 before walking away.

Operator decision now genuinely due (third cap event in nine days): raise the B2 daily caps
so Longhorn + kopia can coexist on the account, or thin/serialise the backup set. Enrolment
of every migrated PVC into `longhorn` is additive by design — the cap is not.

## Recurrence — 2026-08-11 (fourth cap event)

The 00:00 reset briefly worked as planned: the (accidentally re-armed, all-day-failing)
target's storm started *succeeding* at reset, first clean sync 02:01, heartbeat green,
board fully green by 02:30. Then the 03:30 nightly ran and puts began 403ing partway
through — 11 of 12 volumes Completed, the rest went into Longhorn's per-volume retry loop
(54 Error CRs by 05:40), which pins the cap exactly like the original storm.
`b2_reachable` DOWN from 05:11.

Two candidate explanations, distinguishable tomorrow: (a) the 00:00–02:01 storm tail
burned the fresh cap before the nightly (its window shrinks to zero with a clean re-arm),
or (b) the nightly's own transaction volume no longer fits the cap even alone — kopia is
retired now, so the nightly is the only B2 load left, and a clean-cap failure tomorrow
means the cap is simply too small (operator decision from the 08-10 entry stands).

Containment 05:45 UTC: target blanked again (same lever). No kopia to stop this time.

Re-arm after the next 00:00 UTC reset: set `backupTargetURL` back to
`s3://daniel-server-kopia@us-east-005/longhorn`, confirm `available: true`, and watch the
03:30 nightly with a clean cap. The kopia B2 deletion (approved, no retention window)
stays gated on ONE fully-green nightly — note the deletion itself spends class A/B/C
transactions on the shared cap, so run it after the nightly verifies, not before.
