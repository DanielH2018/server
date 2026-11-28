# Longhorn backup tiering — the kopiaignore rules, translated

> **Current arming state (checked here, not restated in prose below):**
> `k3s_longhorn_backup_armed` / `k3s_longhorn_r2_armed` in
> `k8s/roles/longhorn-backup/defaults/main.yml`
> are the live source of truth, updated at every arm/disarm. As of **2026-08-17 13:12 UTC both
> targets are armed**: B2 (`default`) was re-armed once the over-retention backlog that caused
> the seventh cap event was drained, and the weekly B2 shard schedule below is now current
> behaviour rather than design. See *The transaction budget* section for what the backlog was
> and why draining it was the precondition, and
> [`b2-transaction-cap-monitoring-gaps.md`](archive/b2-transaction-cap-monitoring-gaps.md) for the
> monitoring history.

2026-08-12, after the fifth B2 transaction-cap event. The operator declined to raise the
caps, so usage had to fit them. Kopia's path-level ignore rules
(`git show 2460d0675fd748e70fcbcde87185371ffd62402b:ansible/roles/containers/archive/kopia/templates/kopiaignore.j2`) encode two years of
what is and isn't worth backing up; Longhorn backs up whole block volumes and cannot
express a path exclude, so each rule translates one of three ways:

1. **Whole-tree exclusion → whole-volume `no-backup`** when the volume holds only the
   excluded tree.
2. **High-churn path → emptyDir diversion** in the pod spec: block-level incrementals
   make *static* junk nearly free after the first full (unchanged blocks are never
   re-uploaded), so churn — logs, caches, rewritten zips — is what actually spends
   transactions. Divert those; leave static weight alone.
