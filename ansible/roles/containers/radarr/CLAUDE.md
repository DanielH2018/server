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
  Bazarr / Configarr / Janitorr. Shares the `media` network.
- **Mounts the whole `containers/data` tree at `/data`** (not a separate `/movies` +
  `/downloads`), same as Sonarr (qBittorrent and Bazarr mount scoped subtrees of it) — a single shared bind mount is
  required for `copyUsingHardlinks` imports: `link()` returns `EXDEV` across separate
  bind mounts even when they're on the same filesystem. Root folder is
  `/data/media/movies`; qBittorrent lands downloads under `/data/torrents/`.

## Editing
- Compose: `templates/docker-compose.yml.j2`
- Deploy: `uv run ansible-playbook ansible/deploy.yml --tags "radarr"`
