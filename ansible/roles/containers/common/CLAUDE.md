# common — Shared helper role for container deploys

Utility role (not a container). Every container role calls into it via
`include_role: { name: common, tasks_from: ... }`. See repo-root `CLAUDE.md`.

## Tasks (entry points)
- **`setup_dirs.yml`** — creates host directories under
  `/home/{{ sys_user }}/server/containers/<...>`. Callers pass the list as
  `common_dirs_to_create` (e.g. a service's `config` dir and `data/media/*`).
- **`docker_deploy.yml`** — the standard deploy path: templates
  `templates/docker-compose.yml.j2` → `containers/<name>/docker-compose.yml` (mode `0600`),
  then runs `community.docker.docker_compose_v2` with `build: always`, `recreate: always`,
  `remove_orphans: true`. **This is why `containers/` is generated/read-only.**

> **Why `recreate: always` (and not `auto`)?** It's intentional and load-bearing — see
> the long comment in `docker_deploy.yml`. With no notify/handler pattern in the repo,
> always-recreate is what propagates edits to the ~11 bind-mounted config templates
> (authelia, traefik, pihole, homepage, grafana, prometheus, janitorr, livesync, peanut,
> recyclarr, qbittorrent). `auto` would be idempotent but silently *not* apply a
> config-file-only edit (the compose config-hash is unchanged). Switching to `auto` safely
> needs per-service `config-hash` labels or handlers, not a one-line change.

> Note: DNS is handled by a wildcard record / `cloudflare-ddns`; the former per-record
> `dns.yml` helper (Cloudflare CNAME via API) was removed as dead code on 2026-06-05.

## Notable
- `container_item` (name/port/networks/hostname/use_authelia) is the per-service dict from
  `containers_list`; the deploy loop in `deploy.yml` sets it for each role.
- No `meta/deps.yml` deps — it's a pure utility, ordered first implicitly.

## Editing
Changing `docker_deploy.yml` affects **every** service's deploy — test with `--check`.