3. **Everything else → cadence**: the daily/weekly split (see `k3s_longhorn_weekly_volumes`)
   bounds what a rule-less volume can cost. Since 2026-08-16 (the sixth cap event) the split
   follows the target: the R2-routed volumes are the whole daily tier, and **every
   B2-destined volume backs up weekly**, sharded across the seven weekdays (~3 volumes per
   day) so no single B2 cap-day carries a batch. Each claim's day-of-week is the shard its
   `containers_list` entry declares in `weekly_backup_claims`, 0 for Sunday (#4207). Week-old
   worst-case restore for the B2 set is an accepted trade.

Both audits ran against the live deployment templates on 2026-08-12 (every mount on every
backed-up PVC checked against the kopia rules).

## Tier map

**Target** is which backupstore the volume's backups land in (`spec.backupTargetName`).
Four volumes moved to Cloudflare R2 on 2026-08-15 (`5ef0dc8e`) so the TLS material, the SSO
store and the two home-automation stores survive a B2 account-level failure — cap, billing,
or key revocation — that would take the whole B2 target with it. The Airflow warehouse
(`airflow-postgres-data`) joined them on 2026-11-20 through its entry's `r2_backup_claims`:
it holds daily history its sources discard within a week (ADR 0007). Restoring them means
selecting the `r2` target, not `default`; see
[`longhorn-disaster-recovery.md`](longhorn-disaster-recovery.md).

--8<-- "assets/generated/fragments/longhorn-volume-shards.md"

The table lists the volumes the three tier lists name. Every other volume sits on the
`longhorn-nobackup` StorageClass and has no backup. The [services page](reference/services.md)
carries the backup tier of each service, so no list of those volumes is kept here. That is
consistent with kopia, which excluded FTL/gravity, plugins and the TSDBs. kopia excluded
couchdb-data too, and `livesync-data` departs from that (see below). The configs kopia *kept*
are rendered from the repo here.

### Which kopia rule each volume preserves

The tier table has no column for this, so each volume's translation of its kopia rule is
listed here.

- `home-assistant-config` (daily, R2): `.cache/` diverted to emptyDir. `hacs_frontend/` stays
  because it is static (see below).
- `zigbee2mqtt-data` (daily, R2): `log/` diverted to emptyDir.
- `n8n-data`, `n8n-files`: `.cache/` diverted. The WAL churn is accepted (see below).
- `karakeep-data`: `uv-cache/` was already an emptyDir in the time-tagger pod.
- `freshrss-config`: `data/cache/` diverted to emptyDir.
- `authelia-config` (daily, R2): logs were already an emptyDir.
- `wg-easy-config`: clean. It holds the peer keys, the one thing kopia pulled from the Pi.
- `traefik-acme` (daily, R2): clean. It holds only the ACME state, and the access logs were
  already an emptyDir.
- `code-server-workspace`: replaced `code-server-config` on 2026-08-16 (see below).
- `code-server-config` (no backup): the deviation below was reversed once its cost was
  measured.
- `jellyfin-config`: `transcodes/` was already an emptyDir. The metadata stays (see
  deviations).
- `sonarr-config`, `radarr-config`, `prowlarr-config`, `bazarr-config`, `qbittorrent-config`:
  MediaCover, logs and Definitions stay, because the weekly cadence bounds them.
- `tdarr-server`, `tdarr-configs`: `transcode_cache/` and the logs were already an emptyDir.
  The `Backups/` zips are diverted.
- `terraria-config`: the `.wld.bak*` churn is accepted at weekly cadence. The service is
  retired, and the live `.wld` is backed up, which was the same operator call as kopia's.
- `scrutiny-web-config`: clean.
- `valheim-config`: added after the original doc on 2026-08-13 (password to SOPS recovery), and it
  holds the world saves. The image's hourly world zips were diverted to the nobackup
  `valheim-server` claim on 2026-09-02 (`BACKUPS_DIRECTORY`); see *The storage cap is a second
  axis*.
- `valheim-stats-data`, `terraria-stats-data`: added after the original doc. They are small
  stats databases.
- `pi-peer-backup-data`: added after the original doc on 2026-08-14. The Pi's nightly rsync
  lands at 04:30 UTC, so a Saturday 04:30 backup captures the previous day's sync. It is
  crash-consistent either way.
- `navidrome-data`: added after the original doc on 2026-09-03 (issue #946). It moved from
  Sunday into healthchecks-config's index when that role retired (#2806). `ND_DATAFOLDER`
  holds Navidrome's SQLite DB and artwork cache, not the music library, which is a read-only
  `media-data` subPath (`navidrome/templates/deployment.yaml.j2`) that nothing backs up.
- `livesync-data`: kopia excluded couchdb-data, and this volume reverses that. It is the
  CouchDB store behind Obsidian LiveSync, and LiveSync's end-to-end encryption leaves only
  ciphertext in it, so its B2 copy is the one off-site copy of the vault that leaks nothing.
  The `vault-mcp` claims hold the decrypted mirror and stay on the no-backup list. The claim
  moves from `longhorn-nobackup` by the runbook in *Moving `livesync-data` onto the weekly
  tier* below.
- `scrutiny-influxdb-data` (no backup): kopia excluded `scrutiny/influxdb2/`, and the volume
  IS the TSDB (single mount, verified).
- `crowdsec-db` (no backup): Docker's `crowdsec-db` named volume was deliberately outside
  kopia scope.

## The transaction budget (2026-08-17) — what this doc's framing was missing

Everything above prices a volume by **churn**: divert the high-churn paths and a weekly cadence
caps the rest. That is the right model for *upload* cost and it is not the model for the cost
that actually kept firing the cap.

Longhorn enforces `retain` by calling `DeleteDeltaBlockBackup` once per excess backup, and each
call walks the volume's whole block tree with one delimited `ListObjects` per directory
(backupstore `deltablock.go:1496-1510` via `s3.go:108`). LIST is a Backblaze Class C transaction
against a free-tier 2,500/day. So:

- **A prune costs `1 + lv1dirs + lv2dirs` LISTs, about 1.28 per stored block.** It is priced in
  total accumulated blocks, not in what changed. A volume that never changes still costs full
  price every time it prunes.
- **It runs once per deleted backup.** A volume sitting at 11 backups against `retain: 4` costs
  seven full walks the moment Longhorn catches up, not one.

That second point is what produced the seventh cap event on 2026-08-16 and is why four re-arms in
a row failed: 93 backups stood against retain 4, so arming queued ~71 walks — **~22,989 Class C,
nine days of cap**, spent before any new data moved. The backlog was drained directly against the
B2 API before the 2026-08-17 re-arm (deletes are Class A and unmetered, so the end state Longhorn
was heading for cost nothing to reach). With the three stale `no-backup` prefixes removed at the
same time, the store went from 22 prefixes / 93 backups / 9,312 blocks / 6.15 GiB to **19 / 60 /
4,031 / 2.86 GiB**, every volume at or under `retain`.

Post-drain the worst weekday shard projects at ~1,524 Class C against the 2,500 cap, and the
`index mod 7` shard split of the time happened to balance well enough that it needed no change.
That was luck, not design: the split was by list position and the cost is by block count. Since
#4207 each claim declares its shard on its `containers_list` entry, so moving load between
weekdays moves only the claims it names.

> **Superseded 2026-08-20 — do not size a shard off the figure above.** The 16 MiB block migration
> cut block count per volume roughly 8.6x (sonarr 200 → 23, prowlarr 216 → 25), and the cost basis
> here is per block. The worst projected shard is now **~220 Class C against a 2,100 budget** (the
> 2,500 cap less a 400 reserve), where d2 alone used to be 1,359. The live numbers are
> `k3s_longhorn_daily_backup_budget` in `hosts/components/k3s-server/defaults/main.yml` and
> `uv run python scripts/diagnostics/probe.py b2-budget`, which prices every shard against the current store.
> The reasoning below is still correct; only the magnitudes moved.

### Retention is per job, and only while that job still selects the volume

Verified 2026-08-17 by forcing `weekly-backup-d2` to run: `radarr-config` finished on 4
`daily-backup` backups plus 1 `weekly-backup-d2`, and **deleted nothing**. A RecurringJob
enforces its `retain` over its own backups only, and only for volumes listed in its `groups:`.

Two consequences that are easy to get backwards:

- **A shard prunes nothing until it holds more than `retain` of its own backups.** With
  `retain: 2` that is the third weekly run, so a shard's prune cost starts about three weeks
  after the tier goes live — not on its first run. Until then its block count only grows.
- **Backups left by a previous tier are unowned, not queued.** When a volume moved from the
  `default` group to a weekday shard, `daily-backup` stopped selecting it and can never prune
  those backups again. No `retain` value governs them — `retain: 14` on the daily job is
  irrelevant, because the job never runs against that volume.
  `scripts/backup/longhorn_reap.py backups` is their only owner, and its safety floor means it can only clear them once the volume has
  weekly backups of its own.
- **A seed made before 2026-09-03 is unowned in a way the reaper cannot see.** Until then
  `seed_volume_backup.yml` made a backup carrying no `RecurringJob` label at all, so no job's
  `retain` ever counted or pruned it, and the reaper reads an unlabelled backup as the current
  tier's own — on 2026-09-02 it listed zero reapable against eleven live seeds.
  `longhorn_reap.py backups --mode seeds` is their owner. It retires a seed once its
  volume holds `--seed-floor` (default 2, the shards' retain) Completed backups that do carry a
  job label, dry-runs by default, and takes `--claim` to pace the Class C spend one volume at a
  time. The seed's *snapshot* was unowned the same way and outlived the backup: dropping the
  backup left a `seed-*` snapshot pinning every block beneath it against trim, which is how
  valheim-config carried 3.5 GB of deleted files into each weekly backup (issue #942). Since
  2026-09-03 the play labels both CRs `RecurringJob: <owning job>`, so the job's retain prunes
  the backup and the snapshot auto-cleanup removes the snapshot, on the normal cadence.

`k3s_longhorn_weekly_backup_retain` was lowered 4 → 2 on 2026-08-17. Retention is what costs, so
holding two rather than four pins fewer blocks and makes every future prune cheaper; the trade is
recovery reach, which drops from about a month to about a fortnight.

Draining takes `b2_list_file_versions`, **not** `b2_list_file_names`. Deleting one version of an
object that has superseded versions merely promotes the older one, so the backup survives while
its blocks are gone — the first drain pass reported 1,676/1,676 deleted and still left five
volumes over retention, caught only by re-listing afterwards. The operation reporting success is
not evidence of the end state.

**Run `uv run python scripts/diagnostics/probe.py b2-budget` after adding a volume or changing a shard.** It
re-derives the projection from one listing of the live bucket (~10 Class C) and exits non-zero if
a shard is over budget. The cost grows quietly with stored blocks rather than with anything a
deploy touches, so the daily `b2-budget` timer runs the same projection. Checks 9 and 10 of
backup health read its journal: an `OVER BUDGET:` line, or no `budget per day:` summary line
inside 26h, turns the check DOWN.

### What a deletion cost, after it happened

`probe.py b2-deletions` charges each completed backup deletion to the B2 ledger. It reads the
`Complete deleting backup` lines longhorn-manager writes (`backups.go:310`) out of Loki, filters
them to the B2 BackupTarget, and prices each one at its volume's `1 + lv1 + lv2` block-tree walk.
It reads Loki and the Kubernetes API only, so it spends nothing on B2 and is safe on a timer.

Two timers run it (`hosts/components/k3s-host-checks/`): the accounting pass at 07:10, then
the `b2-budget` listing at 07:20. **That order is the constraint.** A deletion can only be priced
by a listing taken before it — afterwards the tree is smaller than the one the deletion walked —
so each pass charges against the previous day's listing and the listing then refreshes for
tomorrow. The recorded figure is therefore a floor, and it is a model rather than a measurement:
the tree each deletion walked is gone.

A deletion whose volume is absent from the snapshot is reported UNPRICED and the command exits 1.
That is the one state to act on — the transactions were spent and no later run can recover the
figure. It still gets a ledger line, at zero Class C with UNPRICED in the note, so the ledger
records that the deletion happened rather than omitting it entirely. Both crons pipe their output
through `logger`, so each run’s summary is searchable in Loki; cron discards the exit code.

A completed `b2-deletions` run ends with one line of a fixed shape —
`b2-deletions: charged N, skipped N, unpriced N`, or `b2-deletions: declined: <reason>` when the
BackupTarget carries no URL. Check 10 of `longhorn-backup-health.sh` reads that line to tell a
run that finished from one that died part-way, so a reword changes the check's regexes in the
same edit (`docs/k3s-node-plane-crons-and-incidents.md`).

Deletions against the `r2` BackupTarget are skipped. R2's caps are monthly and vast, and charging
one against B2's 2,500/day Class C cap would inflate the ledger against a cap that does not
govern it. The target name is read from the BackupTarget CRs rather than inferred from the region
component of the target URL.

## The storage cap is a second axis (2026-09-02)

Everything above prices a backup in transactions. B2's free tier also caps **stored bytes at
10 GB**, and that is the cap that fired on 2026-09-02, with the transaction budget nowhere near
its limit. The two axes have different drivers and different remedies, so a change that fixes one
can leave the other untouched.

Storage is priced by **retained blocks** — every block any retained backup still references. Three
consequences, each measured on 2026-09-02.

**A rewritten file costs new blocks every week, and cadence does not help.** `valheim-config` held
5.01 GB of a 7.59 GB bucket. The image zips `worlds_local` hourly into `/config/backups` and keeps
three days of zips; a zip shares no bytes with the one before it, so every weekly backup uploaded
the whole rotation — 135 new 16 MiB blocks on 2026-09-01, about 2.2 GB a week. That is rule 2
above ("rewritten zips") on a volume added after the 2026-08-12 audit, and the remedy is the one
tdarr already had: `BACKUPS_DIRECTORY` points at the nobackup `valheim-server` claim, which keeps
the local rollback zips and costs B2 nothing.

**Deleting the files does not shrink the backup. A trim does.** Removing `/config/backups` took
the filesystem from 1.33 GB to 58.6 MB and left Longhorn's `actualSize` at 4.26 GB, because a
filesystem delete unmaps nothing underneath — the next backup would still have referenced those
blocks. `fstrim` against the volume's mount on the node released 2.1 GiB:

```bash
findmnt -rn -o TARGET,SOURCE | grep <pvc-name>          # find the globalmount handle
sudo fstrim -v /var/lib/kubelet/plugins/kubernetes.io/csi/driver.longhorn.io/<handle>/globalmount
```

`actualSize` then fell to 3.61 GB rather than to the filesystem's 58.6 MB, because two snapshots
still hold the old bytes: the seed's 1.53 GB and the 2026-09-01 weekly's 2.02 GB. Rotation would
not have released them either: `retain: 2` prunes the *backup*, and the weekly snapshot it was
made from stays as the base of the next incremental, so every new weekly backup kept referencing
the same 3.5 GB of blocks and the bucket never got them back. What released them on 2026-09-03 was deleting both snapshots, then a remount, then
a trim — in that order, because ext4 skips block groups it already trimmed since mount and the
nightly trim had trimmed them all while the snapshots still pinned them. A `rollout restart` is
not a remount (kubelet keeps the volume staged on the same node); scaling the workload to 0
until the Volume CR reads `detached` is. To wait for that state before scaling back, run:

```bash
cc-wait k8s-field -n longhorn-system volumes.longhorn.io/<pvc-name> \
  '{.status.state}' --equals detached
```

`actualSize` went 3.61 GB → 215 MB, the next backup was 63 MB, and the bucket went from
7.64 GB billed to 2.68 GB once the hidden versions were expired.

**A deletion's saving lands seven days later.** The bucket carries one lifecycle rule,
`daysFromHidingToDeleting: 7`. A delete writes a hide marker over the current version, and B2
bills the hidden version until the rule removes it. Deleting the eleven seeds took live bytes from
7.59 GB to 6.14 GB and moved the billed figure by nothing — the console kept reading 7.59 GB, and
would for a week. Measure the live figure by taking the newest version per name and skipping the
ones whose `action` is `hide`; a plain `b2_list_file_names` reports the hidden name as gone and
the reclaimed bytes as already saved, which is wrong in both directions. This is the same trap as
the drain's `b2_list_file_versions` above, one level up.

## Bucket immutability (ADR-0041)

[ADR-0041](adr/0041-b2-weekly-and-r2-etcd-snapshots-are-locked.md) locks the B2 weekly bucket
with per-object Object Lock retentions and the R2 etcd snapshots with a bucket lock rule. It
leaves the R2 daily tier unlocked. The B2 bucket has file lock enabled and **no default
retention**: the daily sweep sets each object's retention. monitor-bridge's `b2_object_lock`
row reads the B2 setting once a day and fires `B2ObjectLockOff`, a warning, until steps 2 and
2a are both done. monitor-bridge's `r2_etcd_lock` row proves the R2 rule by its effect once a
day, and fires `R2EtcdLockNotHolding`, a warning, until step 3 is done. *The R2 lock canary*
below says how.

Every step is **operator-only**. A session holds no credential that can change a bucket, a
key, a Cloudflare token or a cluster object. Run each command as `ubuntu` on daniel-box, with
the `b2` command-line tool authorized by the account's master key. Never use the Longhorn
key, which the cluster holds.

### Step 1 result: a default retention breaks every backup

The operator ran a real Longhorn backup and backup deletion against the scratch bucket
`lock-spike-45841c6d` on 2026-11-21
([#9699](https://github.com/DanielH2018/server/issues/9699#issuecomment-7065073692)).

- **With a governance default retention, the first backup failed** at its `locks/*.lck`
  upload: `Content-MD5 OR x-amz-checksum- HTTP header is required for Put Object requests
  with Object Lock parameters`. B2 treats every upload into a bucket with a default retention
  as carrying lock parameters, and Longhorn's S3 client sends neither header.
- **With file lock on and no default retention, every test held.** Backups completed. A
  governance retention set on each object by hand survived both a backup deletion and a
  backup-volume deletion: each locked version stayed, a hide marker went over it, and
  `longhorn-manager` logged no `failed to delete objects`.

So the design is file lock with no default, and the sweep sets each retention. The cost is
that a new block is unlocked from its upload until the next 07:30 UTC sweep, up to one day.

### Step 2: enable file lock on the B2 weekly bucket

**This cannot be undone.** B2 never turns file lock off on a bucket once it is on. The
command sets no default retention, and none must ever be added: a default makes every weekly
backup fail.

```bash
b2 bucket update --file-lock-enabled daniel-server-kopia
```

```bash
b2 bucket get daniel-server-kopia
```

The second command's `fileLockConfiguration` must read `"isFileLockEnabled": true` and a
`defaultRetention` whose `mode` is `null`. `B2ObjectLockOff` clears at the next daily read
only once the Longhorn key can read the setting, which is step 2a. If the row reads
`key bypasses governance`, the Longhorn key holds `bypassGovernance`: mint a key without it,
then rotate `longhorn_b2_key_id` and `longhorn_b2_application_key`. If it reads
`default retention set`, remove the default at once with
`b2 bucket update --default-retention-mode none daniel-server-kopia`.

### Step 2a: let the Longhorn key read the bucket's lock setting

The `b2_object_lock` row and the daily sweep both read the lock setting with the Longhorn key
(`longhorn_b2_key_id`, which monitor-bridge mounts as `B2_PROBE_KEY_ID`). That key lacks
`readBucketRetentions`, so the row reads `lock unreadable` and the sweep prints
`declined: the key cannot read the bucket's Object Lock setting` whatever the bucket's state.
B2 cannot add a capability to a key, so mint a new one and rotate both SOPS keys to it.

Steps 2a and 2b share one key and one rotation, so the new key also holds
`writeFileRetentions`. The 2026-11-22 rotation copied the live key's capabilities, which
`b2 key list --long` prints, and added the two the lock needs. That keeps every reader of the
key working through the rotation:

```bash
b2 key create --bucket daniel-server-kopia longhorn-weekly listBuckets,writeBuckets,readBuckets,listFiles,readFiles,shareFiles,writeFiles,deleteFiles,readBucketEncryption,writeBucketEncryption,readBucketLogging,writeBucketLogging,readBucketReplications,writeBucketReplications,readBucketNotifications,writeBucketNotifications,readBucketLifecycleRules,writeBucketLifecycleRules,readBucketRetentions,writeFileRetentions
```

Longhorn needs far fewer: the step 1 scratch key backed up and deleted with
`listBuckets,listFiles,readFiles,writeFiles,deleteFiles,readBucketRetentions`.
[#9934](https://github.com/DanielH2018/server/issues/9934) trims the key once the old one is
deleted.

Then set `longhorn_b2_key_id` and `longhorn_b2_application_key` to the printed pair with
`sops`, and run `cluster_vars.py write` so Flux carries them to the cluster
([Secret rotation](secret-rotation.md)). Delete the old key with `b2 key delete` only after a
weekly backup has completed with the new one.

### Step 2b: give the daily sweep a key that can extend retention

**Until this step is done, no B2 object is locked.** With no default retention, the sweep is
the only thing that sets a lock, and it is blocked until the key it reads holds
`writeFileRetentions`. A green `b2_object_lock` row means the bucket is set up, not that any
object is locked. The sweep reads the Longhorn key: the operator chose it over a separate
sweep key on 2026-11-22 ([#9787](https://github.com/DanielH2018/server/issues/9787)), and
ADR-0041's *`writeFileRetentions` lets a leaked key lock objects for good* records the cost.
So step 2b is step 2a with `writeFileRetentions` added to the new key's list: one key and one
rotation. The new key must not hold `bypassGovernance`.

With the key in place, the `homelab-b2-retention` timer on daniel-box stops being blocked at its
next 07:30 UTC run. To confirm, read its line:

```bash
journalctl -t b2-retention --since today
```

It must read `b2-retention: extended N of N objects to <date>`, with the date 7 days ahead.

### What the daily sweep does

B2 counts a version's retention from its upload, and the bucket sets none at upload.
`scripts/backup/b2_extend_retention.py` sets every lock. Once a day it lists the current
objects under `longhorn/backupstore/volumes/` and sets each one's governance retention to now
plus `RETENTION_DAYS` (7). It skips each backup's `locks/` files. That locks a new block by
the next run, and keeps the base blocks that every retained weekly backup references locked,
however old their upload.

- **It costs about four Class C calls a day:** the authorize, the bucket read and one listing
  page per 1,000 objects. Each retention update is Class A, which B2 does not meter.
- **It adds no storage.** It uploads nothing. A block that Longhorn prunes leaves the listing,
  so its lock lapses within 7 days, and the lifecycle rule removes it as before.
- **It declines and changes nothing** while the lock is off or unreadable. The
  `b2_object_lock` row reports both. A bucket default retention does not stop the sweep, but
  the row reports it, because it breaks every backup.
- **It is blocked** once the bucket is locked but the key lacks `writeFileRetentions`. It
  prints `b2-retention: blocked: the key lacks writeFileRetentions` and exits 1, and check 10
  reports the sweep as not completing until either key from step 2b is in place.
- **Checks 9 and 10 watch it.** `longhorn-backup-health` reads the `b2-retention` journal
  tag, and reports `down` on a `FAILED` line or on a day with no completed run. The
  `b2_object_lock` row does not read the sweep, because the pod cannot read daniel-box's
  journal and the checks already own that signal.

Longhorn's `full-backup-interval` was the other candidate, and ADR-0041 records why it was
rejected: a full backup re-uploads every block as a new version, and the replaced versions
stay billed for a week, so the bucket would carry about two copies of the referenced set
against the 10 GB free tier.

### Step 3: lock the R2 etcd snapshot prefix

The bucket name is the SOPS key `r2_bucket`, so each command reads it from the primary
checkout. Authorize wrangler first. Its browser callback listens on daniel-box's port 8976,
so if your browser runs on another computer, open `ssh -L 8976:localhost:8976 daniel-box`
from that computer before you run it.

```bash
npx wrangler login
```

```bash
cd /home/ubuntu/server && npx wrangler r2 bucket lock add "$(sops -d --extract '["r2_bucket"]' secrets/secrets.yml)" etcd-snapshots-7d etcd-snapshots/ --retention-days 7
```

```bash
cd /home/ubuntu/server && npx wrangler r2 bucket lock list "$(sops -d --extract '["r2_bucket"]' secrets/secrets.yml)"
```

The list must show the one rule over `etcd-snapshots/`, for 7 days, and nothing over
`longhorn/` or over an empty prefix. A rule there would lock Longhorn's daily blocks, and R2
refuses a locked delete with a 409, so the daily prune would retry. The rule must stay
shorter than the 14 days k3s keeps its S3 snapshots, or R2 refuses k3s's prune too.

`R2EtcdLockNotHolding` clears at the next daily canary run.

### The R2 lock canary

R2's S3 API cannot read a lock rule, and the Cloudflare permission that can is granted only
for a whole account. The operator chose on 2026-11-23 to prove the rule by its effect with
the R2 key the cluster already holds, rather than give a pod an account-wide token
([#9702](https://github.com/DanielH2018/server/issues/9702)). Once a day, monitor-bridge's
`r2_etcd_lock` row (`k8s/roles/monitor-bridge/files/checks/r2.py:check_r2_etcd_lock`) does
three things under `etcd-snapshots/lock-canary-`:

1. It uploads today's canary, `lock-canary-<date>`, unless the listing already holds it. R2
    refuses to overwrite a locked object, so a restarted bridge must not upload twice.
1. It tries to delete the newest earlier canary younger than the lock, or today's on the
    first run. R2's 409 `ObjectLockedByBucketPolicy` means the rule holds. A delete that
    succeeds means no rule covers the prefix, and the row reads `lock missing`.
1. It deletes each canary dated more than 7 days back, which must succeed. A refusal means a
    lock over the prefix outlasts 7 days, and the row reads `lock outlasts its age`.

Any other 4xx answer from R2 fails the row as `request refused`. The row holds each verdict
for a day, so a missing lock does not upload and delete a canary every cycle. A 5xx or a 429
fails the row for one cycle only, and the next cycle probes again.

- **Each canary is zero bytes.** k3s lists every non-empty object under `etcd-snapshots/` as
  a ready snapshot (`pkg/etcd/s3/s3.go`, `ListSnapshots`, at v1.37.2+k3s1). A non-empty
  canary would read as a fresh snapshot to backup health's check 12 and hide a stale one.
- **No name matches `offbox-*`**, so k3s's retention never counts a canary, and the etcd drill
  never restores one.
- **It costs two Class A operations a day**: the listing and the upload. R2 does
  not bill a delete.
- **It does not read the rule's prefix list.** A rule widened over `longhorn/` passes the row,
  and `r2_usage` notices it only once Longhorn's retried prunes have spent the month's
  operations.

### Step 4: remove the step 1 scratch objects

The scratch locks expire at about 15:40 UTC on 2026-11-22. The first three commands can run
at any time:

```bash
sudo k3s kubectl -n longhorn-system delete volumes.longhorn.io lock-spike
```

```bash
sudo k3s kubectl -n longhorn-system delete backuptarget lock-spike
```

```bash
sudo k3s kubectl -n longhorn-system delete secret lock-spike-b2
```

After the locks expire, empty the bucket, then delete its key and the bucket. B2 refuses to
delete a bucket that holds any version, and refuses to delete a locked version:

```bash
b2 rm -r --versions b2://lock-spike-45841c6d
```

```bash
b2 key delete "$(b2 key list | awk '$2 == "lock-spike" {print $1}')"
```

```bash
b2 bucket delete lock-spike-45841c6d
```

If the key deletion prints a usage error, `b2 key list` has no `lock-spike` row, so the key is
already gone.

## Moving `livesync-data` onto the weekly tier

A bound claim's `storageClassName` is immutable, and both Longhorn classes reclaim with
`Delete`. So the move to `longhorn` recreates the claim, and deleting the old claim would delete
the vault's server copy with it. A Flux apply of the new class to the old claim fails as
immutable, and a failed `livesync` revision writes the stateful hold. So the move takes two PRs
with an operator cutover between them:

- The first PR declares the claim's weekly shard in `k8s/services.yml` and sets
  `flux_suspend: true` in `k8s/roles/livesync/defaults/main.yml`. It keeps the
  `longhorn-nobackup` class, so its render matches the live claim. The suspend keeps Flux off
  `livesync` and `livesync-snapshot` while the operator works.
- The resume PR, the cutover's last step, sets `livesync_k8s_storage_class: longhorn` and
  removes `flux_suspend` in one commit. By then the live claim is on `longhorn`, so Flux
  applies a matching spec. The suspend and the class change never land together, because
  nothing orders the root's write of `spec.suspend` before the `livesync` Kustomization
  reconciles the same revision.

Run the cutover soon after the first PR lands. `FluxSuspended` warns after 24 hours, and arm 5
of the render-diff gates refuses any other PR whose render reaches `livesync` until the resume.
A resume PR that drops the suspend and keeps `longhorn-nobackup` fails
`tests/longhorn/test_every_longhorn_pvc_has_a_tier.py::test_every_backed_up_claim_is_on_longhorn_or_mid_cutover`.
A claim on a backup tier list that is not on `longhorn` is never backed up.

The longhorn-reconcile job leaves the old volume alone meanwhile. The volume sits in the
`no-backup` group, and the job moves a weekly claim into its shard only from `default`
(`k8s/roles/longhorn-reconcile/files/longhorn_reconcile.py:desired_labels`). Nothing is
uploaded from the old 2 MiB-block volume. The recreated volume takes the 16 MiB block size
(ADR 0008).

The Obsidian clients keep their own copies of the vault throughout. Sync pauses while
`livesync` is at zero replicas, and the clients push what they changed once it is back.

Every step that writes is operator-only: it needs the k3s admin kubeconfig. Do not start the
cutover inside the Sunday no-promotion window, because the last step lands a PR.

1. Check that the suspend took hold. `uv run python scripts/diagnostics/probe.py landing` shows
   `last-good` at or past the merge that set `flux_suspend`, and its `suspended (Flux):` line
   names `livesync` and `livesync-snapshot`. A reset to an older `last-good` would resume
   `livesync` in the middle of the cutover.
1. Scale `deploy/livesync` to zero, and wait for its volume to detach:

    ```bash
    sudo k3s kubectl -n homelab scale deploy/livesync --replicas=0
    kubectl -n homelab get pvc livesync-data -o jsonpath='{.spec.volumeName}'
    cc-wait k8s-field -n longhorn-system volumes.longhorn.io/<pv> \
      '{.status.state}' --equals detached
    ```

    `<pv>` is the name the second command prints. Use it in every later step.
1. Keep the old volume when its claim goes. Set the PV's reclaim policy to `Retain`, delete
   the claim, then clear the PV's claim reference so a new claim can bind it:

    ```bash
    sudo k3s kubectl patch pv <pv> \
      -p '{"spec":{"persistentVolumeReclaimPolicy":"Retain"}}'
    sudo k3s kubectl -n homelab delete pvc livesync-data
    sudo k3s kubectl patch pv <pv> --type json \
      -p '[{"op":"remove","path":"/spec/claimRef"}]'
    ```

    The old volume is the fallback until the last step. It survives every step before that,
    so no separate snapshot is needed.
1. Create two claims. `livesync-data-old` binds the old volume, and `livesync-data` is a new,
   empty volume on `longhorn` with the spec the resume PR renders. Do not create it between
   03:17 and 03:30 UTC. A new `longhorn` volume sits in the daily group until the hourly
   reconcile at :17 moves it, and the daily job at 03:30 would leave a B2 backup that no
   job's retain prunes. Write this to a file and apply it with
   `sudo k3s kubectl apply -f <file>`:

    ```yaml
    apiVersion: v1
    kind: PersistentVolumeClaim
    metadata:
      name: livesync-data-old
      namespace: homelab
    spec:
      accessModes: [ReadWriteOnce]
      storageClassName: longhorn-nobackup
      volumeName: <pv>
      resources:
        requests:
          storage: 2Gi
    ---
    apiVersion: v1
    kind: PersistentVolumeClaim
    metadata:
      name: livesync-data
      namespace: homelab
    spec:
      accessModes: [ReadWriteOnce]
      storageClassName: longhorn
      resources:
        requests:
          storage: 2Gi
    ```

1. Copy the database files across with their ownership, and compare the two trees:

    ```bash
    sudo k3s kubectl -n homelab run livesync-copy --rm -it --restart=Never \
      --image=busybox:1.37 \
      --overrides='{"spec":{"containers":[{"name":"copy","image":"busybox:1.37","command":["sh","-c","cp -a /old/. /new/ && du -s /old /new && ls -lan /new"],"volumeMounts":[{"name":"old","mountPath":"/old"},{"name":"new","mountPath":"/new"}]}],"volumes":[{"name":"old","persistentVolumeClaim":{"claimName":"livesync-data-old"}},{"name":"new","persistentVolumeClaim":{"claimName":"livesync-data"}}]}}'
    ```

    The two `du` totals should match, and the files should keep the CouchDB uid.
1. Check the new volume. Its `spec.backupBlockSize` reads `16777216`. Within the hour,
   longhorn-reconcile labels it `recurring-job-group.longhorn.io/weekly-backup-d0`:

    ```bash
    kubectl -n homelab get pvc livesync-data -o jsonpath='{.spec.volumeName}'
    kubectl -n longhorn-system get volumes.longhorn.io <new pv> \
      -o jsonpath='{.spec.backupBlockSize} {.metadata.labels}'
    ```

1. Resume `livesync` with a PR that edits `k8s/roles/livesync/defaults/main.yml`: it sets
   `livesync_k8s_storage_class: longhorn` and removes `flux_suspend`. The same PR adds
   `("homelab", "livesync-data"): ("longhorn", "2Gi")` to `_KNOWN_CLAIMS` in
   `tests/k8s/workloads/test_k8s_claims.py`. Land it with `land.sh`. Flux applies a claim that
   matches the live one and restores `replicas: 1`, and the `livesync-snapshot` Job snapshots
   the new volume first. Open Obsidian on one device and confirm that it syncs.
1. Delete the old volume. Set its reclaim policy back to `Delete`, then delete its claim, which
   deletes the PV and the Longhorn volume:

    ```bash
    sudo k3s kubectl patch pv <pv> \
      -p '{"spec":{"persistentVolumeReclaimPolicy":"Delete"}}'
    sudo k3s kubectl -n homelab delete pvc livesync-data-old
    ```

The move is finished once `probe.py b2-spend` and the backup-health check show
`livesync-data` on shard 0 and its first Sunday backup has completed.

If the copy fails, the old volume is untouched under `livesync-data-old`. Every client still
holds the vault, so LiveSync's "Rebuild everything" from one client is the last fallback.

## Deliberate deviations from kopiaignore (all in the cheap direction)

- ~~**code-server extensions (+VSIX caches)**: kopia excluded them as reinstallable; here
  they stay on the PVC because an emptyDir would lose them on every pod restart (kopia
  could exclude-but-keep-local; a block backup can't). Weekly cadence caps the cost at
  one Sunday delta of the week's extension churn.~~
  **Reversed 2026-08-16**, once the cost was measured rather than assumed. `du` inside the
  pod: 5.8 G in `.local/share/code-server/extensions`, 637 M in `/config/extensions`, 223 M
  of caches, 17 M of code-server state — against **2.4 M of workspace**. Weekly cadence had
  not capped anything, because the cost that mattered was never the nightly delta: the volume
  was 52% of the 13.72 GiB backup set and 3,668 of its 7,023 blocks, and Longhorn's restore
  path issues one Class-B GET per block, putting a code-server restore at **147% of B2's
  2,500/day allowance — not restorable inside one day**.
  The third option this table's framing missed is neither "keep it" nor "emptyDir": a
  **second claim holding only the keepers**. `code-server-workspace` carries `workspace`,
  `.ssh`, `.config` and the git identity (~2.5 M) by subPath, and `code-server-config` moved
  to `k3s_longhorn_nobackup_volumes` (declared since #4207 as the code-server entry's
  `no_backup_claims`). Nothing is deleted and nothing stops persisting across
  restarts — only the backup scope changed. Extensions were always the safest thing to drop:
  all seven are baked into the image as `.vsix` and reinstalled by
  `/custom-cont-init.d/10-extensions.sh` on **every** container start, so they need no network
  and no upstream service in the recovery path.
  Note the 5.8 G is orphaned regardless: the server runs with
  `--extensions-dir /config/extensions` and never reads it.
- **jellyfin metadata/attachments/subtitles**: excluded by kopia as re-fetchable, kept
  here — re-fetching a whole library's metadata on restart hammers the metadata
  providers, and the tree is static-ish so its block-delta cost is small.
- **SQLite WAL/SHM sidecars** (`*.db-wal` etc., kopia's biggest churn rule): inseparable
  from their DB at block level. This churn is the residual per-night cost of the daily
  tier and is the main reason Kuma's constantly heartbeating DB moved to no-backup.
- ***arr logs.db / MediaCover / prowlarr Definitions+Sentry**: not diverted — weekly
  cadence already cut them 7×, and five more emptyDir mounts weren't worth the residual.

## Restore-semantics notes carried over from kopia

- WAL-less restores are consistent-as-of-last-checkpoint (kopia's rule header) — a
  Longhorn snapshot is crash-consistent, strictly better.
- LiveSync: the vault's source of truth is the markdown on each Obsidian client;
  "Rebuild everything" re-uploads the DB from a device.
