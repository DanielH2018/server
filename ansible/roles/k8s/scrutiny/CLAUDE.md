# scrutiny — SMART disk monitoring

Scrutiny's web UI plus a collector DaemonSet and an InfluxDB backend that holds the
SMART trend history. See repo-root `CLAUDE.md` for shared conventions.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's defaults, templates, tasks or containers_list entry, or the k3s role's Longhorn tier lists. -->
- **Deploy tag:** `--tags "scrutiny"`
- **Images:** `ghcr.io/analogj/scrutiny` (`scrutiny_k8s_web_image`), `ghcr.io/analogj/scrutiny`
  (`scrutiny_k8s_collector_image`), `influxdb` (`scrutiny_k8s_influxdb_image`)
- **Route:** `scrutiny.<domain>` · `scrutiny.local.<domain>`, Authelia one_factor
- **Claims:** `scrutiny-influxdb-data` (no backup (listed in k3s_longhorn_nobackup_volumes)),
  `scrutiny-web-config` (weekly -> B2 (default target))
- **Auto-deploy:** denylisted (`k8s_autodeploy: false`) — stateful / manual-upgrade — rolling
  branch tag on a stateful monitor, deliberately manual. ALSO Recreate + RWO volume-claim PVC
  (migrating-state shape) — two independent reasons
<!-- /generated_from -->

- **The Authelia bypass is GET/HEAD only**, on three paths — `/api/summary`, `/api/health`, `/api/device/<wwn>/details` — from the LAN
  and the pod CIDR, configured in the authelia role, not here. It exists for `probe.py
  scrutiny` and the Kuma monitor `k3s Scrutiny`, the only two callers that cross the
  route. Every writer addresses the ClusterIP `http://scrutiny:8080` instead and never
  meets Authelia: the collector DaemonSet (`COLLECTOR_API_ENDPOINT`), monitor-bridge
  (`SCRUTINY_URL`) and homelab-mcp. Scrutiny's web app has no auth of its own, so a wider
  bypass would hand the LAN `DELETE /api/device/:uuid` and `POST /api/settings`.
- **`scrutiny-influxdb-data`** (2Gi, `longhorn`, backed up — the SMART history is the point
  of the tool) and **`scrutiny-web-config`** (1Gi, `longhorn`, the SQLite config DB: device
  metadata, notification settings), seeded through `k8s/volume-claim`.
- **The rolling branch tags** are `master-web` and `master-collector`.

## Notable
- **`networkpolicy-influxdb.yaml.j2` ships in THIS role, not in netpol-baseline** (#1620).
  scrutiny-web EXITS rather than degrades when InfluxDB is unreachable: it calls
  `/api/v2/setup` during AppEngine.Setup and `panic(err)`s on a connection error instead of
  retrying (upstream `webapp/backend/pkg/web/middleware/repository.go`, read against upstream
  master 2026-09-06). `wait-for-influxdb` in `web.yaml.j2` bounds that at 60 x 2s, so with the
  policy absent the pod fails init after two minutes. Same class as authelia's session store
  (#1609). `ansible/tests/services/test_scrutiny_influxdb_policy.py` pins the co-location, the
  port and the `manifests_files` entry. The policy carries no `netpol_baseline_enforced` branch:
  that lever is a netpol-baseline role default and role defaults are role-scoped, so it does not
  resolve here.
- The images are pinned by digest on a **rolling tag**, matching the retired Docker
  copy's policy: Renovate can raise a digest PR for a new commit on the same tag, but
  cannot move the tag itself — that stays a deliberate, supervised redeploy.
- The collector runs on its own cron (`scrutiny_k8s_collector_cron`, daily at
  midnight), pinned because monitor-bridge's SMART-freshness check has a fixed window
  and must not depend on the image's own default schedule.
- **Scrutiny pushes its own Discord alert, and the env key is not the obvious one.**
  `SCRUTINY_NOTIFY_URLS` — top-level `notify.urls` upstream, so `SCRUTINY_WEB_NOTIFY_URLS`
  (the shape the neighbouring `SCRUTINY_WEB_INFLUXDB_*` keys suggest) boots clean and
  notifies nothing. The wrapper in `web.yaml.j2` exports it from the Secret mount rather
  than the pod spec, because the value embeds the webhook token. `secret.yaml.j2` derives
  the shoutrrr `discord://<token>@<id>` from `monitor_discord_webhook_url`, the shared
  "Homelab Alerts" channel Kuma posts to; `tasks/main.yml` asserts that value's shape,
  because CI renders it stubbed and only a deploy sees the real one.
  This does not replace monitor-bridge's `check_scrutiny` — that reads `/api/summary` on a
  poll and covers freshness, wear and temperature as well as `device_status`, and it pages
  when the collector stops reporting at all, which scrutiny itself cannot.
- **scrutiny-web panics if InfluxDB is not answering when it starts.** It calls
  `/api/v2/setup` during `AppEngine.Setup` and upstream `panic(err)`s instead of retrying
  (`webapp/backend/pkg/web/middleware/repository.go`, unchanged since 2022; read against
  upstream master 2026-09-06). The `wait-for-influxdb` init container in `web.yaml.j2` polls
  `http://scrutiny-influxdb:8086/api/v2/setup` for up to 120s and holds the web container
  until it answers. Without it a shared restart costs a crash and a `restarts=1` that fails
  `probe.py health scrutiny`'s own 180s window, so a deploy that worked reports unhealthy.
  A probe cannot cover this — the panic is before either probe is in play.
- **The notify LEVEL is not settable here.** `notify.level` is deprecated upstream and
  rejected at startup with a `ConfigValidationError`, so `SCRUTINY_NOTIFY_LEVEL` crashloops
  the pod. The level lives in the dashboard Settings page — SQLite in `scrutiny-web-config`,
  not the repo. Read 2026-09-06 through `GET /api/settings`: `metrics.notify_level: 2`
  (Fail) with `status_threshold: 3` (SMART self-assessment and Scrutiny's own thresholds
  both). Changing it is a UI action, and nothing in this role can hold it.
