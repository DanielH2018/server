# radarr — Movie download manager

Part of the *arr media stack. See repo-root `CLAUDE.md` for shared conventions.

## At a glance
- **Image:** `lscr.io/linuxserver/radarr` (version-pinned, Renovate-managed)
- **Host:** daniel-server · **Port:** 7878 · **URL:** `radarr.<domain>` (Authelia: yes)
- **Networks:** media
- **Depends on:** traefik, authelia, **prowlarr** (indexers)
- **Config in:** `ansible/inventory/host_vars/daniel-server.yml` → `containers_list`

## Notable
- Gets indexers from Prowlarr, sends downloads to qBittorrent, and is consumed by
  Bazarr / Recyclarr / Janitorr. Shares the `media` network and the `data/media` tree.

## Editing
- Compose: `templates/docker-compose.yml.j2`
- Deploy: `ansible-playbook ansible/deploy.yml --tags "radarr"`
