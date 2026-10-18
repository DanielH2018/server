# prowlarr — indexer manager for the *arr stack

Prowlarr, with a `flaresolverr` sidecar that solves Cloudflare challenges for the indexers by
rendering attacker-supplied pages in a headless browser.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's defaults, templates, tasks or containers_list entry, or the k3s role's Longhorn tier lists. -->
- **Applied by:** Flux, through `promote` (`owner: flux`); `deploy.sh` refuses
  `--tags=prowlarr` with exit 5
- **Images:** `lscr.io/linuxserver/prowlarr` (`prowlarr_k8s_image`),
  `ghcr.io/flaresolverr/flaresolverr` (`prowlarr_k8s_flaresolverr_image`),
  `ghcr.io/onedr0p/exportarr` (`prowlarr_k8s_exportarr_image`)
- **Route:** `prowlarr.<domain>` · `prowlarr.local.<domain>`, Authelia one_factor
- **Claim:** `prowlarr-config` (weekly -> B2 (default target))
<!-- /generated_from -->

- **The `-lsNN` linuxserver tag scheme** means a breaking bump can hide as a routine patch
  bump. `prowlarr_k8s_flaresolverr_image` and `prowlarr_k8s_exportarr_image` are sidecars.
- **Port:** 9696
- **Persists:** `prowlarr-config` PVC (`longhorn`, ~84Mi) — indexer definitions and their API
  keys. flaresolverr's browser profile is disposable and sits on an emptyDir.
- **Secrets:** `prowlarr_api_key` (also reused as the exportarr sidecar's credential —
  Prowlarr has no scoped read-only key to mint instead).
- **Flux applies it, not Ansible** (#5594, a Wave D PR with monitor-bridge). Its
  `containers_list` entry carries `owner: flux`, so `deploy.sh`, the play and the GitOps
  deployer all skip it, and a merged change reaches the cluster when `promote` publishes and
  verifies the next bundle. To give it back to Ansible, follow the two-PR handback in
  `docs/flux-deploy-plane.md`; a plain revert of the flag leaves two writers on the objects.
  authelia and traefik stay Ansible-owned, so the Kustomization records them in its
  `homelab/ansible-owned-deps` annotation, not in `dependsOn`.
- **The `prowlarr-snapshot` Job snapshots `prowlarr-config` before each revision applies**
  (`k8s_autodeploy_snapshot_pvcs: [prowlarr-config]`). The prowlarr Kustomization depends on
  it, so a revision applies only after the snapshot is ready. The Deployment is `Recreate`. A
  failed revision writes the stateful hold, and nothing reverts the claim on its own: the
  operator follows the revert runbook in `docs/volume-snapshot-drills.md`.
  `k8s_autodeploy: true` still lets Renovate automerge the image bump.
- **Where the tasks went.** `tasks/main.yml` holds the `k8s/manifests` include, which no Flux
  path runs; it stays for the handback. The Kustomization's `wait` replaces the rollout wait
  for both Deployments. The flaresolverr isolation probe became the `prowlarr-verify` Job,
  and its five tasks and `netpol-probe-job.yaml.j2` were deleted.

## Notable
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
- **Both Deployments wait 780s, above the shared default.** A cold pull on daniel-server took 8m54s for the
  292 MB flaresolverr image and 3m41s for prowlarr's own (#2369). The budget is the literal
  `ansible/roles/k8s/prowlarr/defaults/main.yml:manifests_rollout_timeout`, which Flux's
  `timeout` and the drain's wait read, and each template's `progressDeadlineSeconds` reads it
  through `prowlarr_k8s_rollout_timeout`. The deadline has to move
  with the wait, or `rollout status` fails at the 600s default anyway.
- **flaresolverr's memory limit is sized from a burst, not from steady state.** It launches a
  fresh headless Chromium per solve, so a full deploy that restarts the *arr stack hands a cold
  pod prowlarr's whole indexer re-sync at once. That burst OOM-killed it at the old 1024Mi cap on
  2026-09-28 (#2883) and failed the deploy at the stabilisation gate.
  `ansible/roles/k8s/prowlarr/defaults/main.yml:prowlarr_k8s_fs_mem_limit` carries the
  measurements, and why
  `ansible/roles/k8s/prowlarr/defaults/main.yml:prowlarr_k8s_fs_mem_request` deliberately stays
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
  setting `ansible/roles/k8s/prowlarr/defaults/main.yml:prowlarr_k8s_fs_disable_media` false and
  deploying, then record the refutation beside it.
- **Log churn, not log size, drives PVC growth.** `prowlarr_k8s_log_level`/`_log_rotate`
  override upstream's noisier defaults; see `roles/k8s/sonarr/defaults/main.yml` for the full
  rationale, shared across the *arr roles.

## Editing
- Manifest: `templates/deployment.yaml.j2`, `templates/deployment-flaresolverr.yaml.j2`
