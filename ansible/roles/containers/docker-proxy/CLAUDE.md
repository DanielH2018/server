# docker-proxy — Docker socket proxy

Gives other containers safe, scoped access to the Docker API instead of mounting the
raw socket. See repo-root `CLAUDE.md` for shared conventions.

## At a glance
- **Image:** `lscr.io/linuxserver/socket-proxy:latest` (two instances)
- **Host:** daniel-server · **No web UI**, no Authelia
- **Networks:** proxy, monitoring, apps (+ a `lifecycle` write proxy)
- **Depends on:** nothing (consumed by other roles)
- **Config in:** `ansible/inventory/host_vars/daniel-server.yml` → `containers_list`

## Notable
- Read-only proxy serves monitoring consumers (e.g. AutoKuma in `uptime-kuma`, Homepage).
- A separate **`docker-proxy-lifecycle`** (write-capable) proxy sits on the `lifecycle`
  network and is what `autoheal` and `watchtower` talk to — so those two never join the
  broad networks.

## Editing
- Compose: `templates/docker-compose.yml.j2`
- Deploy: `uv run ansible-playbook ansible/deploy.yml --tags "docker-proxy"`
