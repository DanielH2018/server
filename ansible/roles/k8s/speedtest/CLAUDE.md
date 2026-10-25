# speedtest — speedtest-tracker

`linuxserver/speedtest-tracker`, a periodic self-hosted speed test with history. See
repo-root `CLAUDE.md` for shared conventions.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's defaults, templates, tasks or containers_list entry, or the Longhorn tier lists in ansible/roles/k8s/longhorn-reconcile/defaults/main.yml. -->
- **Applied by:** Flux, through `promote` (`owner: flux`); `deploy.sh` refuses
  `--tags=speedtest` with exit 5
- **Image:** `lscr.io/linuxserver/speedtest-tracker` (`speedtest_k8s_image`)
- **Route:** `speedtest.<domain>` · `speedtest.local.<domain>`, Authelia one_factor
- **Claim:** `speedtest-config` (no backup (StorageClass longhorn-nobackup))
<!-- /generated_from -->

- **`speedtest-config` is `longhorn-nobackup`**, 1Gi. `/config` holds a real
  `database.sqlite`, but its Laravel `APP_KEY` lives in SOPS so a rebuilt instance still
  works — losing the volume loses history, not function.
- **Flux applies it, not Ansible** (D5.9, the first Wave B PR). Its `containers_list` entry
  carries `owner: flux`, so `deploy.sh`, the play and the GitOps deployer all skip it, and a
  merged change reaches the cluster when `promote` publishes and verifies the next bundle. Its
  image-bump PRs therefore draw no `land-wait` and no `probe.py health` run. To give it back to
  Ansible, follow the two-PR handback in `docs/flux-deploy-plane.md`; a plain revert of the
  flag leaves two writers on the objects. Both dependencies, authelia and traefik, stay
  Ansible-owned, so the Kustomization records them in its `homelab/ansible-owned-deps`
  annotation, not in `dependsOn`. The Deployment is `Recreate`, so a change that rolls it
  takes the service down briefly.
- **The `speedtest-snapshot` Job snapshots `speedtest-config` before each revision applies**
  (D5.2a). The speedtest Kustomization depends on `speedtest-snapshot`, so a revision applies
  only after the snapshot is ready; `docs/flux-deploy-plane.md` lists the Job's checks. The
  snapshot is Longhorn-local, because the claim is `longhorn-nobackup`. A failed revision that
  changes speedtest writes the stateful hold, and `promote` does not reset `prod`. Nothing
  reverts the claim on its own: the operator follows the revert runbook in
  `docs/volume-snapshot-drills.md`. `k8s_autodeploy: true` still lets Renovate automerge the
  digest bump. speedtest is the role D5's Wave B revert drill breaks on purpose.
- **The APP_KEY Secret substitutes from `homelab-cluster-vars`.** `speedtest_app_key` is a
  `${<key>}` placeholder in the published render. Flux substitutes the value from the
  `homelab-cluster-vars` Secret, which it decrypts from `ansible/vars/cluster-vars.sops.env`.
  The Kustomization depends on `cluster-vars`, so a rotated value is live before speedtest
  reconciles. A rotation reaches the cluster only through `cluster_vars.py write`, which the
  operator runs after a hand rotation and the rotation cron runs after it rotates an
  `auto`-tier key. The renderer stamps a restart checksum on the Deployment, so a changed
  value rolls the pod.
- **Where the task went.** `tasks/main.yml` holds one task, the include of `k8s/manifests`,
  and no Flux path runs it. It stays for the handback. Its pre-apply snapshot through
  `k8s/volume-snapshot` became the `speedtest-snapshot` Job, and the Kustomization's `wait`
  replaces the rollout wait.
- **REQUIRED one-time post-deploy step (#996):** log in at `speedtest.<domain>`, open
  **Settings -> Data Integration**, and turn on **Prometheus**. Without this the
  `speedtest` scrape job in observability's `prometheus.yaml.j2` returns 404 forever — see
  below for why this cannot be set any other way.

## Notable
- Was the original slice-1 auto-deploy pilot, paused for the same `Recreate` + RWO PVC
  shape every other \*arr-adjacent role was held back for; the snapshot/revert machinery
  added in slice 7b is what let it re-enable.
- The image is pinned `tag@sha256` rather than a bare digest, deliberately: Renovate's
  k8s-defaults manager tracks the tag to raise digest-bump PRs, and a bare `@sha256`
  pin would freeze with no update signal.
- **Prometheus scrape (#996) needs the manual UI toggle above — there is no declarative
  alternative, checked at the exact pinned build.** monitor-bridge's `speedtest` check
  reads the app's REST API and pushes a verdict to Kuma, which keeps a tile, not a
  series — a degradation (the 2026-09-03 78.8 Mbps DOWN, itself below the worst-ever
  119 Mbps this pinned server had returned) had no history. The image natively exposes
  `/prometheus` (`speedtest_tracker_download_bits`, `_upload_bits`, `_ping_ms`, etc.,
  labeled by server/ISP), scraped by observability's `prometheus.yaml.j2`
  (`job_name: speedtest`).

  The pin (`defaults/main.yml`) resolves to upstream `alexjustesen/speedtest-tracker`
  tag `v1.14.7`, LSIO build `ls166` (confirmed via the image's
  `org.opencontainers.image.version` label, not inferred from the tag string). At that
  exact tag: `config/settings.php` sets `default_repository => database` with no env
  override wired in; `app/Settings/DataIntegrationSettings.php` has no `env()` calls;
  the `prometheus_enabled` migration hardcodes its `false` default rather than reading
  one; `MetricsController::__invoke()` gates on `$this->settings->prometheus_enabled`
  with no query-param or header bypass; and no artisan command in the app touches
  `prometheus_enabled` (a repo-wide code search for the string turns up only the UI
  page, the controller, the settings class and its migration/tests). The LSIO wrapper's
  `init-speedtest-tracker-config` script (same file, byte-identical, at both `main` and
  the pinned commit) seeds `DB_CONNECTION`, `APP_KEY` and migrations from env — nothing
  Prometheus-related. This role has no precedent for seeding an app's DB-backed setting
  either (`tasks/main.yml` only applies manifests) — unlike
  `SPEEDTEST_SCHEDULE` and the other `SPEEDTEST_*` env vars in
  `templates/deployment.yaml.j2`, which the app *does* read directly.

  Until the toggle is flipped, the scrape target 404s and reads `up == 0` — loud, not a
  silently-missing series. `prometheus_allowed_ips` can stay empty: blank means "allow
  any caller", so the in-cluster scrape needs no IP entered once the toggle is on.
