# lidarr — music library manager

Lidarr, the music member of the *arr stack. It finds albums through Prowlarr, downloads them
through qBittorrent, and imports them by hardlink into `/data/media/music`, which navidrome
serves read-only.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's defaults, templates or containers_list entry, or the Longhorn tier lists in k8s/roles/longhorn-reconcile/defaults/main.yml. -->
- **Applied by:** Flux, through `promote`; `land.sh --tags=lidarr` gates the rollout
- **Images:** `lscr.io/linuxserver/lidarr` (`lidarr_k8s_image`), `ghcr.io/onedr0p/exportarr`
  (`lidarr_k8s_exportarr_image`)
- **Route:** `lidarr.<domain>` · `lidarr.local.<domain>`, Authelia one_factor
- **Claims:** `lidarr-config` (weekly -> B2 (default target)), `media-data` (not Longhorn
  (media-local))
<!-- /generated_from -->

- **Stable branch only.** `lidarr_k8s_image` tracks LSIO's stable stream. Lidarr cannot move
  from nightly or the plugins branch back to stable without restoring its database, so a
  Soulseek plugin that needs another branch is a decision for #8192, not an image bump.
- **The API key comes from SOPS.** `lidarr_api_key` reaches the pod through the
  `lidarr-exportarr` Secret, and the shared macro's `api_key_secret` argument sets
  `LIDARR__AUTH__APIKEY` from it. Lidarr reads `Lidarr:Auth:ApiKey` before its own
  `config.xml`, so the key in SOPS is the key Lidarr uses, and exportarr and Prowlarr can hold
  it before Lidarr first starts.
- **The `lidarr-snapshot` Job snapshots `lidarr-config` before each revision applies**, as
  radarr's does, and `media-data` is mounted but never reverted
  (`k8s_autodeploy_unreverted_claims`).
- **Mounts all of `media-data` at `/data`.** The root folder is `/data/media/music`, and
  downloads land under `/data/torrents/lidarr`, so an import is a hardlink.

## Notable
- **Not wired yet, and why.** arr-notification's Discord seed speaks `/api/v3`, and Lidarr
  serves `/api/v1`, so this role ships no `templates/verify/` seed. Homepage's tiles read each
  app's key by name, so lidarr has no tile. The quality profiles decided on #8190 are set in the
  UI until a seed Job declares them.
- **Lidarr's own state lives in `lidarr.db`:** the root folder, the qBittorrent download client
  and the quality profiles are configured in the UI and survive through the snapshot, not git.

## Editing
- Manifest: `templates/deployment.yaml.j2`, a single call to
  `k8s/templates/arr-deployment.yml.j2`, the macro radarr and sonarr render from.
- Image pin, Renovate stance, resources and log bounds: `defaults/main.yml`.
