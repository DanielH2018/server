# uptime-kuma — Service uptime monitoring

Uptime Kuma with an AutoKuma sidecar that auto-creates monitors from container labels.
See repo-root `CLAUDE.md` for shared conventions.

## At a glance
- **Images:** `louislam/uptime-kuma:2` + `ghcr.io/bigboot/autokuma:latest`
- **Host:** daniel-server · **Port:** 3001 · **URL:** `uptime-kuma.<domain>` (Authelia: yes)
- **Networks:** monitoring
- **Depends on:** traefik, authelia, **docker-proxy**
- **Config in:** `ansible/inventory/host_vars/daniel-server.yml` → `containers_list`

## Notable
- **AutoKuma** reads the `kuma(...)` labels every service's compose emits (via
  `templates/autokuma.yml.j2`) and provisions monitors automatically — through the
  read-only `docker-proxy` socket. That's why almost every role imports the kuma macro.

## Editing
- Compose: `templates/docker-compose.yml.j2`
- Deploy: `ansible-playbook ansible/deploy.yml --tags "uptime-kuma"`
