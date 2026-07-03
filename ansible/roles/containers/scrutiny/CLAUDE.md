# scrutiny — Hard-drive SMART monitoring

Scrutiny web UI + collector, backed by InfluxDB. See repo-root `CLAUDE.md`.

## At a glance
- **Images:** `ghcr.io/analogj/scrutiny:master-web` + `:master-collector` + `influxdb:2.9`
- **Host:** daniel-server · **Port:** 8080 · **URL:** `scrutiny.<domain>` (Authelia: yes)
- **Networks:** monitoring
- **Depends on:** traefik, authelia
- **Config in:** `ansible/inventory/host_vars/daniel-server.yml` → `containers_list`

## Notable
- The **collector** needs host disk access (SMART data) — it maps physical devices /
  runs with the privileges required to read `smartctl`. Verify drives appear after deploy.
- InfluxDB stores the time-series SMART history.
- **Collector liveness is monitored via monitor-bridge** ("SMART Data Freshness": every
  device must report within 26 h via the web `/api/summary`). The collector itself has no
  Docker healthcheck on purpose — cron is PID 1 (death self-heals via restart) and its
  only meaningful failure mode is silently-aging data, which the bridge check catches.
- **Update policy = manual (watchtower opted OUT).** `master-web`/`master-collector` are
  rolling upstream-branch tags; `scrutiny-web` + `scrutiny-collector` carry
  `com.centurylinklabs.watchtower.enable=false` so a stateful monitoring service isn't fed
  unsupervised `master` builds. Renovate can't version-track a branch tag either, so updating
  is a deliberate `deploy.yml --tags scrutiny -e common_pull=always` — the `-e` is REQUIRED:
  a plain redeploy never re-pulls a tag that's already present locally
  (docker_compose_v2's default pull policy; see common/tasks/docker_deploy.yml), so without
  it "update by redeploy" is a silent no-op. `influxdb:2.9` stays on the
  pinned-major non-critical tier (watchtower patches within 2.9). The compose CI guard's
  mutable-tag check catches the `master-` PREFIX form (not just `-stable` suffixes), so a
  future rolling tag here can't slip the update-policy decision again.

## Editing
- Compose: `templates/docker-compose.yml.j2`
- Deploy: `uv run ansible-playbook ansible/deploy.yml --tags "scrutiny"`
