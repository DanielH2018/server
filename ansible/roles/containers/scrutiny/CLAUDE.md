# scrutiny — Hard-drive SMART monitoring

Scrutiny web UI + collector, backed by InfluxDB. See repo-root `CLAUDE.md`.

## At a glance
- **Images:** `ghcr.io/analogj/scrutiny:master-web` + `:master-collector` + `influxdb:2.2`
- **Host:** daniel-server · **Port:** 8080 · **URL:** `scrutiny.<domain>` (Authelia: yes)
- **Networks:** monitoring
- **Depends on:** traefik, authelia
- **Config in:** `ansible/inventory/host_vars/daniel-server.yml` → `containers_list`

## Notable
- The **collector** needs host disk access (SMART data) — it maps physical devices /
  runs with the privileges required to read `smartctl`. Verify drives appear after deploy.
- InfluxDB stores the time-series SMART history.

## Editing
- Compose: `templates/docker-compose.yml.j2`
- Deploy: `uv run ansible-playbook ansible/deploy.yml --tags "scrutiny"`
