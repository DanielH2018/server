# healthchecks — Cron-job monitoring

Healthchecks.io (self-hosted) — dead-man's-switch monitoring for scheduled jobs/backups.
See repo-root `CLAUDE.md` for shared conventions.

## At a glance
- **Image:** `lscr.io/linuxserver/healthchecks:latest`
- **Host:** daniel-server · **Port:** 8000 · **URL:** `healthchecks.<domain>` (Authelia: yes)
- **Networks:** apps (off `monitoring` since 2026-07-02 — docker-proxy sits there; see host_vars comment)
- **Depends on:** traefik, authelia
- **Config in:** `ansible/inventory/host_vars/daniel-server.yml` → `containers_list`

## Notable
- Scheduled jobs ping a unique URL; a missed ping triggers an alert. Here the pinging jobs are
  the **reboot** + **docker-image-prune** host crons (Kopia backup liveness is Kuma-monitored via
  monitor-bridge's `check_backup`, not healthchecks). `SITE_ROOT`/`ALLOWED_HOSTS` must match the
  public URL or pings 400.

## Editing
- Compose: `templates/docker-compose.yml.j2`
- Deploy: `uv run ansible-playbook ansible/deploy.yml --tags "healthchecks"`
