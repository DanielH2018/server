# prowlarr — indexer manager for the *arr stack

Prowlarr, with a `flaresolverr` Deployment beside it
(`k8s/roles/prowlarr/templates/deployment-flaresolverr.yaml.j2`) that solves Cloudflare
challenges for the indexers by rendering attacker-supplied pages in a headless browser.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's defaults, templates or containers_list entry, or the Longhorn tier lists in k8s/roles/longhorn-reconcile/defaults/main.yml. -->
- **Applied by:** Flux, through `promote`; `land.sh --tags=prowlarr` gates the rollout
- **Images:** `lscr.io/linuxserver/prowlarr` (`prowlarr_k8s_image`),
  `ghcr.io/flaresolverr/flaresolverr` (`prowlarr_k8s_flaresolverr_image`),
  `ghcr.io/onedr0p/exportarr` (`prowlarr_k8s_exportarr_image`)
- **Route:** `prowlarr.local.<domain>` (LAN only), Authelia one_factor
- **Claim:** `prowlarr-config` (weekly -> B2 (default target))
<!-- /generated_from -->

- **The `-lsNN` linuxserver tag scheme** means a breaking bump can hide as a routine patch
  bump. `prowlarr_k8s_flaresolverr_image` is its own Deployment, and `prowlarr_k8s_exportarr_image` is a sidecar in the prowlarr pod.
- **Port:** 9696
- **Persists:** `prowlarr-config` PVC (`longhorn`, ~84Mi) — indexer definitions and their API
  keys. flaresolverr's browser profile is disposable and sits on an emptyDir.
- **Secrets:** `prowlarr_api_key` (also reused as the exportarr sidecar's credential —
  Prowlarr has no scoped read-only key to mint instead).
- **Flux applies it.** Its `containers_list`
  entry is in `cluster/services.yml`, so nothing applies it by hand, and a merged change reaches the cluster
  when `promote` publishes and verifies the next bundle. authelia and traefik are flux-owned too,
  so the Kustomization lists them in `dependsOn`, and Flux applies this tag only after their
  Kustomizations are Ready.
- **The `prowlarr-snapshot` Job snapshots `prowlarr-config` before each revision applies**
  (`k8s_autodeploy_snapshot_pvcs: [prowlarr-config]`). The prowlarr Kustomization depends on
  it, so a revision applies only after the snapshot is ready. The Deployment is `Recreate`. A
  failed revision writes the stateful hold, and nothing reverts the claim on its own: the
  operator follows the revert runbook in `docs/volume-snapshot-drills.md`.
  `k8s_autodeploy: true` still lets Renovate automerge the image bump.
- **Its checks.** The Kustomization's `wait` holds both Deployments' rollout. The
  `prowlarr-verify` Kustomization runs the flaresolverr isolation probe and two seed Jobs
  that write the Discord notification and the app API keys.

## Notable
- **Prowlarr keeps its own copy of each app's API key.** Settings → Apps stores the Sonarr,
  Radarr and Lidarr keys in Prowlarr's database. A stale copy fails every indexer push with a
  401 while RSS through Prowlarr keeps working.
- **`templates/verify/app-keys.yaml.j2` writes the SOPS keys into those entries.** The seed
  Job runs `files/seed_prowlarr_app_keys.py` in `prowlarr-verify`, reading each key from the
  app's `<app>-exportarr` Secret. Prowlarr's API masks `apiKey` as `********`, so the script
  cannot compare keys. It sends each entry unchanged to `POST /api/v1/applications/test`, which
  tests the app with the stored key, and PUTs the SOPS key only when that test fails on
  `ApiKey`. A PUT pushes every indexer to the app, so a second run writes nothing
  (`k8s/roles/prowlarr/tests/test_seed_prowlarr_app_keys.py::test_a_second_run_writes_nothing`).
  Any other test failure, or an app with no entry or two, fails the Job without a write. The
  Job's spec embeds the `cluster-vars.source` digest of the three keys and a digest of the
  rendered Deployment. Flux therefore runs it again after a key rotation and after each
  revision that rolls prowlarr. `flux_verify_after` holds `prowlarr-verify` until sonarr,
  radarr and lidarr are Ready.
- **A `prowlarr-config` revert can leave a stale key until the next prowlarr revision.** The
  revert changes no render, so the seed does not run after it.
  `ProwlarrAppSyncFailing` in `k8s/roles/observability/files/rules/arr-apps.yaml` pages once
  Prowlarr's `ApplicationLongTermStatusCheck` has reported an app for 6 hours, read from the
  exportarr sidecar's `prowlarr_system_health_issues` series. To repair it, paste the app's
  key from its Settings → General into Prowlarr's entry for it, then click Sync App Indexers.
