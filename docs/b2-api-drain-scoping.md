# Deleting Longhorn backups through the B2 API — scoping

**Recommendation: build it**, on the transaction-cost argument alone. Measured 2026-08-19/20
against the live bucket, read-only apart from one chain deletion that ran on its own schedule.

Two findings carry the decision:

1. **Deletion through the B2 API is effectively free.** Enumerating the *entire* backupstore —
   28 volume prefixes, ~3,400 live objects, 2.64 GiB — costs **5 Class C calls**. Deletes are
   Class A, which B2 does not meter. Deleting one volume's chain through Longhorn costs on the
   order of several hundred Class C, because a prune walks the whole block tree once per
   deleted backup.
2. **Blocks live under each volume's own prefix**, so deleting a prefix is safe. Details below.

## Retracted: Longhorn does not leave residue

An earlier draft of this document claimed that deleting a chain through Longhorn strands about
half the objects in B2, and argued the API route was worth building for that reason alone.
**That was wrong, and the error was in how I counted.**

`b2_list_file_versions` returns every version of every object. On a versioned bucket, deleting a
file writes a hide marker and keeps the upload version underneath — so a correctly deleted
object still comes back with `action == "upload"`. I counted those as live. Five volumes whose
chains Longhorn had already deleted appeared to hold 286 live objects; counting each file's
*current* state instead, they hold **one object each — `volume.cfg` — and zero blocks.**

Two things confirm the deletion path is working correctly:

- **Watching one run live.** `sonarr-config`'s chain was deleted at 00:06 UTC. Its block count
  went 200 → 110 over the following five minutes, with hide markers climbing in step. Longhorn
  deletes blocks; it just does so gradually.
- **The retained versions clear themselves.** The `daniel-server-kopia` bucket carries a
  lifecycle rule `daysFromHidingToDeleting: 7` across the whole bucket, so hidden versions are
  removed automatically after a week at no transaction cost.

So a prefix that still lists objects, and still bills for storage, right after a successful
delete is expected. It is not evidence of a leak.

What this changes: the route is worth building for its Class C cost and nothing else. That
argument is measured and still large.

## What was measured

Survey method: one `b2_authorize_account`, then `b2_list_file_versions` paginated over the
prefix `longhorn/backupstore/volumes/`. Versions come back newest-first per file name, so the
first version seen for a name is its current state.

### The seven stale prefixes

A prefix is stale when its `BackupVolume` CR exists but the Longhorn `Volume` does not — the
seven volumes rebuilt at 16 MiB blocks. There are 32 `BackupVolume` CRs against 43 live volumes.

Snapshot at 00:11 UTC, with `sonarr-config` mid-deletion:

| Prefix | Service | Live | Blocks | Deleted | Retained versions | Stored |
|---|---|---|---|---|---|---|
| `pvc-9432817d` | prowlarr-config | 219 | 216 | 2 | 6 | 50.4 MiB |
| `pvc-cc4ab76b` | sonarr-config | 112 | 110 | 94 | 100 | 64.1 MiB |
| `pvc-8e41a06c` | — | 1 | 0 | 67 | 89 | 1.1 MiB |
| `pvc-de7f9d60` | — | 1 | 0 | 58 | 67 | 10.8 MiB |
| `pvc-c926b73e` | — | 1 | 0 | 33 | 39 | 0.5 MiB |
| `pvc-d06c0d9d` | — | 1 | 0 | 31 | 56 | 0.2 MiB |
| `pvc-36a38101` | — | 1 | 0 | 17 | 30 | 0.1 MiB |

The five one-object rows are drained volumes. Their single remaining object is `volume.cfg`;
their storage is retained versions awaiting the 7-day lifecycle sweep.

`prowlarr-config` is the only chain still intact.

### The bucket layout, and why prefix deletion is safe

```
longhorn/backupstore/volumes/<xx>/<yy>/<volume-name>/
    volume.cfg
    backups/backup_<id>.cfg
    blocks/<aa>/<bb>/<sha256>.blk
```

**Blocks live under each volume's own prefix.** The same content hash appears as separate
objects under different volumes — `7e208b53….blk` exists under both `pvc-a36be217` and
`pvc-2e468900`. Deduplication is within a volume, never across volumes.

This is the property the design rests on: deleting everything under one volume's prefix cannot
remove a block another volume's backup depends on.

### Credentials

The existing SOPS key `kopia_b2_key_id` / `kopia_b2_application_key` already carries
`deleteFiles`. No new credential, no `secret_rotation.py sync`.

