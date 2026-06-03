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
  `templates/application.yml.j2`. **It deletes files** — keep `dryRun` on until verified.

## Editing
- Compose: `templates/docker-compose.yml.j2` · Rules: `templates/application.yml.j2`
- Deploy: `ansible-playbook ansible/deploy.yml --tags "janitorr"`
