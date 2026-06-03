# healthchecks — Cron-job monitoring

Healthchecks.io (self-hosted) — dead-man's-switch monitoring for scheduled jobs/backups.
See repo-root `CLAUDE.md` for shared conventions.

## At a glance
- **Image:** `lscr.io/linuxserver/healthchecks`
- **Host:** daniel-server · **Port:** 8000 · **URL:** `healthchecks.<domain>` (Authelia: yes)
- **Networks:** monitoring
- **Depends on:** traefik, authelia
- **Config in:** `ansible/inventory/host_vars/daniel-server.yml` → `containers_list`

## Notable
- Jobs (e.g. Kopia backups, cron tasks) ping a unique URL; a missed ping triggers an
  alert. `SITE_ROOT`/`ALLOWED_HOSTS` must match the public URL or pings 400.

## Editing
- Compose: `templates/docker-compose.yml.j2`
- Deploy: `ansible-playbook ansible/deploy.yml --tags "healthchecks"`
