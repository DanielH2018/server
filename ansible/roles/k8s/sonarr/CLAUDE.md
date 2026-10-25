# sonarr — TV library management (\*arr)

Sonarr with an exportarr metrics sidecar and a striptracks mod. Shares the `media-data`
RWX volume with the rest of the \*arr stack. See repo-root `CLAUDE.md` for shared
conventions.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's defaults, templates, tasks or containers_list entry, or the k3s role's Longhorn tier lists. -->
- **Applied by:** Flux, through `promote` (`owner: flux`); `deploy.sh` refuses `--tags=sonarr`
  with exit 5
- **Images:** `lscr.io/linuxserver/sonarr` (`sonarr_k8s_image`), `ghcr.io/onedr0p/exportarr`
  (`sonarr_k8s_exportarr_image`), `python` (`sonarr_k8s_air_date_guard_image`)
- **Route:** `sonarr.<domain>` · `sonarr.local.<domain>`, Authelia one_factor
- **Claims:** `sonarr-config` (weekly -> B2 (default target)), `media-data` (not Longhorn
  (media-local))
<!-- /generated_from -->

- **`sonarr-config`** (2Gi, `longhorn`, backed up) holds the library DB and absolute
  root-folder paths; `media-data` is the shared claim (mounted, not owned).
- **Flux applies it, not Ansible** (D5.20, a Wave D PR with radarr and mosquitto). Its
  `containers_list` entry carries `owner: flux`, so `deploy.sh`, the play and the GitOps
  deployer all skip it, and a merged change reaches the cluster when `promote` publishes and
  verifies the next bundle. Its image-bump PRs therefore draw no `land-wait` and no `probe.py
  health` run. To give it back to Ansible, follow the two-PR handback in
  `docs/flux-deploy-plane.md`; a plain revert of the flag leaves two writers on the objects.
  authelia, media-volume and traefik stay Ansible-owned, so the Kustomization records them in
  its `homelab/ansible-owned-deps` annotation, not in `dependsOn`. The Deployment is
  `Recreate`, so a change that rolls it takes the service down briefly.
- **The `sonarr-snapshot` Job snapshots `sonarr-config` before each revision applies**
  (D5.2a). The sonarr Kustomization depends on `sonarr-snapshot`, so a revision applies only
  after the snapshot is ready; `docs/flux-deploy-plane.md` lists the Job's checks. A failed
  revision that changes sonarr writes the stateful hold, and `promote` does not reset `prod`,
  because the failed image may already have migrated the database. Nothing reverts the claim
  on its own: the operator follows the revert runbook in `docs/volume-snapshot-drills.md`.
  `k8s_autodeploy: true` still lets Renovate automerge the image bump, which is safe for the
  same reason it was under Ansible: a bad swap can be reverted to the snapshot.
- **`media-data` is neither snapshotted nor reverted** (`k8s_autodeploy_unreverted_claims`). A
  revert can desync import/rename history from files left in place, recoverable by a library
  rescan.
- **Where the tasks went.** `tasks/main.yml` holds one include, `k8s/manifests`, and no Flux
  path runs it. It stays for the handback. The pre-apply snapshot through `k8s/volume-snapshot`
  became the `sonarr-snapshot` Job, and the Kustomization's `wait` replaces the rollout wait.
  The library reads of `tasks/verify.yml`, deleted here, became the `sonarr-verify` Job.
  arr-notification's seed Job beside it declares the Discord notification (*Notable*). Its
  `k8s/arr-notification` include went in #5561, so a handback re-adds it as well as reverting
  the flip. The seed Job's `lookup()`s are what make sonarr a caller of arr-notification in
  `scripts/lib/k8s_role_callers.py:role_callers`.

## Notable
- `sonarr_k8s_log_level: info` / `rotate: "10"` overrides upstream's debug-level default
  on purpose: Longhorn backs up allocated blocks, and a directory that rewrites 50MB of
  debug logs in a rolling window ships that churn forever.
