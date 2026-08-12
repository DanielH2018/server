# Longhorn backup tiering — the kopiaignore rules, translated

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
   bounds what a rule-less volume can cost.

Both audits ran against the live deployment templates on 2026-08-12 (every mount on every
backed-up PVC checked against the kopia rules).

## Tier map

| Volume | Tier | kopia rule preserved |
|---|---|---|
| home-assistant-config | daily | `.cache/` diverted to emptyDir; `hacs_frontend/` stays (static — see below) |
| zigbee2mqtt-data | daily | `log/` diverted to emptyDir |
| n8n-data, n8n-files | daily | `.cache/` diverted; WAL churn accepted (below) |
| karakeep-data | daily | `uv-cache/` was already an emptyDir in the time-tagger pod |
| freshrss-config | daily | `data/cache/` diverted to emptyDir |
| healthchecks-config | daily | `log/` diverted to emptyDir |
| authelia-config | daily | logs were already an emptyDir |
| wg-easy-config | daily | clean (peer keys — the one thing kopia pulled from the Pi) |
| traefik-acme | daily | clean (acme-only; access logs already emptyDir) |
| code-server-config | weekly | extensions/caches stay — see deliberate deviations |
| jellyfin-config | weekly | `transcodes/` already emptyDir; metadata stays — see deviations |
| sonarr/radarr/prowlarr/bazarr/qbittorrent-config | weekly | MediaCover/logs/Definitions stay — weekly cadence bounds them |
| tdarr-server, tdarr-configs | weekly | `transcode_cache/` + logs already emptyDir; `Backups/` zips diverted |
| terraria-config | weekly | `.wld.bak*` churn accepted at weekly cadence (retired service; live `.wld` backed up, same operator call as kopia's) |
| scrutiny-web-config | weekly | clean |
| scrutiny-influxdb-data | **no-backup** | kopia: `scrutiny/influxdb2/` — the volume IS the TSDB (single mount, verified) |
| uptime-kuma-data | **no-backup** | kopia: `uptime-kuma/data*/` — monitors regenerate from the static-monitors Secret; admin recreated by hand; history not kept (kopia's own caveat, now in this doc) |
| crowdsec-db | **no-backup** | Docker's crowdsec-db named volume was deliberately outside kopia scope |
| autokuma-data | **no-backup** | regenerates from the static-monitors Secret |
| pihole-etc, livesync-data, grafana-data, registry/prometheus/loki/tempo/speedtest/karakeep-meili/mosquitto/flaresolverr | no-backup (pre-existing) | consistent with kopia: FTL/gravity, couchdb-data, plugins, TSDBs were all excluded; the configs kopia *kept* are Ansible-rendered here |

## Deliberate deviations from kopiaignore (all in the cheap direction)

- **code-server extensions (+VSIX caches)**: kopia excluded them as reinstallable; here
  they stay on the PVC because an emptyDir would lose them on every pod restart (kopia
  could exclude-but-keep-local; a block backup can't). Weekly cadence caps the cost at
  one Sunday delta of the week's extension churn.
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
