# radarr — movie library manager

Radarr, part of the *arr stack. Imports by hardlinking from `/data/torrents` into
`/data/media`, so it mounts the whole `media-data` tree at `/data` — a hardlink only works
when both paths share one mount.

## At a glance
- **Image:** `radarr_k8s_image` (`-lsNN` linuxserver scheme — a breaking bump can hide as a
  routine patch bump), plus an `exportarr` metrics sidecar (`radarr_exportarr_image`, pinned
  in lockstep with sonarr and prowlarr — `test_exportarr_pins_in_lockstep.py` enforces it).
- **Route:** `radarr.<domain>` · Authelia · port 7878
- **Persists:** `radarr-config` PVC (`longhorn`, ~30Mi) — `radarr.db` holds the library and
  absolute root-folder paths. Also mounts `media-data` (`radarr_k8s_media_claim`), shared RWX.
- **`radarr-striptracks`** Docker mod strips unwanted audio/subtitle tracks on import.
- **Deploy tag:** `--tags "radarr"`. `k8s_autodeploy: true`, `Recreate` strategy — protected by
  a pre-apply Longhorn snapshot (`k8s_autodeploy_snapshot_pvcs: [radarr-config]`) that
  `k8s/manifests` reverts to on a failed deploy.

## Notable
- **A revert desyncs import history, not files.** `media-data` is declared in
  `k8s_autodeploy_unreverted_claims` — it's mounted but never reverted, so a rollback rewinds
  Radarr's own import/rename history while the files on `media-data` stay put. Recoverable by
  a library rescan, not automatic.
- **Log churn, not log size, drives PVC growth** — `radarr_k8s_log_level`/`_log_rotate` cap
  upstream's noisier defaults; see `roles/k8s/sonarr/defaults/main.yml` for the shared
  rationale across all three *arr roles.

## Editing
- Manifest: `templates/deployment.yaml.j2`
- Deploy: `uv run ansible-playbook ansible/deploy.yml --tags "radarr"`
