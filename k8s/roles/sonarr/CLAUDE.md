# sonarr — TV library management (\*arr)

Sonarr with an exportarr metrics sidecar and a striptracks mod
(`k8s/roles/sonarr/defaults/main.yml:sonarr_k8s_docker_mods`). Shares the `media-data`
RWX volume with the rest of the \*arr stack
(`k8s/roles/sonarr/defaults/main.yml:sonarr_k8s_media_claim`). See repo-root `CLAUDE.md` for shared
conventions.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's defaults, templates or containers_list entry, or the Longhorn tier lists in k8s/roles/longhorn-reconcile/defaults/main.yml. -->
- **Applied by:** Flux, through `promote`; `land.sh --tags=sonarr` gates the rollout
- **Images:** `lscr.io/linuxserver/sonarr` (`sonarr_k8s_image`), `ghcr.io/onedr0p/exportarr`
  (`sonarr_k8s_exportarr_image`)
- **Route:** `sonarr.<domain>` · `sonarr.local.<domain>`, Authelia one_factor
- **Claims:** `sonarr-config` (weekly -> B2 (default target)), `media-data` (not Longhorn
  (media-local))
<!-- /generated_from -->

- **`sonarr-config`** (2Gi, `longhorn`, backed up) holds the library DB and absolute
  root-folder paths; `media-data` is the shared claim (mounted, not owned).
- **Flux applies it.** Its
  `containers_list` entry is in `k8s/services.yml`, so nothing applies it by hand, and a merged change
  reaches the cluster when `promote` publishes and verifies the next bundle. Its image-bump PRs
  therefore draw no `land-wait` and no `probe.py health` run. authelia, media-volume and traefik
  are flux-owned too, so the Kustomization lists them in `dependsOn`, and Flux applies this tag
  only after their Kustomizations are Ready. The Deployment is `Recreate`, so a change that rolls
  it takes the service down briefly.
- **The `sonarr-snapshot` Job snapshots `sonarr-config` before each revision applies**.
  The sonarr Kustomization depends on `sonarr-snapshot`, so a revision applies only
  after the snapshot is ready; `docs/flux-deploy-plane.md` lists the Job's checks. A failed
  revision that changes sonarr writes the stateful hold, and `promote` does not reset `prod`,
  because the failed image may already have migrated the database. Nothing reverts the claim
  on its own: the operator follows the revert runbook in `docs/volume-snapshot-drills.md`.
- **The Sonarr pin is the v5 beta, and its bumps are never automerged.** LinuxServer publishes
  v5 as `develop-5.0.0.N-lsNN`. A renovate.json rule matches only that channel and major, so a
  v4 tag is never offered, and a bump waits for a tick on the dependency dashboard.
  `k8s_autodeploy: true` governs the role's other pins. *Rolling back Sonarr* below has the way
  back to v4, which needs the volume as well as the image.
- **`media-data` is neither snapshotted nor reverted** (`k8s_autodeploy_unreverted_claims`). A
  revert can desync import/rename history from files left in place, recoverable by a library
  rescan.
