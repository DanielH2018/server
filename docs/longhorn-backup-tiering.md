# Longhorn backup tiering — the kopiaignore rules, translated

> **Current arming state (checked here, not restated in prose below):**
> `k3s_longhorn_backup_armed` / `k3s_longhorn_r2_armed` in
> [`ansible/roles/setup/k3s/defaults/main.yml`](../ansible/roles/setup/k3s/defaults/main.yml)
> are the live source of truth, updated at every arm/disarm. As of 2026-08-16 ~20:45 UTC (the
> seventh transaction-cap event) B2 (`default` target) is **disarmed** and staying that way
> until spend fits the cap or the cap is raised — see
> [`b2-transaction-cap-monitoring-gaps.md`](b2-transaction-cap-monitoring-gaps.md). R2 stays
> armed independently, so only its four volumes (below) are actively backing up; the weekly
> B2 shard schedule this doc describes is the *design*, not current behavior.

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
