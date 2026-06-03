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
- Only acts on containers labelled `autoheal=true`; relies on each service defining a
  `healthcheck`.

## Editing
- Compose: `templates/docker-compose.yml.j2`
- Deploy: `ansible-playbook ansible/deploy.yml --tags "autoheal"`
