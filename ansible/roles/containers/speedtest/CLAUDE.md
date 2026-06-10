# speedtest — Periodic internet speed tests

Speedtest Tracker — scheduled Ookla speed tests with history/graphs.
See repo-root `CLAUDE.md` for shared conventions.

## At a glance
- **Image:** `lscr.io/linuxserver/speedtest-tracker:latest`
- **Host:** daniel-server · **Port:** 80 · **URL:** `speedtest.<domain>` (Authelia: yes)
- **Networks:** monitoring
- **Depends on:** traefik, authelia
- **Config in:** `ansible/inventory/host_vars/daniel-server.yml` → `containers_list`

## Notable
- Needs an `APP_KEY` (Laravel) from secrets; schedule is set via env cron expression
  (runs in UTC).
- Results are **stored in UTC by design** (`APP_TIMEZONE` left default per upstream docs);
  `DISPLAY_TIMEZONE={{ tz }}` makes the UI show local time.
- Results are also surfaced on Homepage.

## Editing
- Compose: `templates/docker-compose.yml.j2`
- Deploy: `uv run ansible-playbook ansible/deploy.yml --tags "speedtest"`
