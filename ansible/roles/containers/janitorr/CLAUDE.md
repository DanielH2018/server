# janitorr — Automated media library cleanup

Deletes watched/old media and cleans up Sonarr/Radarr based on disk-usage rules.
See repo-root `CLAUDE.md` for shared conventions.

## At a glance
- **Image:** `ghcr.io/schaka/janitorr:jvm-stable`
- **Host:** daniel-server · **No web UI**, no Authelia (background service)
- **Networks:** media
- **Depends on:** traefik, authelia, **sonarr, radarr**
- **Config in:** `ansible/inventory/host_vars/daniel-server.yml` → `containers_list`

## Notable
- Behaviour (retention rules, leaving-soon thresholds, dry-run flag) lives in
  `templates/application.yml.j2`. **It deletes files** — `dry-run` was flipped off
  2026-06-10 (operator decision after the initial trial period), so it now cleans for
  real. Tag media `janitorr_keep` in the *arrs to exempt it.
- Mounts the whole `containers/data` tree at `/data` (same as qBittorrent/Sonarr/Radarr/
  Bazarr since the 2026-07-02 hardlink-mount unification). No `application.yml.j2`
  path-mapping config exists or is needed: janitorr acts on media via the Sonarr/Radarr
  APIs, not by resolving arr-reported filesystem paths itself, and its own direct
  filesystem use (`leaving-soon-dir`, `free-space-check-dir`) is already `/data`-relative.

## Editing
- Compose: `templates/docker-compose.yml.j2` · Rules: `templates/application.yml.j2`
- Deploy: `uv run ansible-playbook ansible/deploy.yml --tags "janitorr"`
