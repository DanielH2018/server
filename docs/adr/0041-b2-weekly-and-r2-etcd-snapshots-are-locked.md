---
id: "0041"
title: The B2 weekly bucket and the R2 etcd snapshots are locked, and the R2 daily tier is not
status: Accepted
date: 2026-11-20
governs: []
---

# ADR-0041: The B2 weekly bucket and the R2 etcd snapshots are locked, and the R2 daily tier is not

## Status

Accepted. The operator ruled on 2026-11-20, on
[#8511](https://github.com/DanielH2018/server/issues/8511), with the B2 half resting on a
premise the source reading could not confirm: that B2 accepts Longhorn's version-less delete
of a locked version. Step 1 of
[Bucket immutability](../longhorn-backup-tiering.md#bucket-immutability-adr-0041) ran a real
Longhorn backup and backup deletion against a lock-enabled scratch bucket on 2026-11-21
([#9699](https://github.com/DanielH2018/server/issues/9699#issuecomment-7065073692)). The
premise held, but the step also showed that the first draft's bucket default retention fails
every Longhorn upload. This record was revised on 2026-11-22 to the shape that step proved:
file lock with no default retention, and a daily sweep that sets each object's retention. The
bucket settings are operator steps, because no session holds a credential that can change a
bucket, and [#9699](https://github.com/DanielH2018/server/issues/9699) tracks them. On
2026-11-23 the operator chose a canary as the R2 rule's standing reader
([#9702](https://github.com/DanielH2018/server/issues/9702)), and the *Decision* records it.

## Context

Longhorn writes to both backup buckets, and k3s writes the etcd snapshots to R2. The
cluster therefore holds credentials that can delete every object in both buckets. Before
this record, nothing stopped such a delete: no bucket had Object Lock, a lock rule or a
writer key without delete rights. A leaked key, or a bug in a prune, could leave no copy to
restore from.

Three shapes were on the table:

1. B2 Object Lock on the weekly bucket. The first draft set a default retention shorter than
   the weekly prune age; the revision sets none and locks each object from a daily sweep.
2. An R2 bucket lock rule over the etcd snapshot prefix.
3. A writer key without delete rights, with a bucket lifecycle rule doing the pruning.

The question that decided between them was whether a Longhorn prune tolerates an object it
cannot delete. A prune that fails retries, and a retry storm against B2 is a known cost
([ADR-0007](0007-backup-tiering-r2-daily-b2-weekly.md)).

**The spike, from the source.** Longhorn v1.13.0 pins backupstore `57b3845ad4ef`. Its S3
driver deletes each object with one `DeleteObject` call that carries a `Key` and no
`VersionId` (`s3/s3_service.go`, `DeleteObjects`), and any failed delete fails the whole
removal. The two stores differ in what that call does to a locked object:

- **B2 keeps versions.** On a versioned store, a `DeleteObject` without a `VersionId` writes
  a hide marker and leaves the version in place. Under S3 Object Lock semantics a hide is
  allowed while the version is locked. Backblaze's own pages do not state the B2 behaviour,
  so this half is unconfirmed until the live spike runs.
- **R2 keeps no versions.** Cloudflare documents that a delete blocked by a lock rule fails
  with HTTP 409 `ObjectLockedByBucketPolicy`. A locked Longhorn block on R2 therefore fails
  the prune, and the prune retries.

The stake is larger than the prune. Every backup writes a `locks/*.lck` object and removes
it when the backup ends (`lock.go`, `Lock` and `Unlock`), seconds after the upload, so the
removal always meets a locked version. If B2 refused a version-less delete of a locked
version, every weekly backup would fail, not only the prune.

**The live spike.** Step 1 confirmed the B2 premise: with a governance retention on every
object, a backup deletion and a backup-volume deletion each left the locked versions in place
under hide markers, and `longhorn-manager` logged no `failed to delete objects`. It also
refuted the first draft. With a governance default retention on the bucket, the first backup
failed at its lock file:

```text
AWS Error: InvalidRequest Content-MD5 OR x-amz-checksum- HTTP header is required for Put Object requests with Object Lock parameters
```

B2 treats every upload into a bucket with a default retention as carrying Object Lock
parameters, and Longhorn's S3 client sends neither header. A default retention on the weekly
bucket would therefore fail every weekly backup before any block uploads. With file lock on
and the default set to none, metadata and data backups completed.

k3s prunes its S3 snapshots by count (`etcd-snapshot-retention` in
`hosts/components/k3s-server/component.yml`), and deletes only the snapshots past that
count. The scheduled snapshots alone reach about 14 days before a prune, so a 7-day lock
never meets a scheduled prune. An on-demand `k3s etcd-snapshot save --etcd-s3` counts toward
the same retention. More than 14 of them in a week would bring a prune inside the lock, and
R2 would refuse that delete until the lock expires.

## Decision

The B2 bucket that holds the weekly tier has file lock enabled (`--file-lock-enabled`) and
**no default retention**. The key Longhorn writes with must not hold `bypassGovernance`, so
governance mode binds it, and the operator's master key keeps the one way to remove a
mistaken lock.

A daily sweep
(`scripts/backup/b2_extend_retention.py`, run by the `homelab-b2-retention` timer on
daniel-box) sets the governance retention of every current Longhorn object under
`longhorn/backupstore/volumes/` to now plus 7 days
(`scripts/backup/b2_extend_retention.py:RETENTION_DAYS`). The period is the sweep's own,
because the bucket carries none. That locks a new block by the next run, and keeps every
block a retained backup references locked, however old its upload. The period is shorter
than the age at which the weekly prune deletes a backup (`k3s_longhorn_weekly_backup_retain`
weekly backups), so a pruned block's hidden version stays billed for at most 7 days. The sweep reads the Longhorn key, which
holds `writeFileRetentions`: the operator chose it over a separate sweep key on 2026-11-22
([#9787](https://github.com/DanielH2018/server/issues/9787)), and the *Consequences* below
weigh the two. Longhorn's own `full-backup-interval` would
renew each lock by re-uploading every block, and was rejected: the replaced versions stay
billed until the lifecycle rule removes them, so the bucket would hold about two copies of the
referenced set. The bucket held 6.25 GB of B2's 10 GB free tier on 2026-11-21.

The R2 bucket has one lock rule: an `Age` rule of 7 days over `etcd-snapshots/`. No rule
covers `longhorn/`, the R2 daily tier, because R2 refuses a locked delete and Longhorn's
prune would retry it.

No bucket uses a writer key without delete rights. Longhorn's prune would fail on every
delete and retry.

monitor-bridge's `b2_object_lock` row
(`k8s/roles/monitor-bridge/files/checks/b2.py:check_b2_object_lock`) reads the B2 setting
once a day. It pages a warning when the bucket departs from this record: the lock off or
unreadable, any default retention, or a writer key that holds `bypassGovernance`. The row reads
the bucket, not the sweep. Checks 9 and 10 of `longhorn-backup-health` read the sweep's
journal on daniel-box, which the monitor-bridge pod cannot reach, so the row does not repeat
them.

monitor-bridge's `r2_etcd_lock` row
(`k8s/roles/monitor-bridge/files/checks/r2.py:check_r2_etcd_lock`) proves the R2 rule by its
effect once a day. R2's S3 API implements no lock operation, so the cluster's R2 key can
neither read nor change a rule. Reading one takes a Cloudflare API token, and Cloudflare
scopes the permission that reads lock rules only to a whole account, never to one bucket. The
operator ruled on 2026-11-23
([#9702](https://github.com/DanielH2018/server/issues/9702)) for a canary written with the R2
key the cluster already holds, over giving a pod or host an account-wide token. Each day the
row uploads a zero-byte `etcd-snapshots/lock-canary-<date>` and tries to delete an earlier
canary that is younger than the lock. R2's 409 `ObjectLockedByBucketPolicy` passes the row,
and a delete that succeeds fails it. The row then deletes the canaries older than 7 days, and
a refusal there fails it too. It pages a warning, `R2EtcdLockNotHolding`.

## Consequences

**A new block is unlocked until the next sweep.** Nothing locks a version at upload, so a block
is open to deletion from its upload until the next 07:30 UTC sweep, up to one day. A deletion
in that window removes nothing for good: Longhorn's delete only writes a hide marker, and the
lifecycle rule keeps a hidden version for 7 days.

**Nothing is locked until the sweep holds a key that can extend retention.** A green
`b2_object_lock` row means the bucket is set up, not that any object is locked. Step 2b of the
runbook, which gives the Longhorn key `writeFileRetentions`, makes the lock real.

**The sweep keeps old blocks locked.** B2 counts a version's retention from its upload, and a
Longhorn backup uploads only the blocks that changed, so the base blocks every weekly backup
references are months old. The daily sweep renews each one, so a referenced block is always
locked for at least 6 of the next 7 days. A pruned block
leaves the listing when Longhorn hides it, so its lock lapses within one period, and the
lifecycle rule then removes it as before. The sweep costs about four Class C calls a day,
because each retention update is Class A, which B2 does not meter.

**A deletion with the Longhorn key still hides a locked block.** The lock keeps the version
but not the name, so a restore after such a deletion needs the operator to delete the hide
markers first. A hide marker carries no retention, so any key with `deleteFiles` can delete
one, and the locked version becomes current again. The lock makes the deletion recoverable; it does not leave
the backups restorable untouched.

**`writeFileRetentions` lets a leaked key lock objects for good.** Backblaze documents the
capability as setting a retention's mode and expiration. Backblaze also states that no user
can remove compliance-mode retention, the master key included, and names closing the account as the
only way out. A holder of the Longhorn key could therefore lock objects in compliance mode for
up to 3,000 days and keep them billed for that long. Switching an existing governance
retention to compliance takes `bypassGovernance`, so the exposure is mainly objects whose lock
has lapsed. The bound is the bucket's stored bytes plus whatever the key uploads, which a B2
storage cap limits if the account sets one. The rejected alternative was a separate sweep key with only
`listBuckets`, `listFiles`, `readBucketRetentions` and `writeFileRetentions`, held in SOPS for
the host and never rendered into a cluster Secret, so a leaked cluster credential would not
carry the capability. It needed a code change to point the sweep at new SOPS keys and a second
key to rotate. The operator accepted the exposure on 2026-11-22
([#9787](https://github.com/DanielH2018/server/issues/9787)) in exchange for one key, one
rotation and a sweep that locks from the next run.

**The sweep depends on its timer.** If it stops, every lock lapses within 7 days, and no new
block is locked at all. Checks 9 and 10 of `longhorn-backup-health` read its journal
tag `b2-retention`, and report `down` on a refused extension or on a day with no completed run.

**The etcd snapshots are whole files**, so each locked snapshot restores on its own. The
lock covers every snapshot younger than 7 days.

**The R2 daily tier stays open to deletion.** The five daily volumes have no off-site copy but
R2's. Locking it waits on a Longhorn prune that tolerates a refused delete.

**A drain stops at a locked version.** `scripts/backup/b2_drain.py` deletes
versions directly and stops the run on any refusal other than `file_not_present`. A stray
prefix younger than the retention cannot be drained until its lock expires.

**File lock on a B2 bucket cannot be turned off.** The bucket stays lock-enabled for good.
A default retention must never be added to it, because that fails every weekly backup; the
`b2_object_lock` row pages if one appears.

**The R2 canary sees a missing or lengthened rule, not a widened one.** A deleted or
disabled rule fails the `r2_etcd_lock` row within a day, and so does a lock over the prefix
that outlasts 7 days. A rule widened over `longhorn/` passes it, because the canary writes
only under `etcd-snapshots/`. Such a rule makes every daily Longhorn prune retry against a
409, and the `r2_usage` row notices only once the retries have spent most of the month's
operations.

**monitor-bridge holds the R2 write key and writes to the backup bucket.** The canary needs
the key Longhorn and k3s write with, which can also delete the unlocked `longhorn/` daily
tier. A leaked monitor-bridge credential therefore reaches every R2 object the lock does not
cover. The operator accepted this over an account-wide Cloudflare token, which could read and
change every bucket's rules. Each canary is zero bytes, because k3s lists every non-empty
object under `etcd-snapshots/` as a ready snapshot, and a non-empty canary would hide a stale
snapshot from backup health's check 12. No canary name matches `offbox-*`, so k3s's
retention never counts one.

**The B2 row reads red from deploy until the operator enables file lock**, and fires a warning. A
warning that holds for 48 hours escalates to a page through `WarningUnresolved`.

## Governs

No single line. The settings live in the two buckets, outside the tree. The `b2_object_lock`
row reads the B2 one, the `r2_etcd_lock` row proves the R2 one, and
`k8s/roles/monitor-bridge/tests/test_check_bucket_locks.py` holds both verdicts to this
record.
