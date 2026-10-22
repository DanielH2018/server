# radarr — movie library manager

Radarr, part of the *arr stack. Imports by hardlinking from `/data/torrents` into
`/data/media`, so it mounts the whole `media-data` tree at `/data` — a hardlink only works
when both paths share one mount.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's defaults, templates, tasks or containers_list entry, or the k3s role's Longhorn tier lists. -->
- **Applied by:** Flux, through `promote` (`owner: flux`); `deploy.sh` refuses `--tags=radarr`
  with exit 5
- **Images:** `lscr.io/linuxserver/radarr` (`radarr_k8s_image`), `ghcr.io/onedr0p/exportarr`
  (`radarr_k8s_exportarr_image`)
- **Route:** `radarr.<domain>` · `radarr.local.<domain>`, Authelia one_factor
- **Claims:** `radarr-config` (weekly -> B2 (default target)), `media-data` (not Longhorn
  (media-local))
<!-- /generated_from -->

- **The `-lsNN` linuxserver tag scheme** means a breaking bump can hide as a routine patch
  bump. The `exportarr` metrics sidecar (`radarr_k8s_exportarr_image`) is pinned in lockstep with
  sonarr and prowlarr — `ansible/tests/services/test_exportarr_sidecars.py::test_the_image_pins_stay_in_lockstep`
  enforces it.
- **Port:** 7878
- **Persists:** `radarr-config` PVC (`longhorn`, ~30Mi) — `radarr.db` holds the library and
  absolute root-folder paths. Also mounts `media-data` (`radarr_k8s_media_claim`), shared RWX.
- **`radarr-striptracks`** Docker mod strips unwanted audio/subtitle tracks on import.
- **Flux applies it, not Ansible** (D5.20, a Wave D PR with sonarr and mosquitto). Its
  `containers_list` entry carries `owner: flux`, so `deploy.sh`, the play and the GitOps
  deployer all skip it, and a merged change reaches the cluster when `promote` publishes and
  verifies the next bundle. Its image-bump PRs therefore draw no `land-wait` and no `probe.py
  health` run. To give it back to Ansible, follow the two-PR handback in
  `docs/flux-deploy-plane.md`; a plain revert of the flag leaves two writers on the objects.
  authelia, media-volume and traefik stay Ansible-owned, so the Kustomization records them in
  its `homelab/ansible-owned-deps` annotation, not in `dependsOn`. The Deployment is
  `Recreate`, so a change that rolls it takes the service down briefly.
- **The `radarr-snapshot` Job snapshots `radarr-config` before each revision applies**
  (D5.2a). The radarr Kustomization depends on `radarr-snapshot`, so a revision applies only
  after the snapshot is ready; `docs/flux-deploy-plane.md` lists the Job's checks. A failed
  revision that changes radarr writes the stateful hold, and `promote` does not reset `prod`,
  because the failed image may already have migrated the database. Nothing reverts the claim
  on its own: the operator follows the revert runbook in `docs/volume-snapshot-drills.md`.
  `k8s_autodeploy: true` still lets Renovate automerge the image bump, which is safe for the
  same reason it was under Ansible: a bad swap can be reverted to the snapshot.
- **`media-data` is neither snapshotted nor reverted** (`k8s_autodeploy_unreverted_claims`). A
  revert can desync import/rename history from files left in place, recoverable by a library
  rescan.
- **Where the tasks went.** `tasks/main.yml` holds one include, `k8s/manifests`, and no Flux
  path runs it. It stays for the handback. The pre-apply snapshot through `k8s/volume-snapshot`
  became the `radarr-snapshot` Job, and the Kustomization's `wait` replaces the rollout wait.
  arr-notification's seed Job in `radarr-verify` declares the Discord notification
  (*Notable*). Its `k8s/arr-notification` include went in #5561, so a handback re-adds it as
  well as reverting the flip. The seed Job's `lookup()`s are what make radarr a caller of
  arr-notification in `scripts/lib/k8s_role_callers.py:role_callers`.

## Notable
- **The rollout waits 660s, above the shared default.** A first boot installing DOCKER_MODS takes up to 10
  minutes. `ansible/roles/k8s/radarr/defaults/main.yml:manifests_rollout_timeout` is the one place
  that budget is written, as a literal role default because the renderer reads Flux's
  `timeout` only from one (#5464). The template's
  `progressDeadlineSeconds` reads it through `radarr_k8s_rollout_timeout` — see
  `ansible/roles/k8s/sonarr/CLAUDE.md` for why the deadline has to move with the budget (#2370).
- **`templates/verify/` holds arr-notification's seed Job.** The renderer emits it into
  `radarr-verify`, which holds only that Job and its `radarr-verify` ServiceAccount: radarr had
  no Ansible verify tasks to convert. The Job's pod carries `app: radarr-verify`, which
  radarr's `netpol_from` lists (#5535). `ansible/roles/k8s/arr-notification/CLAUDE.md` has the Job's design.
- **Log churn, not log size, drives PVC growth** — `radarr_k8s_log_level`/`_log_rotate` cap
  upstream's noisier defaults; see `roles/k8s/sonarr/defaults/main.yml` for the shared
  rationale across all three *arr roles.

## Editing
- Manifest: `templates/deployment.yaml.j2` — a single call to the shared
  `ansible/templates/arr-deployment.yml.j2` macro, which radarr and sonarr both render from
  (#2871). Change the body there; change an argument here. The macro's header says why
  prowlarr is not a caller.
