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
- **`scrutiny-influxdb-data`** (2Gi) is deliberately not backed up. It holds only the SMART
  time series, which the daily collector runs rebuild; the reason sits beside its entry in
  `k3s_longhorn_nobackup_volumes` in `ansible/roles/setup/k3s/defaults/main.yml`.
  **`scrutiny-web-config`** (1Gi) is the SQLite config DB: device metadata and notification
  settings. `k8s/volume-claim` creates both claims.
- **The rolling branch tags** are `master-web` and `master-collector`.

## Notable
- **`networkpolicy-influxdb.yaml.j2` ships in THIS role, not in netpol-baseline** (#1620).
  scrutiny-web EXITS rather than degrades when InfluxDB is unreachable: it calls
  `/api/v2/setup` during AppEngine.Setup and `panic(err)`s on a connection error instead of
  retrying (upstream `webapp/backend/pkg/web/middleware/repository.go`, read against upstream
  master 2026-09-06). `wait-for-influxdb` in `web.yaml.j2` holds the web container for up to
  60 x 2s until InfluxDB answers, so with the policy absent the pod fails init after two
  minutes. Without that init container a shared restart costs a crash and a `restarts=1` that
  fails `probe.py health scrutiny`'s 180s window. A probe cannot cover this, because the panic
  happens before either probe runs. Same class as authelia's session store
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
- **Scrutiny sends no notification of its own; monitor-bridge is the single pager.**
  `check_scrutiny` reads `/api/summary` on a poll of at most 300 s and pages on a non-zero
  `device_status`, which is the same SMART self-assessment and the same Scrutiny attribute
  thresholds scrutiny's own alert fired on. It also covers freshness, wear and temperature,
  it drives a Kuma up/down tile, and it pages when the collector stops reporting at all.
  The shoutrrr `SCRUTINY_NOTIFY_URLS` path went on 2026-09-28 (#2832) because it was a
  second page for one failed disk, and it added no threshold the bridge lacks.
- **Do not re-add a notify path here without moving `check_scrutiny` out of the way first.**
  Two senders on one threshold is the state #2832 removed. Two upstream traps make re-adding
  it look easier than it is: the env key is top-level `notify.urls`, so
  `SCRUTINY_WEB_NOTIFY_URLS` (the shape the neighbouring `SCRUTINY_WEB_INFLUXDB_*` keys
  suggest) boots clean and notifies nothing; and `notify.level` is deprecated upstream and
  rejected at startup with a `ConfigValidationError`, so `SCRUTINY_NOTIFY_LEVEL` crashloops
  the pod. The level is dashboard state — SQLite in `scrutiny-web-config`, not the repo —
  so nothing in this role can hold it either way.
