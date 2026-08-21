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

## Traps

### A dockerd restart permanently breaks docker-proxy
Restarting `dockerd` replaces `/var/run/docker.sock` with a new inode. A container that
bind-mounts the socket keeps the old inode, which has no listener, so the proxy fails
permanently. A plain restart re-mounts nothing, so `autoheal` — which only restarts
unhealthy containers — is structurally blind to this and the outage persists silently.

Observed 2026-08-07 on daniel-server: all four proxies (`docker-proxy`,
`docker-proxy-portainer`, `docker-proxy-codeserver`, `docker-proxy-lifecycle`) sat
`Up 5 days (unhealthy)` for ~1.5 h. haproxy logs flip from `200` to `SH--` (502, server
hangup) then steadily to `SC--` (503, server connection error) at exactly the socket's new
mtime.

The decisive one-line test compares the inode on the host and inside the container:
`stat -c %i /var/run/docker.sock` returned `2834440` vs `1565`.

The blast radius is wide because so much reads the daemon through these proxies. AutoKuma
stops syncing, so monitors are silently never created — a `push failed … Monitor not found
or not active` in `check.py --once` is the tell. Kuma's ~40 `docker`-type monitors go blind,
and promtail's docker_sd stream stops.

If AutoKuma logs `Docker responded with status code 503`, or a newly-added Kuma monitor
never appears, check the inode pair before reading the proxy's config. Fix each proxy with
`docker compose -f <dir>/docker-compose.yml up -d --force-recreate` —
`docker-proxy-portainer` lives in `containers/portainer/`, the other three in
`containers/docker-proxy/`.

That four-proxy observation predates the 2026-08-14 Docker retirement on daniel-server. On
daniel-pi, the only remaining Docker host, this role renders `docker-proxy` and
`docker-proxy-lifecycle` — the mechanism is unchanged, the recreate list is shorter.
