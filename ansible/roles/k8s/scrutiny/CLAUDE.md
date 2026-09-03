# scrutiny — SMART disk monitoring

Scrutiny's web UI plus a collector DaemonSet and an InfluxDB backend that holds the
SMART trend history. See repo-root `CLAUDE.md` for shared conventions.

## At a glance
- **Deploy tag:** `--tags "scrutiny"`.
- **Route:** `scrutiny.<domain>`, behind Authelia. `/api/*` is bypassed for LAN traffic
  (the collector's POSTs, monitor-bridge's summary reads) — configured in the authelia
  role, not here.
- **Claims:** `scrutiny-influxdb-data` (2Gi, `longhorn`, backed up — the SMART history is
  the point of the tool) and `scrutiny-web-config` (1Gi, `longhorn`, the SQLite config
  DB: device metadata, notification settings).
- **`k8s_autodeploy: false`** — rolling upstream branch-tag images (`master-web`,
  `master-collector`) on a stateful monitor, deliberately manual; also `Recreate` + an
  RWO PVC seeded through `k8s/volume-claim`. Reason is in `defaults/main.yml`.

## Notable
- The images are pinned by digest on a **rolling tag**, matching the retired Docker
  copy's policy: Renovate can raise a digest PR for a new commit on the same tag, but
  cannot move the tag itself — that stays a deliberate, supervised redeploy.
- The collector runs on its own cron (`scrutiny_k8s_collector_cron`, daily at
  midnight), pinned because monitor-bridge's SMART-freshness check has a fixed window
  and must not depend on the image's own default schedule.
