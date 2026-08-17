# Longhorn backup tiering — the kopiaignore rules, translated

> **Current arming state (checked here, not restated in prose below):**
> `k3s_longhorn_backup_armed` / `k3s_longhorn_r2_armed` in
> [`ansible/roles/setup/k3s/defaults/main.yml`](../ansible/roles/setup/k3s/defaults/main.yml)
> are the live source of truth, updated at every arm/disarm. As of **2026-08-17 13:12 UTC both
> targets are armed**: B2 (`default`) was re-armed once the over-retention backlog that caused
> the seventh cap event was drained, and the weekly B2 shard schedule below is now current
> behaviour rather than design. See *The transaction budget* section for what the backlog was
> and why draining it was the precondition, and
> [`b2-transaction-cap-monitoring-gaps.md`](b2-transaction-cap-monitoring-gaps.md) for the
> monitoring history.

2026-08-12, after the fifth B2 transaction-cap event. The operator declined to raise the
caps, so usage had to fit them. Kopia's path-level ignore rules
(`ansible/roles/containers/archive/kopia/templates/kopiaignore.j2`) encode two years of
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
   follows the target: the four R2-routed volumes are the whole daily tier, and **every
   B2-destined volume backs up weekly**, sharded across the seven weekdays (~3 volumes per
   day, list index mod 7 = day-of-week) so no single B2 cap-day carries a batch. Week-old
   worst-case restore for the B2 set is an accepted trade.

Both audits ran against the live deployment templates on 2026-08-12 (every mount on every
backed-up PVC checked against the kopia rules).

## Tier map

**Target** is which backupstore the volume's backups land in (`spec.backupTargetName`).
Four volumes moved to Cloudflare R2 on 2026-08-15 (`5ef0dc8e`) so the TLS material, the SSO
store and the two home-automation stores survive a B2 account-level failure — cap, billing,
or key revocation — that would take the whole B2 target with it. Restoring them means
selecting the `r2` target, not `default`; see
[`longhorn-disaster-recovery.md`](longhorn-disaster-recovery.md).

| Volume | Tier | Target | kopia rule preserved |
|---|---|---|---|
| home-assistant-config | daily | **R2** | `.cache/` diverted to emptyDir; `hacs_frontend/` stays (static — see below) |
| zigbee2mqtt-data | daily | **R2** | `log/` diverted to emptyDir |
| n8n-data, n8n-files | weekly (Sun/Fri) | B2 | `.cache/` diverted; WAL churn accepted (below) |
| karakeep-data | weekly (Wed) | B2 | `uv-cache/` was already an emptyDir in the time-tagger pod |
| freshrss-config | weekly (Fri) | B2 | `data/cache/` diverted to emptyDir |
| healthchecks-config | weekly (Wed) | B2 | `log/` diverted to emptyDir |
| authelia-config | daily | **R2** | logs were already an emptyDir |
| wg-easy-config | weekly (Thu) | B2 | clean (peer keys — the one thing kopia pulled from the Pi) |
| traefik-acme | daily | **R2** | clean (acme-only; access logs already emptyDir) |
| code-server-workspace | weekly (Sun) | B2 | **replaced code-server-config 2026-08-16** — see below |
| code-server-config | **no-backup** | — | the deviation below was reversed once its cost was measured |
| jellyfin-config | weekly (Mon) | B2 | `transcodes/` already emptyDir; metadata stays — see deviations |
| sonarr/radarr/prowlarr/bazarr/qbittorrent-config | weekly (sharded) | B2 | MediaCover/logs/Definitions stay — weekly cadence bounds them |
| tdarr-server, tdarr-configs | weekly (Mon/Tue) | B2 | `transcode_cache/` + logs already emptyDir; `Backups/` zips diverted |
| terraria-config | weekly (Wed) | B2 | `.wld.bak*` churn accepted at weekly cadence (retired service; live `.wld` backed up, same operator call as kopia's) |
| scrutiny-web-config | weekly (Sat) | B2 | clean |
| valheim-config | weekly (Tue) | B2 | post-doc addition (2026-08-13, pwd→SOPS recovery); world saves |
| valheim-stats-data, terraria-stats-data | weekly (Mon/Sun) | B2 | post-doc additions; small stats DBs |
| pi-peer-backup-data | weekly (Sat) | B2 | post-doc addition (2026-08-14); the Pi's nightly rsync lands at 04:30 UTC, so a Saturday 04:30 backup captures the previous day's sync — crash-consistent either way |
| scrutiny-influxdb-data | **no-backup** | — | kopia: `scrutiny/influxdb2/` — the volume IS the TSDB (single mount, verified) |
| uptime-kuma-data | **no-backup** | — | kopia: `uptime-kuma/data*/` — monitors regenerate from the static-monitors Secret; admin recreated by hand; history not kept (kopia's own caveat, now in this doc) |
| crowdsec-db | **no-backup** | — | Docker's crowdsec-db named volume was deliberately outside kopia scope |
| autokuma-data | **no-backup** | — | regenerates from the static-monitors Secret |
| pihole-etc, livesync-data, grafana-data, registry/prometheus/loki/tempo/speedtest/karakeep-meili/mosquitto/flaresolverr | no-backup (pre-existing) | — | consistent with kopia: FTL/gravity, couchdb-data, plugins, TSDBs were all excluded; the configs kopia *kept* are Ansible-rendered here |

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
existing `index mod 7` shard split happens to balance well enough that it needed no change. That
is luck, not design — the split is by list position and the cost is by block count.

Draining takes `b2_list_file_versions`, **not** `b2_list_file_names`. Deleting one version of an
object that has superseded versions merely promotes the older one, so the backup survives while
its blocks are gone — the first drain pass reported 1,676/1,676 deleted and still left five
volumes over retention, caught only by re-listing afterwards. The operation reporting success is
not evidence of the end state.

**Run `uv run python scripts/probe.py b2-budget` after adding a volume or changing a shard.** It
re-derives the projection from one listing of the live bucket (~10 Class C) and exits non-zero if
a shard is over budget. Nothing else will announce the drift, because the cost grows quietly with
stored blocks rather than with anything a deploy touches.

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
  to `k3s_longhorn_nobackup_volumes`. Nothing is deleted and nothing stops persisting across
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
  tier and is the main reason Kuma's constantly-heartbeating DB moved to no-backup.
- ***arr logs.db / MediaCover / prowlarr Definitions+Sentry**: not diverted — weekly
  cadence already cut them 7×, and five more emptyDir mounts weren't worth the residual.

## Restore-semantics notes carried over from kopia

- WAL-less restores are consistent-as-of-last-checkpoint (kopia's rule header) — a
  Longhorn snapshot is crash-consistent, strictly better.
- uptime-kuma DR: recreate the first-run admin by hand, then AutoKuma backfills from the
  static files (was: from labels).
- LiveSync: the vault's source of truth is the markdown on each Obsidian client;
  "Rebuild everything" re-uploads the DB from a device.
