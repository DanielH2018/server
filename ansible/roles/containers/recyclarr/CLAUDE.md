# recyclarr — Sonarr/Radarr quality-profile syncer

Syncs TRaSH-guide quality definitions and custom formats into Sonarr & Radarr.
See repo-root `CLAUDE.md` for shared conventions.

## At a glance
- **Image:** `ghcr.io/recyclarr/recyclarr` (version-pinned, Renovate-managed — pinned
  2026-06-10 after an unsupervised `:latest` → v8 major broke the nightly sync)
- **Host:** daniel-server · **No web UI**, no Authelia (runs on a schedule)
- **Networks:** media
- **Depends on:** traefik, **sonarr, radarr**
- **Config in:** `ansible/inventory/host_vars/daniel-server.yml` → `containers_list`

## Notable
- All behaviour lives in `templates/recyclarr.yml.j2` — the per-instance API keys and
  which profiles to sync. Talks to Sonarr/Radarr over the `media` network.
- **v8 config format**: guide-backed `quality_profiles: - trash_id: …` (v8 removed all
  `include:` templates upstream; the old include-based config failed every nightly sync
  with "Unable to find include template" — invisible, since the healthcheck only watches
  the supercronic scheduler). Sync failures only show in `docker logs recyclarr`.
- The Sonarr **"Anime" profile is deliberately NOT managed** (operator's own; opt-in
  trash_ids are in the template comment). Pre-existing same-name profiles need
  `recyclarr state repair --adopt` before v8 will sync onto them.
- `recyclarr config create` writes starter files into `/config/configs/`, which recyclarr
  **auto-loads** — leftover placeholders break the sync; delete them after use.

## Editing
- Compose: `templates/docker-compose.yml.j2` · Sync config: `templates/recyclarr.yml.j2`
- Deploy: `uv run ansible-playbook ansible/deploy.yml --tags "recyclarr"`
