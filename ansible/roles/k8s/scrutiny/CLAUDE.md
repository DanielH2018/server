# scrutiny — SMART disk monitoring

Scrutiny's web UI plus a collector DaemonSet and an InfluxDB backend that holds the
SMART trend history. See repo-root `CLAUDE.md` for shared conventions.

## At a glance
- **Deploy tag:** `--tags "scrutiny"`.
- **Route:** `scrutiny.<domain>`, behind Authelia. The bypass is **GET/HEAD only**, on
  three paths — `/api/summary`, `/api/health`, `/api/device/<wwn>/details` — from the LAN
  and the pod CIDR, configured in the authelia role, not here. It exists for `probe.py
  scrutiny` and the Kuma monitor `k3s Scrutiny`, the only two callers that cross the
  route. Every writer addresses the ClusterIP `http://scrutiny:8080` instead and never
  meets Authelia: the collector DaemonSet (`COLLECTOR_API_ENDPOINT`), monitor-bridge
  (`SCRUTINY_URL`) and homelab-mcp. Scrutiny's web app has no auth of its own, so a wider
  bypass would hand the LAN `DELETE /api/device/:uuid` and `POST /api/settings`.
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
