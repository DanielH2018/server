# radarr — movie library manager

Radarr, part of the *arr stack. Imports by hardlinking from `/data/torrents` into
`/data/media`, so it mounts the whole `media-data` tree at `/data` — a hardlink only works
when both paths share one mount.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's defaults, templates, tasks or containers_list entry, or the k3s role's Longhorn tier lists. -->
- **Deploy tag:** `--tags "radarr"`
- **Images:** `lscr.io/linuxserver/radarr` (`radarr_k8s_image`), `ghcr.io/onedr0p/exportarr`
  (`radarr_exportarr_image`)
- **Route:** `radarr.<domain>` · `radarr.local.<domain>`, Authelia one_factor
- **Claims:** `radarr-config` (weekly -> B2 (default target)), `media-data` (not Longhorn
  (media-local))
- **Auto-deploy:** eligible (`k8s_autodeploy: true`)
<!-- /generated_from -->

- **The `-lsNN` linuxserver tag scheme** means a breaking bump can hide as a routine patch
  bump. The `exportarr` metrics sidecar (`radarr_exportarr_image`) is pinned in lockstep with
  sonarr and prowlarr — `test_exportarr_pins_in_lockstep.py` enforces it.
- **Port:** 7878
- **Persists:** `radarr-config` PVC (`longhorn`, ~30Mi) — `radarr.db` holds the library and
  absolute root-folder paths. Also mounts `media-data` (`radarr_k8s_media_claim`), shared RWX.
- **`radarr-striptracks`** Docker mod strips unwanted audio/subtitle tracks on import.
- **`Recreate` strategy, auto-deployed anyway** — protected by a pre-apply Longhorn snapshot (`k8s_autodeploy_snapshot_pvcs: [radarr-config]`) that
  `k8s/manifests` reverts to on a failed deploy.

## Notable
- **A revert desyncs import history, not files.** `media-data` is declared in
  `k8s_autodeploy_unreverted_claims` — it's mounted but never reverted, so a rollback rewinds
  Radarr's own import/rename history while the files on `media-data` stay put. Recoverable by
  a library rescan, not automatic.
- **The rollout waits 660s, above the shared default.** A first boot installing DOCKER_MODS takes up to 10
  minutes. `radarr_k8s_rollout_timeout` is the one place that budget is written; both
  `manifests_rollout_timeout` and the template's `progressDeadlineSeconds` read it. The
  deadline has to move with the budget, or `rollout status` fails at the 600s Kubernetes
  default whatever `--timeout` says (#2370).
- **Log churn, not log size, drives PVC growth** — `radarr_k8s_log_level`/`_log_rotate` cap
  upstream's noisier defaults; see `roles/k8s/sonarr/defaults/main.yml` for the shared
  rationale across all three *arr roles.

## Editing
- Manifest: `templates/deployment.yaml.j2`
- Deploy: `uv run ansible-playbook ansible/deploy.yml --tags "radarr"`