- **A revert desyncs indexer IDs.** Unlike sonarr/radarr/bazarr/jellyfin, which self-heal a
  volume revert (rescan, re-download, existence check), Prowlarr pushes indexer config to the
  other *arr apps and they store Prowlarr-assigned IDs back. A rollback needs a manual re-sync.
- **flaresolverr is fenced to ingress from prowlarr alone**, via
  `templates/networkpolicy-flaresolverr.yaml.j2` — a deliberate narrowing from the Compose-era
  isolated network, since egress policies on this cluster don't hold but ingress does.
- **`templates/verify/job.yaml.j2` proves flaresolverr's fence holds.** The renderer emits it
  into `prowlarr-verify`, which Flux applies once `prowlarr` is Ready. The Job runs
  `files/prowlarr_verify.py`: prowlarr's port must answer, as the control, and flaresolverr's
  must refuse. Its pod carries `app: prowlarr-verify`, which prowlarr's `netpol_from` lists
  and flaresolverr's fence must never admit. Its spec embeds a digest of flaresolverr's
  rendered NetworkPolicy and Deployment, so an edit to either makes Flux run it again.
  `job.yaml.j2` carries the caller census, and `tests/test_prowlarr_verify.py` renders the
  directory through the Job census and checks both fences against the pod.
- **`templates/verify/notification.yaml.j2` declares the `Discord (health)` notification.**
  It is arr-notification's seed Job (`k8s/roles/arr-notification/`), which writes the SOPS
  `arr_discord_webhook_url` into the row on every rollout. Prowlarr's all-indexers-down error
  reaches Discord only through that row. `ArrNotificationsFailing` in
  `k8s/roles/observability/files/rules/arr-apps.yaml` pages when its sends fail.
- **Both Deployments wait 780s, above the shared default.** A cold pull on daniel-server took 8m54s for the
  292 MB flaresolverr image and 3m41s for prowlarr's own (#2369). The budget is the literal
  `k8s/roles/prowlarr/defaults/main.yml:manifests_rollout_timeout`, which Flux's
  `timeout` and the drain's wait read, and each template's `progressDeadlineSeconds` reads it
  through `prowlarr_k8s_rollout_timeout`. The deadline has to move
  with the wait, or `rollout status` fails at the 600s default anyway.
- **flaresolverr's memory limit is sized from a burst, not from steady state.** It launches a
  fresh headless Chromium per solve, so a full deploy that restarts the *arr stack hands a cold
  pod prowlarr's whole indexer re-sync at once. That burst OOM-killed it at the old 1024Mi cap on
  2026-09-28 (#2883) and failed the deploy at the stabilisation gate.
  `k8s/roles/prowlarr/defaults/main.yml:prowlarr_k8s_fs_mem_limit` carries the
  measurements, and why
  `k8s/roles/prowlarr/defaults/main.yml:prowlarr_k8s_fs_mem_request` deliberately stays
  far below it. Upstream exposes no concurrency cap, so sizing is the only lever on how many
  browsers run at once.
- **`prowlarr_k8s_fs_disable_media` is the second lever — on how expensive each browser is.**
  It sets FlareSolverr's `DISABLE_MEDIA`, so the browser loads no images, CSS or other media
  while it solves a challenge (#2893). Upstream documents it as a bandwidth saving, not a
  memory one. The 7d measurement confirms that: the peak with the flag on was 1112Mi, above
  the 934Mi recorded without it (#2962). `prowlarr_k8s_fs_mem_limit` therefore stays at
  2048Mi, and the measurement sits beside the flag in the defaults file. The failure mode it
  risks lands on the indexers rather than on this pod: a challenge type needing an image or a
  stylesheet stops solving, and monitor-bridge's `prowlarr_indexers` check pages. Revert by
  setting `k8s/roles/prowlarr/defaults/main.yml:prowlarr_k8s_fs_disable_media` false and
  deploying, then record the refutation beside it.
- **Log churn, not log size, drives PVC growth.** `prowlarr_k8s_log_level`/`_log_rotate`
  override upstream's noisier defaults; see `k8s/roles/sonarr/defaults/main.yml` for the full
  rationale, shared across the *arr roles.

## Editing
- Manifest: `k8s/roles/prowlarr/templates/deployment.yaml.j2`,
  `k8s/roles/prowlarr/templates/deployment-flaresolverr.yaml.j2`
- **The route is LAN-only.** The entry in `cluster/services.yml` sets `public_route: false`,
  and the shared `k8s/templates/ingressroute-default.yaml.j2` renders no public name from it
  whatever `k8s_public_route` says. The operator ruled it on 2026-11-15 (#8514): WireGuard
  covers remote use.
