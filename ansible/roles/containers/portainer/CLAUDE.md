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
- The role is host-agnostic (per-host port/authelia come from `containers_list`), but it is
  currently only in daniel-server's list — the Pi runs its own lighter docker-proxy stack.

## Editing
- Compose: `templates/docker-compose.yml.j2`
- Deploy: `uv run ansible-playbook ansible/deploy.yml --tags "portainer"`
