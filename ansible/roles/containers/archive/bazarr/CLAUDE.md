# bazarr — Subtitle manager for Sonarr & Radarr

See repo-root `CLAUDE.md` for shared conventions.

## At a glance
- **Image:** `lscr.io/linuxserver/bazarr` (version-pinned, Renovate-managed)
- **Host:** daniel-server · **Port:** 6767 · **URL:** `bazarr.<domain>` (Authelia: yes)
- **Networks:** media
- **Depends on:** traefik, authelia, **sonarr, radarr**
- **Config in:** `ansible/inventory/host_vars/daniel-server.yml` → `containers_list`

## Notable
- Pulls the library lists from Sonarr/Radarr (deploys after them) and writes subtitle
  files alongside media in the shared `data/media` tree.
- **Mounts `containers/data/media` at `/data/media`** (scoped from the whole-tree `/data`
  mount, 2026-07-02 security review): the in-container paths Sonarr/Radarr report still
  resolve identically, but Bazarr no longer sees `torrents/`, which it never needs. Bazarr
  itself only reads/writes subtitles (no hardlinking) — the single-whole-tree-mount EXDEV
  requirement applies to Sonarr/Radarr's import path only.

## Editing
- Compose: `templates/docker-compose.yml.j2`
- Deploy: `uv run ansible-playbook ansible/deploy.yml --tags "bazarr"`
