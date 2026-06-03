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
- **`dns.yml`** — idempotently ensures a Cloudflare **CNAME** (`<name>.{{ domain }}`,
  proxied) exists via the Cloudflare API. Currently commented out in most roles' tasks
  (DNS is handled by a wildcard / `cloudflare-ddns`), kept for per-record use.

## Notable
- `container_item` (name/port/networks/hostname/use_authelia) is the per-service dict from
  `containers_list`; the deploy loop in `deploy.yml` sets it for each role.
- No `meta/deps.yml` deps — it's a pure utility, ordered first implicitly.

## Editing
Changing `docker_deploy.yml` affects **every** service's deploy — test with `--check`.
