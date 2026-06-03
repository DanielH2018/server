# portainer — Docker container management UI

See repo-root `CLAUDE.md` for shared conventions.

## At a glance
- **Images:** `portainer/portainer-ce:alpine` + `lscr.io/linuxserver/socket-proxy:latest` (sidecar)
- **Hosts:** **daniel-server AND daniel-pi** (defined in both host_vars files)
- **Port:** 9000 · **URL:** `portainer.<domain>`
- **Authelia:** yes on server, **no on the Pi** (Pi is LAN-only)
- **Networks:** proxy
- **Depends on:** traefik, authelia

## Notable
- Reaches Docker through its own socket-proxy sidecar rather than mounting the raw
  `docker.sock` directly.
- Same role serves both hosts; per-host port/authelia come from each
  `inventory/host_vars/<host>.yml` → `containers_list`.

## Editing
- Compose: `templates/docker-compose.yml.j2`
- Deploy: `ansible-playbook ansible/deploy.yml --tags "portainer"` (add `--limit daniel-pi` for the Pi)
