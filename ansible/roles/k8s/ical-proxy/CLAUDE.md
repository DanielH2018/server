# ical-proxy — iCal feed aggregation proxy

Small Flask app that merges several ICS calendars into one feed for the Homepage
calendar widget. See repo-root `CLAUDE.md` for shared conventions.

## At a glance
- **Image:** built from `templates/Dockerfile.j2` (Flask app in `files/app.py`)
- **Host:** daniel-server · **Port:** `false` — **internal only, no Traefik route**
- **Authelia:** no
- **Networks:** homepage_private (reachable only by Homepage)
- **Depends on:** traefik
- **Config in:** `ansible/inventory/host_vars/daniel-server.yml` → `containers_list`

## Notable
- Aggregates Google + Obsidian ICS URLs (`calendar_1/2/4` from secrets), refreshing every
  15 min. Not exposed publicly — Homepage consumes it over the private network.
- Image is built — update via redeploy, not Watchtower; a weekly Sunday rebuild cron
  (06:20, via `common/redeploy_cron.yml`) pulls the newest `python:3.14-slim` base.

## Editing
- Compose: `templates/docker-compose.yml.j2` · App: `files/app.py`, `templates/Dockerfile.j2`
- Deploy: `uv run ansible-playbook ansible/deploy.yml --tags "ical-proxy"`
