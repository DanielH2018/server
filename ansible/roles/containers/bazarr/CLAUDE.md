# bazarr — Subtitle manager for Sonarr & Radarr

See repo-root `CLAUDE.md` for shared conventions.

## At a glance
- **Image:** `lscr.io/linuxserver/bazarr:latest`
- **Host:** daniel-server · **Port:** 6767 · **URL:** `bazarr.<domain>` (Authelia: yes)
- **Networks:** media
- **Depends on:** traefik, authelia, **sonarr, radarr**
- **Config in:** `ansible/inventory/host_vars/daniel-server.yml` → `containers_list`

## Notable
- Pulls the library lists from Sonarr/Radarr (deploys after them) and writes subtitle
  files alongside media in the shared `data/media` tree.

## Editing
- Compose: `templates/docker-compose.yml.j2`
- Deploy: `ansible-playbook ansible/deploy.yml --tags "bazarr"`
