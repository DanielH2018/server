# recyclarr — Sonarr/Radarr quality-profile syncer

Syncs TRaSH-guide quality definitions and custom formats into Sonarr & Radarr.
See repo-root `CLAUDE.md` for shared conventions.

## At a glance
- **Image:** `ghcr.io/recyclarr/recyclarr:latest`
- **Host:** daniel-server · **No web UI**, no Authelia (runs on a schedule)
- **Networks:** media
- **Depends on:** traefik, **sonarr, radarr**
- **Config in:** `ansible/inventory/host_vars/daniel-server.yml` → `containers_list`

## Notable
- All behaviour lives in `templates/recyclarr.yml.j2` — the per-instance API keys and
  which profiles/formats to sync. Talks to Sonarr/Radarr over the `media` network.

## Editing
- Compose: `templates/docker-compose.yml.j2` · Sync config: `templates/recyclarr.yml.j2`
- Deploy: `ansible-playbook ansible/deploy.yml --tags "recyclarr"`
