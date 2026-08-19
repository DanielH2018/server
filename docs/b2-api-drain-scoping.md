# Deleting Longhorn backups through the B2 API — scoping

**Recommendation: build it.** The evidence below was measured on 2026-08-19 against the live
bucket, read-only, for a total of 10 Class C transactions.

Two findings carry the decision, and the second was not expected:

1. **Deletion through the B2 API is effectively free.** Enumerating the *entire* backupstore —
   28 volume prefixes, 4,080 live objects, 2.64 GiB — cost **5 Class C calls**. Deletes are
   Class A, which B2 does not meter. Deleting one volume's chain through Longhorn costs on the
   order of several hundred Class C.
2. **Deleting a chain through Longhorn leaves objects behind.** Five volumes whose chains were
   deleted through Longhorn earlier today have **zero Backup CRs** and still hold **286 live
   objects (12.7 MiB)** in B2. We paid full Class C price for a deletion that only half
   happened.

**Finding 2 has an untested alternative explanation, and it is the likelier one.**
`drop_migrated_backup_chain.yml` deletes `Backup` CRs only — it never deletes the
`BackupVolume` CR, which is what owns the volume *directory* in Longhorn's object model. All
seven stale `BackupVolume` CRs are still standing. So "Backups gone, directory still there" is
at least as consistent with *we never deleted the directory's owner* as with *Longhorn strands
objects*. Until that is tested, finding 2 reads as: these objects remain, and the path that
would clean them up was never exercised.

The cost argument alone carries the recommendation, and it is measured. Finding 2 changes only
how much extra the route is worth.

## What was measured

Survey method: one `b2_authorize_account`, then `b2_list_file_versions` paginated over the
prefix `longhorn/backupstore/volumes/`, counting live objects (`action == "upload"`) separately
from hide markers.

### The seven stale prefixes

A prefix is stale when its `BackupVolume` CR exists but the Longhorn `Volume` does not — the
seven volumes rebuilt at 16 MiB blocks today. There are 32 `BackupVolume` CRs against 43 live
volumes.

| Prefix | Service | Live objects | Blocks | Hide markers | Size | Backup CRs |
|---|---|---|---|---|---|---|
| `pvc-9432817d` | prowlarr-config | 225 | 216 | 2 | 50.4 MiB | 2 |
| `pvc-cc4ab76b` | sonarr-config | 206 | 200 | 1 | 64.1 MiB | 2 |
| `pvc-8e41a06c` | — | 90 | 57 | 67 | 1.1 MiB | **0** |
| `pvc-de7f9d60` | — | 68 | 52 | 58 | 10.8 MiB | **0** |
| `pvc-d06c0d9d` | — | 57 | 21 | 31 | 0.2 MiB | **0** |
| `pvc-c926b73e` | — | 40 | 29 | 33 | 0.5 MiB | **0** |
| `pvc-36a38101` | — | 31 | 11 | 17 | 0.1 MiB | **0** |

The top two are the chains deliberately not yet deleted. The bottom five are the residue.

The hide-marker column is the tell. Volumes untouched today carry 1–6 hide markers; the five
drained ones carry 17–67. Longhorn issued deletes, roughly half the objects went away, and the
rest stayed. Their `BackupVolume` CRs last synced at 23:18–23:19Z and carry no
`deletionTimestamp` — but they were also never asked to go away, which is the open question
above.

**The test that settles it** — one command, on the smallest prefix (`pvc-36a38101`: 31 live
objects, 0.1 MiB, zero Backup CRs, replacement already backed up at 16 MiB). Delete its
`BackupVolume` CR, then re-list the prefix. If it empties, finding 2 collapses and the fix is
one extra task in `drop_migrated_backup_chain.yml`. A ready-to-run play with both refusal
guards is at `/home/ubuntu/.claude/jobs/2763e390/tmp/oneshot-bv-delete-test.yml`; the auto-mode
classifier blocks it as a backup-plane write, so it needs explicit approval.

### The bucket layout, and why prefix deletion is safe

```
longhorn/backupstore/volumes/<xx>/<yy>/<volume-name>/
    volume.cfg
    backups/backup_<id>.cfg
    blocks/<aa>/<bb>/<sha256>.blk
```

**Blocks live under each volume's own prefix.** The same content hash appears as separate
objects under different volumes — `7e208b53…blk` exists under both `pvc-a36be217` and
`pvc-2e468900`. Deduplication is within a volume, never across volumes.

