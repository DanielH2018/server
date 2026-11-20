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
[#8511](https://github.com/DanielH2018/server/issues/8511). The bucket settings themselves
are operator steps, because no session holds a credential that can change a bucket. The
steps and the live half of the prune spike are in
[Bucket immutability](../longhorn-backup-tiering.md#bucket-immutability-adr-0041).

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

k3s prunes its S3 snapshots by count (`etcd-snapshot-retention` in
`hosts/components/k3s-server/component.yml`), and deletes only the snapshots past that
count. A lock shorter than the age those snapshots reach never meets a k3s delete.

## Decision

The B2 bucket that holds the weekly tier has Object Lock enabled, with a governance-mode
default retention of 7 days. That is shorter than the age at which the weekly prune deletes
a backup (`k3s_longhorn_weekly_backup_retain` weekly backups). The key Longhorn writes with
must not hold `bypassGovernance`, so governance mode binds it, and the operator's master key
keeps the one way to remove a mistaken lock.

The R2 bucket has one lock rule: an `Age` rule of 7 days over `etcd-snapshots/`. No rule
covers `longhorn/`, the R2 daily tier, because R2 refuses a locked delete and Longhorn's
prune would retry it.

No bucket uses a writer key without delete rights. Longhorn's prune would fail on every
delete and retry.

monitor-bridge reads both settings once a day: `b2_object_lock`
(`k8s/roles/monitor-bridge/files/checks/b2.py:b2_object_lock`) and `r2_etcd_lock`
(`k8s/roles/monitor-bridge/files/checks/r2.py:r2_etcd_lock`). Each pages a warning when
its bucket departs from this record.

## Consequences

**The B2 lock protects only what was uploaded in the last 7 days.** B2 counts a version's
retention from its upload. A Longhorn backup uploads only the blocks that changed, so the
base blocks every weekly backup still references are months old and unlocked. A holder of
the Longhorn key can delete those blocks, and every backup that references them can no
longer be restored, even though its `.cfg` is locked. The lock defends against a prune bug and
against a deletion of recent backups, not against a deliberate deletion of old blocks. The
follow-up that would close this gap is
[#9685](https://github.com/DanielH2018/server/issues/9685), filed and not decided.

**The etcd snapshots are whole files**, so each locked snapshot restores on its own. The
lock covers every snapshot younger than 7 days.

**The R2 daily tier stays open to deletion.** The five daily volumes have no off-site copy but
R2's. Locking it waits on a Longhorn prune that tolerates a refused delete.

**A drain stops at a locked version.** `scripts/backup/b2_drain.py` deletes
versions directly and stops the run on any refusal other than `file_not_present`. A stray
prefix younger than the retention cannot be drained until its lock expires.

**Object Lock on a B2 bucket cannot be turned off.** The default retention can be changed
or removed later, but the bucket stays lock-enabled.

**The Cloudflare analytics token gains Workers R2 Storage: Read.** The R2 lock read needs
it, and the token is the bridge's one Cloudflare credential. The permission cannot change a
bucket or a rule. Whether it also lets the REST API read object contents is not settled by
Cloudflare's documentation. If it does, the bridge can read the etcd snapshots, which hold
every cluster Secret.

**Both rows read red from deploy until the operator acts**, and each fires a warning. A
warning that holds for 48 hours escalates to a page through `WarningUnresolved`.

## Governs

No single line. The settings live in the two buckets, outside the tree. The two
monitor-bridge rows read them, and `k8s/roles/monitor-bridge/tests/test_check_bucket_locks.py`
holds the rows' verdicts to this record.
