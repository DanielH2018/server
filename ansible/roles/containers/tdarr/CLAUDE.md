# tdarr — Distributed video transcoding

Tdarr server + node for automated library transcoding/health checks.
See repo-root `CLAUDE.md` for shared conventions.

## At a glance
- **Image:** `ghcr.io/haveagitgat/tdarr:latest`
- **Host:** daniel-server · **Port:** 8265 · **URL:** `tdarr.<domain>` (Authelia: yes)
- **Networks:** media
- **Depends on:** traefik, authelia
- **Config in:** `ansible/inventory/host_vars/daniel-server.yml` → `containers_list`

## Notable
- Operates on the shared `data/media` tree; can use the Intel iGPU for transcodes
  (same `/dev/dri` device class as Jellyfin) if configured.
- **Weekly cron cleans `transcode_cache/`** (Mon 05:45, `-mtime +7`): failed/interrupted
  jobs orphan `tdarr-workDir2-*` dirs forever — 36 GB had piled up by 2026-06-11. The
  cache is kopiaignore-excluded (regenerable), so week-old leftovers are safe to drop.

## Editing
- Compose: `templates/docker-compose.yml.j2`
- Deploy: `uv run ansible-playbook ansible/deploy.yml --tags "tdarr"`
