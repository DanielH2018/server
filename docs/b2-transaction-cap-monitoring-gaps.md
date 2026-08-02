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

3. **Track the transaction dimension, not just bytes** (G2). Whether B2's API exposes cap headroom
   cheaply needs checking; if not, the `B2 Reachable` gate in (1) is the practical substitute,
   since a cap breach surfaces as a 403 on the next call.

## Not established

- **What consumed the transactions.** 2026-08-02 saw Longhorn's 03:30 daily backup of two volumes
  plus repeated PVC create/delete cycles during the Authelia and Traefik work, each of which makes
  Longhorn re-list the backup store. That is a plausible contributor, not a measured one — the
  split needs the B2 console. Deliberately not investigated further from this side to avoid
  spending more of the capped budget.
- **Whether the cap is daily-resetting or account-lifetime.** Determines whether this self-clears.
- **Whether Kopia recovers on its own** once the cap lifts, given it is now panicking on the
  refresh path. It may need a restart.