It also carries `writeBuckets`, `writeBucketLifecycleRules` and `writeBucketReplications` — far
broader than either Longhorn or a drain tool needs. Narrowing it is out of scope here but
belongs on the list.

## Cost

| | Longhorn | B2 API |
|---|---|---|
| Enumerate | — | shared: 5 Class C for the whole store |
| Delete | ~1.28 Class C per stored block, per deleted backup | Class A, unmetered |
| Reconcile | — | one backup-target sync |

For the 14 volumes still at 2 MiB, deleting the old chains through Longhorn is on the order of
**thousands of Class C**. Through the API it is one enumeration (5), one verification re-list
per volume (14), plus the target sync — call it **20–50 Class C** in total.

This does **not** make the remaining migration free, and an earlier claim of mine said it would.
The measured ~180 Class C per volume of *migration* cost — volume deletion, target syncs, the
first backup at 16 MiB — is Longhorn-side and unaffected. Fourteen volumes is still roughly
2,500 Class C, so about **two days**, not one session. The API route removes the deletion half.

## Design

A host-side script plus a thin Ansible play to supply the SOPS credentials.

1. **Enumerate** `longhorn/backupstore/volumes/` once, group keys by volume.
2. **Classify** a prefix as drainable only when its volume name is absent from
   `kubectl get volumes.longhorn.io`, using the same fail-closed guard as
   `drop_migrated_backup_chain.yml`: if the live-volume list is empty or unreadable, refuse
   everything rather than classify everything as an orphan.
3. **Require an explicit allow-list** of volume names on the command line. Discovery proposes;
   the operator disposes. Dry run is the default.
4. **Delete every version**, including hide markers. A drain cannot wait out the 7-day lifecycle
   rule, so it removes the retained versions itself.
5. **Re-list the prefix and assert it is empty**, counting current state rather than versions —
   the mistake this document already made once. The 2026-08-19 drain reported "1,676/1,676
   deleted" and left five volumes over retention for the same family of reason.
6. **Force one backup-target sync** (`backuptarget.spec.syncRequestedAt`) so Longhorn drops the
   now-dangling `Backup` and `BackupVolume` CRs.

### Reuse

`ansible/roles/k8s/monitor-bridge/files/check.py` already has a tested B2 client —
`b2_authorize_data`, `b2_storage_api` (tolerant of the v1/v2/v3 response shapes),
`b2_list_versions` (already the versions call, already paginating on
`nextFileName`/`nextFileId`), and `b2_sum_versions`, with tests in `test_check.py`.

Import it, or copy it? **Copy the roughly 60 lines.** `check.py` is 3,109 lines and evaluates
~60 environment-derived constants at import time; pulling a monitor's entire configuration
surface into a CLI tool to reuse four functions is the worse trade. The alternative — extract
the client into a sibling module on `pythonpath`, following the `roles/setup/common/files`
precedent — is cleaner but means editing a deployed monitor for the benefit of a cleanup tool.
Take that path if a third consumer ever appears.

The one change either way: `b2_list_versions` needs a `prefix` parameter.

## What is not verified

- **The `syncRequestedAt` reconcile.** Documented in this repo's own reaper header; not
  exercised. If it does not drop the dangling CRs, they need removing another way.
- **The sync's own cost.** A sync must walk every volume directory, so estimate tens of Class C
  per drain run — small, but not zero, and not measured.
- **What deletes `volume.cfg`.** Each drained prefix keeps one. The likely answer is deleting
  the `BackupVolume` CR, which `drop_migrated_backup_chain.yml` never does — all seven stale CRs
  are still standing. One object per volume, so this is tidiness, not cost.
- **B2's Class C billing granularity for large pages.** B2 bills listing per 1,000 names
  returned, so the 5-call figure is 5 billed units either way.

## Risks

- **Irreversible against the backup store.** Mitigated by dry-run default, the explicit
  allow-list, the fail-closed live-volume guard, and refusing any prefix whose volume exists.
- **New code, unsupervised, against the only offsite copy.** The guards belong in tests before
  the first real run, matching the pattern already used for the migration playbooks.
- **Deleting underneath Longhorn leaves dangling CRs** until the sync runs. Cosmetic, but it
  will make monitoring read wrong in the interim.

## Suggested first target

`prowlarr-config` — the only chain still intact, and the only remaining case where the API route
can be compared directly against the Longhorn path. Its replacement is backed up and verified at
16 MiB, and it still has live `Backup` CRs, so it exercises the sync-reconcile step.

The five drained prefixes are not worth targeting: one `volume.cfg` each, with their retained
versions already scheduled for lifecycle deletion.