- **Its snapshot and checks.** The `sonarr-snapshot` Job takes the pre-apply snapshot, and
  the Kustomization's `wait` holds the rollout. The `sonarr-verify` Job runs the library
  reads. arr-notification's seed Job beside it declares the Discord notification
  (*Notable*). The seed Job's `lookup()`s are what make sonarr a caller of arr-notification
  in `scripts/lib/k8s_role_callers.py:role_callers` (#5561).

## Notable
- `sonarr_k8s_log_level: info` / `rotate: "10"` overrides upstream's debug-level default
  on purpose: Longhorn backs up allocated blocks, and a directory that rewrites 50MB of
  debug logs in a rolling window ships that churn forever.
- The exportarr sidecar's image tag is pinned in this role's own `defaults/main.yml`
  (not `group_vars`, so Renovate's k8s-images manager can see it) and kept in lockstep
  with radarr and prowlarr's copies by
  `tests/services/test_exportarr_sidecars.py::test_the_image_pins_stay_in_lockstep`.
- Sonarr is the only *arr whose sidecar gets `--enable-additional-metrics`
  (`additional_metrics=true` on the shared `exportarr` macro). The flag turns on the episode
  collector that publishes `sonarr_episode_monitored_total`, `_unmonitored_total` and
  `_quality_total`. It costs two extra Sonarr API calls per series per scrape, so the
  parameter exists to keep it off radarr and prowlarr, whose collectors have no block behind
  it. `test_only_sonarr_enables_the_additional_metrics_collector` asserts both halves.
- **`templates/deployment.yaml.j2` is one call to a shared macro.** The body sonarr and
  radarr share lives in `k8s/templates/arr-deployment.yml.j2` (#2871); the two roles
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
  the directory, the ServiceAccount and the pod label; `k8s/roles/arr-notification/CLAUDE.md`
  has its design.
- **The air-date Release Profile rejects fakes that race the real release.**
  `templates/verify/release-profile.yaml.j2` is a seed Job in `sonarr-verify` that declares a
  Sonarr Release Profile with the air-date restriction on. Sonarr then rejects a release whose
  indexer publish date is more than `sonarr_k8s_air_date_grace_hours` before its episode airs,
  and an episode with no air date. Series tagged with a `sonarr_k8s_air_date_excluded_tags`
  entry are exempt. `files/seed_sonarr_release_profile.py` has the rule and why anime is
  excluded.
  - **The seed owns the profile, not configarr.** configarr refuses a negative grace; the
    `DECIDED:` note in the template says what adding `release_profiles` to configarr would do.
  - **It writes through the v5 API.** A profile written through v3 never applies; the
    script's docstring says why.
  - **To exempt a series**, add a tag to `sonarr_k8s_air_date_excluded_tags`, tag the
    series in Sonarr and deploy. A rejected release shows on the episode's interactive search
    with the air-date reason.
- **The rollout budget is 660s.** A first boot installing the striptracks DOCKER_MODS takes up
  to 10 minutes. `k8s/roles/sonarr/defaults/main.yml:manifests_rollout_timeout` is the
  one place that budget is written, as a literal role default because the renderer reads Flux's
  `timeout` only from one (#5464). The template's `progressDeadlineSeconds` reads it through
  `sonarr_k8s_rollout_timeout`. The deadline has to move with the budget, or the rollout fails
  at the 600s Kubernetes default whatever the wait allows (#2370).

## Rolling back Sonarr

Sonarr v5 migrates the database one way: migration 207 moves the size limits into the quality
profiles and drops their old columns. A rollback to v4 therefore puts back the volume and the
image together. Every step is operator-only.

1. Suspend `sonarr-snapshot`, then `sonarr`, with the command in step 1 of
   `docs/volume-snapshot-drills.md`, *Branch 1*. On a stateful hold, follow that runbook
   instead of the steps below.
2. Take the digest8 from the pre-v5 snapshot's name. The move to v5 applied on 2026-11-25
   (PR #10143) behind `autodeploy-sonarr-978c1de9-config-20261125215041`, so its digest8 is
   `978c1de9`. List the names with
   `kubectl -n longhorn-system get snapshots.longhorn.io | grep autodeploy-sonarr`. The Job
   keeps the newest three, so three later sonarr revisions prune it.
3. Revert the volume. Drop `--apply` first to see which snapshot the run names:

    ```bash
    sudo .venv/bin/python -B scripts/backup/volume_revert.py --service sonarr --namespace homelab \
      --sha <digest8> --claim sonarr-config --apply
    ```

4. Land a revert of the pin to `4.0.20.3014-ls329`
   (`sha256:44b06045a98bc1f237b9ffefc4b28dab64cb29305e4a2a94875ae93b5224e4cc`), with the
   renovate.json rule reverted too.
5. Once `homelab` holds that revision, resume `sonarr-snapshot`, then `sonarr`.

If the snapshot is gone, restore the last B2 weekly backup taken before v5
(`backup-656b27f018934d75`, 2026-11-21) instead; its age is the data lost. Sonarr's own
zips under `/config/Backups` sit on the same volume, so they cover a bad database but not a
lost volume. A v4 zip there restores through Sonarr's UI only on a v4 image.
