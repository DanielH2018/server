# prowlarr — Indexer manager for the *arr apps

Central indexer/tracker manager that feeds Sonarr & Radarr. See repo-root `CLAUDE.md`.

## At a glance
- **Images:** `linuxserver/prowlarr` (version-pinned, Renovate-managed) + `ghcr.io/flaresolverr/flaresolverr:latest`
  (`:latest` but `watchtower.enable=false` — prowlarr `depends_on` it `service_healthy` at boot, so it's
  pinned against unsupervised auto-updates like crowdsec/unbound, the other health-gating sidecars)
- **Host:** daniel-server · **Port:** 9696 · **URL:** `prowlarr.<domain>` (Authelia: yes)
- **Networks:** media
- **Depends on:** traefik, authelia
- **Config in:** `ansible/inventory/host_vars/daniel-server.yml` → `containers_list`

## Notable
- **FlareSolverr sidecar** solves Cloudflare/JS challenges for protected indexers;
  Prowlarr points its FlareSolverr proxy at it.
- Sonarr and Radarr declare Prowlarr as a dependency, so they deploy after it.

## Editing
- Compose: `templates/docker-compose.yml.j2`
- Deploy: `uv run ansible-playbook ansible/deploy.yml --tags "prowlarr"`
