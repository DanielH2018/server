# autoheal — Restarts unhealthy containers

Watches Docker healthchecks and restarts any container reporting `unhealthy`.
See repo-root `CLAUDE.md` for shared conventions.

## At a glance
- **Image:** `willfarrell/autoheal:latest`
- **Host:** daniel-server · **No web UI**, no Authelia
- **Networks:** lifecycle only (reaches the write-capable `docker-proxy-lifecycle`,
  not the broad networks)
- **Depends on:** docker-proxy
- **Config in:** `ansible/inventory/host_vars/daniel-server.yml` → `containers_list`

## Notable
- Runs with `AUTOHEAL_CONTAINER_LABEL=all`, so it monitors **every** container that defines
  a `healthcheck` (no `autoheal=true` label required) and restarts any that report
  `unhealthy`. Corollary: a service is only self-healing if its healthcheck actually fails
  when it's broken — e.g. qBittorrent's check probes external egress, not just loopback, so
  an orphaned VPN netns is caught (see the `qbittorrent` role).

## Editing
- Compose: `templates/docker-compose.yml.j2`
- Deploy: `uv run ansible-playbook ansible/deploy.yml --tags "autoheal"`