- The exportarr sidecar's image tag is pinned in this role's own `defaults/main.yml`
  (not `group_vars`, so Renovate's k8s-images manager can see it) and kept in lockstep
  with radarr and prowlarr's copies by
  `ansible/tests/services/test_exportarr_sidecars.py::test_the_image_pins_stay_in_lockstep`.
- Sonarr is the only *arr whose sidecar gets `--enable-additional-metrics`
  (`additional_metrics=true` on the shared `exportarr` macro). The flag turns on the episode
  collector that publishes `sonarr_episode_monitored_total`, `_unmonitored_total` and
  `_quality_total`. It costs two extra Sonarr API calls per series per scrape, so the
  parameter exists to keep it off radarr and prowlarr, whose collectors have no block behind
  it. `test_only_sonarr_enables_the_additional_metrics_collector` asserts both halves.
- **`templates/deployment.yaml.j2` is one call to a shared macro.** The body sonarr and
  radarr share lives in `ansible/templates/arr-deployment.yml.j2` (#2871); the two roles
  differ only in the arguments they pass. `additional_metrics=true` is sonarr's alone.
- **`templates/verify/job.yaml.j2` is sonarr's verify Job, which proves the library loaded.**
  A running pod alone proves nothing about a broken import. The renderer emits the Job into
  `sonarr-verify`, which Flux applies once `sonarr` is Ready. It runs `files/sonarr_verify.py`,
  which reads the root folders and the series through the Service's cluster name, not the
  ingress, which Authelia would intercept. Every root folder must be accessible under `/data/`,
  and the series must measure files through the mount. Its first connection goes through the
  `flux_job_connect` helper. It reads the key from the `sonarr-exportarr` Secret; the
  `DECIDED:` note in `job.yaml.j2` says why. Its pod carries `app: sonarr-verify`, which
  sonarr's `netpol_from` lists (#5535), so the fence admits it. `tests/test_sonarr_verify.py`
  renders the directory, runs the Job census over it, and runs the rendered script against a
  fixture API. `notification.yaml.j2` beside it is arr-notification's seed Job, which shares
  the directory, the ServiceAccount and the pod label; `ansible/roles/k8s/arr-notification/CLAUDE.md`
  has its design.
- **`sonarr-air-date-guard` (CronJob, every 5 minutes) removes fakes that race the real
  release.** It applies Sonarr v5's "do not download before air date" rule after the grab:
  a queued download whose release was published more than
  `sonarr_k8s_air_date_guard_grace_hours` before its episode airs is removed from qBittorrent
  and blocklisted. Anime series, series tagged `allow-early` and episodes with no air date
  are skipped. `files/sonarr_air_date_guard.py` has the rule and the backtest behind each
  exception. It is a stopgap: retire it once Sonarr v5 stable ships and a Release Profile
  carries the same setting.
  - **Its gate Job runs a real pass on every sonarr revision.** `flux_cronjob_gates` copies
    it into `sonarr-verify` as a mutation gate, so Renovate's image bumps run it too. A
    second run removes nothing
    (`ansible/roles/k8s/sonarr/tests/test_sonarr_air_date_guard.py::test_a_second_run_removes_nothing`).
  - **Its pod carries `app: sonarr-verify`**, the label `netpol_from` already admits, so the
    fence needed no inventory change.
  - **To exempt one series without a deploy**, add the `allow-early` tag to it in Sonarr. To
    make every run a dry-run report, set `sonarr_k8s_air_date_guard_apply: false`. To
    restore a release it blocklisted by mistake, remove it from Sonarr's Activity →
    Blocklist.
- **The rollout budget is 660s.** A first boot installing the striptracks DOCKER_MODS takes up
  to 10 minutes. `ansible/roles/k8s/sonarr/defaults/main.yml:manifests_rollout_timeout` is the
  one place that budget is written, as a literal role default because the renderer reads Flux's
  `timeout` only from one (#5464). The template's `progressDeadlineSeconds` reads it through
  `sonarr_k8s_rollout_timeout`. The deadline has to move with the budget, or the rollout fails
  at the 600s Kubernetes default whatever the wait allows (#2370).