This is the property the whole design rests on: deleting everything under one volume's prefix
cannot remove a block another volume's backup depends on. Without it, prefix deletion would be
unsafe at any price.

### Credentials

The existing SOPS key `kopia_b2_key_id` / `kopia_b2_application_key` already carries
`deleteFiles`. No new credential, no `secret_rotation.py sync`.

Worth noting separately: that key also carries `writeBuckets`, `deleteFiles`,
`writeBucketLifecycleRules` and `writeBucketReplications`. It is far broader than either
Longhorn or a drain tool needs. Narrowing it is out of scope here but belongs on the list.

## Cost

Per volume, the two paths:

| | Longhorn | B2 API |
|---|---|---|
| Enumerate | — | shared: 5 Class C for the whole store |
| Delete | ~1.28 Class C per stored block, per deleted backup | Class A, unmetered |
| Reconcile | — | one backup-target sync |
| Objects actually removed | about half | all of them, verified |

For the 14 volumes still at 2 MiB, deleting the old chains through Longhorn is on the order of
**thousands of Class C**. Through the API it is one enumeration (5), one verification re-list
per volume (14), plus the target sync — call it **20–50 Class C** in total.

**This does not make the remaining migration free, and I earlier said it would.** The measured
~180 Class C per volume of *migration* cost — volume deletion, target syncs, the first backup
at 16 MiB — is Longhorn-side and unaffected. Fourteen volumes is still roughly 2,500 Class C,
so about **two days**, not one session. What the API route removes is the deletion half.

## Design

A host-side script plus a thin Ansible play to supply the SOPS credentials.

1. **Enumerate** `longhorn/backupstore/volumes/` once, group keys by volume.
2. **Classify** a prefix as drainable only when its volume name is absent from
   `kubectl get volumes.longhorn.io`, using the same fail-closed guard as
   `drop_migrated_backup_chain.yml`: if the live-volume list is empty or unreadable, refuse
   everything rather than classify everything as an orphan.
3. **Require an explicit allow-list** of volume names on the command line. Discovery proposes;
   the operator disposes. Dry run is the default.
4. **Delete every version**, including hide markers, via `b2_delete_file_version`.
5. **Re-list the prefix and assert it is empty.** This is not optional. The 2026-08-19 drain
   reported "1,676/1,676 deleted" and left five volumes over retention, because it listed
   current names and B2 retains superseded versions. A tool that reports success without
   re-reading is the failure mode we already hit once.
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
the client into a sibling module and put it on `pythonpath`, following the
`roles/setup/common/files` precedent — is cleaner but means editing a deployed monitor for the
benefit of a cleanup tool. Take that path if a third consumer ever appears.

The one change either way: `b2_list_versions` needs a `prefix` parameter.

## What is not verified

- **The `syncRequestedAt` reconcile.** Documented in this repo's own reaper header; not
  exercised this session. If it does not drop the dangling CRs, they need removing another way.
- **The sync's own cost.** The header calls it cheap. A sync must walk every volume directory,
  so estimate tens of Class C per drain run — small, but not zero, and not measured.
- **Whether the residue is residue at all.** The object counts are solid; the cause is not
  established, and the leading explanation is that we never deleted the `BackupVolume` CR. See
  the test above. It does not change the recommendation, since the API route removes the objects
  either way.
- **B2's Class C billing granularity for large pages.** B2 bills listing per 1,000 names
  returned, so the 5-call figure is 5 *billed* units either way; a smaller `maxFileCount` would
  not reduce it.

## Risks

- **Irreversible against the backup store.** Mitigated by dry-run default, the explicit
  allow-list, the fail-closed live-volume guard, and refusing any prefix whose volume exists.
- **New code, unsupervised, against the only offsite copy.** The guards belong in tests before
  the first real run, matching the pattern already used for the migration playbooks.
- **Deleting underneath Longhorn leaves dangling CRs** until the sync runs. Cosmetic, but it
  will make monitoring read wrong in the interim.

## Suggested first target

`prowlarr-config` and `sonarr-config` — the two chains still pending deletion. They are already
scheduled to be deleted through Longhorn at a cost of roughly a thousand Class C between them,
they still have live `Backup` CRs (so they exercise the sync-reconcile step, which the residue
volumes would not), and their replacements are backed up and verified at 16 MiB.

Draining the five residue prefixes is the zero-risk warm-up: no Longhorn object references them,
so there is nothing to reconcile and nothing to lose.
