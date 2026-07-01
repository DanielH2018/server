# docker-proxy — Docker socket proxy

Gives other containers safe, scoped access to the Docker API instead of mounting the
raw socket. See repo-root `CLAUDE.md` for shared conventions.

## At a glance
- **Image:** `lscr.io/linuxserver/socket-proxy:latest` (three instances on daniel-server)
- **Hosts:** daniel-server AND daniel-pi (host-agnostic; listed in both `containers_list`s) · **No web UI**, no Authelia
- **Networks:** proxy, monitoring (+ a `lifecycle` write proxy, + a `codeserver` read proxy)
- **Depends on:** nothing (consumed by other roles)
- **Config in:** each `ansible/inventory/host_vars/<host>.yml` → `containers_list`

## Notable
- Read-only proxy serves monitoring consumers (e.g. AutoKuma in `uptime-kuma`, Homepage).
- A separate **`docker-proxy-lifecycle`** (write-capable) proxy sits on the `lifecycle`
  network and is what `autoheal` and `watchtower` talk to — so those two never join the
  broad networks.
- A third **`docker-proxy-codeserver`** (read-only) proxy sits on the private `codeserver`
  network, shared only with `code-server`. **Security M1 (2026-07-01): the shared read-only
  `docker-proxy` was taken OFF `apps`** — with `CONTAINERS=1`, `GET /containers/{id}/json`
  returns every container's `Env` (secrets), and haproxy can't body-filter a response, so the
  only control is *who can reach the proxy*. code-server was the sole apps-side consumer (its
  in-IDE `docker` CLI via `DOCKER_HOST`), so it got this dedicated proxy and the app fleet on
  `apps` can no longer enumerate other containers' secrets. (Residual: `monitoring`-net
  consumers still can — accepted; they're infra, not the internet-facing app tier.)

## Editing
- Compose: `templates/docker-compose.yml.j2`
- Deploy: `uv run ansible-playbook ansible/deploy.yml --tags "docker-proxy"`
