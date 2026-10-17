# scrutiny — SMART disk monitoring

Scrutiny's web UI plus a collector DaemonSet and an InfluxDB backend that holds the
SMART trend history. See repo-root `CLAUDE.md` for shared conventions.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's defaults, templates, tasks or containers_list entry, or the k3s role's Longhorn tier lists. -->
- **Applied by:** Flux, through `promote` (`owner: flux`); `deploy.sh` refuses
  `--tags=scrutiny` with exit 5
- **Images:** `ghcr.io/analogj/scrutiny` (`scrutiny_k8s_web_image`), `ghcr.io/analogj/scrutiny`
  (`scrutiny_k8s_collector_image`), `influxdb` (`scrutiny_k8s_influxdb_image`)
- **Route:** `scrutiny.<domain>` · `scrutiny.local.<domain>`, Authelia one_factor
- **Claims:** `scrutiny-influxdb-data` (no backup (listed in k3s_longhorn_nobackup_volumes)),
  `scrutiny-web-config` (weekly -> B2 (default target))
<!-- /generated_from -->

- **The Authelia bypass is GET/HEAD only**, on three paths — `/api/summary`, `/api/health`, `/api/device/<wwn>/details` — from the LAN
  and the pod CIDR, configured in the authelia role, not here. It exists for `probe.py
  scrutiny` and the Kuma monitor `k3s Scrutiny`, the only two callers that cross the
  route. Every writer addresses the ClusterIP `http://scrutiny:8080` instead and never
  meets Authelia: the collector DaemonSet (`COLLECTOR_API_ENDPOINT`), monitor-bridge
  (`SCRUTINY_URL`) and homelab-mcp. Scrutiny's web app has no auth of its own, so a wider
  bypass would hand the LAN `DELETE /api/device/:uuid` and `POST /api/settings`.
- **`scrutiny-influxdb-data`** (2Gi) is deliberately not backed up. It holds only the SMART
  time series, which the daily collector runs rebuild; the reason sits beside the
  `no_backup_claims` key of this role's `containers_list` entry in
  `ansible/inventory/host_vars/daniel-box.yml`, which `k3s_longhorn_nobackup_volumes` derives
  from.
  **`scrutiny-web-config`** (1Gi) is the SQLite config DB: device metadata and notification
  settings. Both claims are `k8s_claims` entries in `defaults/main.yml`.
- **The rolling branch tags** are `master-web` and `master-collector`.
- **Renovate does not automerge its image bumps** (`k8s_autodeploy: false`). The images ride
  rolling branch tags on a stateful monitor, and the InfluxDB and web Deployments are
  `Recreate` with RWO claims, so a bump that rolls either takes the UI and API down briefly.
- **Flux applies it, not Ansible** (D5.8, Wave A'). Its `containers_list` entry carries
  `owner: flux`, so `deploy.sh`, the play and the GitOps deployer all skip it, and a merged
  change reaches the cluster when `promote` publishes and verifies the next bundle. Its
  image-bump PRs therefore draw no `land-wait` and no `probe.py health` run. To give it back to
  Ansible, follow the two-PR handback in `docs/flux-deploy-plane.md`; a plain revert of the
  flag leaves two writers on the objects. Both dependencies, authelia and traefik, stay
  Ansible-owned, so the Kustomization records them in its `homelab/ansible-owned-deps`
  annotation, not in `dependsOn`.
- **Where the task went.** `tasks/main.yml` holds one task, the include of `k8s/manifests`,
  and no Flux path runs it. It stays for the handback. `manifests_rollout: scrutiny-web` named
  the web Deployment for the stabilisation gate and the shared restart. The Kustomization's
  `wait` replaces the gate, and the restart checksum the renderer stamps on `scrutiny-web` replaces the
  restart. The two `manifests_extra_rollouts` entries declare `restart_on: []`, and the
  renderer honours it, so neither InfluxDB nor the collector carries the checksum, and neither
  rolls on a config or Secret change. `manifests_exclude_files: [service.yaml]` still keeps
  the shared default Service out of the render.
- **The credentials Secret substitutes from `homelab-cluster-vars`.**
  `scrutiny_influxdb_admin_password` and `scrutiny_influxdb_token` are `${<key>}`
  placeholders in the published render. Flux substitutes the values from the
  `homelab-cluster-vars` Secret, which it decrypts from `ansible/vars/cluster-vars.sops.env`.
  The Kustomization depends on `cluster-vars`, so a rotated value is live before scrutiny
  reconciles. A rotation reaches the cluster only through `cluster_vars.py write`. The
  operator runs it after a hand rotation, and the rotation cron runs it after it rotates an
  `auto`-tier key; either way the new value and the cluster file land in one commit. The
  rotation rolls `scrutiny-web` through its checksum. It does not roll InfluxDB, which reads
  its credentials only at first-run setup, as under Ansible.

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
  port and the file's place in the role's resolved `manifests_files`. The policy carries no `netpol_baseline_enforced` branch:
  that lever is a netpol-baseline role default and role defaults are role-scoped, so it does not
  resolve here.
- The images are pinned by digest on a **rolling tag**, matching the retired Docker
  copy's policy: Renovate can raise a digest PR for a new commit on the same tag, but
  cannot move the tag itself. A tag move stays a deliberate, supervised change: its PR is
  reviewed and merged by hand, and `promote` applies it.
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
