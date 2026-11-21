---
id: "0041"
title: The B2 weekly bucket and the R2 etcd snapshots are locked, and the R2 daily tier is not
status: Proposed
date: 2026-11-20
governs: []
---

# ADR-0041: The B2 weekly bucket and the R2 etcd snapshots are locked, and the R2 daily tier is not

## Status

Proposed. The operator ruled on 2026-11-20, on
[#8511](https://github.com/DanielH2018/server/issues/8511). The B2 half rests on a premise
the source reading below cannot confirm: that B2 accepts Longhorn's version-less delete of a
locked version. Step 1 of
[Bucket immutability](../longhorn-backup-tiering.md#bucket-immutability-adr-0041) runs a real
Longhorn backup and backup deletion against a lock-enabled scratch bucket. This record becomes
Accepted when that step passes, and is revisited if it fails. The bucket settings are
operator steps, because no session holds a credential that can change a bucket, and
[#9699](https://github.com/DanielH2018/server/issues/9699) tracks them.

## Context

Longhorn writes to both backup buckets, and k3s writes the etcd snapshots to R2. The
cluster therefore holds credentials that can delete every object in both buckets. Before
this record, nothing stopped such a delete: no bucket had Object Lock, a lock rule or a
writer key without delete rights. A leaked key, or a bug in a prune, could leave no copy to
restore from.

Three shapes were on the table:

1. B2 Object Lock on the weekly bucket, with a default retention shorter than the weekly
   prune age.
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

k3s prunes its S3 snapshots by count (`etcd-snapshot-retention` in
`hosts/components/k3s-server/component.yml`), and deletes only the snapshots past that
count. The scheduled snapshots alone reach about 14 days before a prune, so a 7-day lock
never meets a scheduled prune. An on-demand `k3s etcd-snapshot save --etcd-s3` counts toward
the same retention. More than 14 of them in a week would bring a prune inside the lock, and
R2 would refuse that delete until the lock expires.

## Decision

The B2 bucket that holds the weekly tier has Object Lock enabled, with a governance-mode
default retention of 7 days. That is shorter than the age at which the weekly prune deletes
a backup (`k3s_longhorn_weekly_backup_retain` weekly backups). The key Longhorn writes with
must not hold `bypassGovernance`, so governance mode binds it, and the operator's master key
keeps the one way to remove a mistaken lock.

The same key holds `writeFileRetentions`. A daily sweep
(`scripts/backup/b2_extend_retention.py`, run by the `homelab-b2-retention` timer on
daniel-box) sets the retention of every current Longhorn object under
`longhorn/backupstore/volumes/` to now plus the bucket's default period. That keeps every
block a retained backup references locked, however old its upload. A key without
`bypassGovernance` can extend a governance retention but never shorten it, so the capability
gives a leaked key no way to unlock anything. Longhorn's own `full-backup-interval` would
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
once a day. It pages a warning when the bucket departs from this record: the lock off, no
default retention, compliance mode, a retention as long as the weekly prune age, or a writer
key that holds `bypassGovernance` or lacks `writeFileRetentions`.

The R2 rule needs a standing reader too, and this record does not yet have one. Until it
lands, the operator verifies the rule by hand with `wrangler r2 bucket lock list` after
setting it. R2's S3 API implements no lock operation, so the cluster's R2 key can neither
read nor change a rule. Reading one takes a Cloudflare account API token, and Cloudflare
scopes the permission that reads lock rules only to a whole account, never to one bucket. No
such token exists in the tree, and only the operator can mint one, so the reader cannot ship
with this record. [#9702](https://github.com/DanielH2018/server/issues/9702) holds it and the
operator's choice of where it runs.

## Consequences

**The sweep keeps old blocks locked.** B2 counts a version's retention from its upload, and a
Longhorn backup uploads only the blocks that changed, so the base blocks every weekly backup
references would otherwise be months old and unlocked. The daily sweep renews each one, so a
referenced block is always locked for at least the default period less a day. A pruned block
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
storage cap limits if the account sets one. The alternative is a separate sweep key with only
`listBuckets`, `listFiles`, `readBucketRetentions` and `writeFileRetentions`. It would be held
in SOPS for the host and never rendered into a cluster Secret, so a leaked cluster credential
would not carry the capability. The sweep reads the Longhorn key, and
[#9787](https://github.com/DanielH2018/server/issues/9787) leaves the choice to the operator at
mint time. Choosing the separate key means pointing the sweep at the new SOPS keys and
dropping the `writeFileRetentions` condition from the `b2_object_lock` row.

**The sweep depends on its timer.** If it stops, the locks on blocks older than the default
period lapse within one period. Checks 9 and 10 of `longhorn-backup-health` read its journal
tag `b2-retention`, and report `down` on a refused extension or on a day with no completed run.

**The etcd snapshots are whole files**, so each locked snapshot restores on its own. The
lock covers every snapshot younger than 7 days.

**The R2 daily tier stays open to deletion.** The five daily volumes have no off-site copy but
R2's. Locking it waits on a Longhorn prune that tolerates a refused delete.

**A drain stops at a locked version.** `scripts/backup/b2_drain.py` deletes
versions directly and stops the run on any refusal other than `file_not_present`. A stray
prefix younger than the retention cannot be drained until its lock expires.

**Object Lock on a B2 bucket cannot be turned off.** The default retention can be changed
or removed later, but the bucket stays lock-enabled.

**Until #9702 lands, nothing pages on an edit to the R2 rule.** A deleted or disabled rule
removes the etcd snapshots' lock silently. A rule widened over `longhorn/` makes every daily
Longhorn prune retry against a 409. The `r2_usage` row would notice that only once the
retries had spent most of the month's operations.

**The B2 row reads red from deploy until the operator acts**, and fires a warning. A
warning that holds for 48 hours escalates to a page through `WarningUnresolved`.

## Governs

No single line. The settings live in the two buckets, outside the tree. The `b2_object_lock`
row reads the B2 one, and `k8s/roles/monitor-bridge/tests/test_check_bucket_locks.py` holds
its verdict to this record.
