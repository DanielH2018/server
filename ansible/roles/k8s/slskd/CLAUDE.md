# slskd — Soulseek downloads for Lidarr, behind its own Mullvad tunnel

slskd is the Soulseek client. Soularr, a second container in its pod, reads Lidarr's wanted
albums every 5 minutes, searches and downloads them through slskd into `torrents/slskd`, and
asks Lidarr to import them into `media/music`.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's defaults, templates or containers_list entry, or the Longhorn tier lists in ansible/roles/k8s/longhorn-reconcile/defaults/main.yml. -->
- **Applied by:** Flux, through `promote`; `land.sh --tags=slskd` gates the rollout
- **Images:** `slskd/slskd` (`slskd_k8s_image`), `ghcr.io/mrusse/soularr`
  (`slskd_k8s_soularr_image`), `lscr.io/linuxserver/wireguard` (`slskd_k8s_wireguard_image`)
- **Route:** `slskd.<domain>` · `slskd.local.<domain>`, Authelia one_factor
- **Claims:** `slskd-config` (no backup (StorageClass longhorn-nobackup)), `media-data` (not
  Longhorn (media-local))
<!-- /generated_from -->

- **Sharing has one switch:** `slskd_k8s_share_library` in `defaults/main.yml`. `true` shares
  `media/music` read-only at `/music`; `false` shares and mounts nothing of the library. A PR
  that flips it restarts the pod with the new share
  (`ansible/tests/services/test_slskd_share_switch.py`).
- **The tunnel is the shared `ansible/templates/wireguard-sidecar.yml.j2`**, qbittorrent's
  sidecar, with a Mullvad device of its own (`slskd_wireguard_private_key`) on the same account.
  qbittorrent's `CLAUDE.md` has its traps: the kill-switch that outlives a container, the netns
  reset, the modcache lock. The netns reset script is qbittorrent's, read across roles, and so
  is the account Secret `qbittorrent-mullvad-account`: `cluster-vars.source` files a SOPS key
  under one manifest only, so `mullvad_account` cannot ship in a second one.
- **slskd and Soularr run as the sidecar's PUID**, because `netns-reset.sh` fences only that uid
  off `eth0` while the tunnel is down. A container added here at another uid could leak.
- **slskd runs with web auth off.** Authelia gates the UI, the pod's only caller is traefik,
  and Soularr reaches the API over localhost with a placeholder key, which slskd's passthrough
  scheme accepts.
- **Why Soularr and not Tubifarry.** Tubifarry 2.3.1 builds against Lidarr 3.2, which is
  nightly-only, and Lidarr cannot return from nightly to stable without a database restore
  (#8192 has the evidence). Revisit once a stable Lidarr 3.2 ships.
- **`k8s_autodeploy: false`**, as qbittorrent's: a bump that breaks the tunnel gate fails the
  first revision and sets `DEPLOY_HOLD` for the fleet.

## Editing
- Manifests: `templates/`. Soularr's `config.ini` is `templates/config/soularr.ini.j2`, shipped
  in the `slskd-soularr` Secret because it carries Lidarr's API key. Soularr mounts it through
  a subPath, which never sees an update; the restart `restart.py` stamps for a mounted Secret
  is what picks a change up.
- Pins, the share switch, Soularr's interval and file types: `defaults/main.yml`.
