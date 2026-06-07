# common — Shared helper role for container deploys

Utility role (not a container). Every container role calls into it via
`include_role: { name: common, tasks_from: ... }`. See repo-root `CLAUDE.md`.

## Tasks (entry points)
- **`setup_dirs.yml`** — creates host directories under
  `/home/{{ sys_user }}/server/containers/<...>`. Callers pass the list as
  `common_dirs_to_create` (e.g. a service's `config` dir and `data/media/*`).
- **`docker_deploy.yml`** — the standard deploy path: templates
  `templates/docker-compose.yml.j2` → `containers/<name>/docker-compose.yml` (mode `0600`),
  then runs `community.docker.docker_compose_v2` with `build: always`,
  `recreate: "{{ 'always' if common_config_changed | default(false) else 'auto' }}"`,
  `remove_orphans: true`. **This is why `containers/` is generated/read-only.**

> **Why `recreate` is conditional (not hardcoded `always`).** Deploys are idempotent: a
> no-op `deploy.yml` recreates nothing; editing one config recreates only that service.
> A role that bind-mounts an Ansible-templated config file `register:`s those config tasks
> and passes `common_config_changed` (an **`include_role` var, not a `set_fact`** — a fact
> would leak across the `deploy.yml` role loop and wrongly recreate later services). When a
> config changed → `recreate: always` picks it up; otherwise `recreate: auto`. `auto` alone
> would silently *not* apply a config-file-only edit (the compose config-hash is unchanged),
> but it *does* handle image changes (`build: always` rebuilds; identical rebuild = no-op)
> and `docker-compose.yml` edits. Wired roles: authelia, traefik, homepage, grafana,
> prometheus, janitorr, livesync, peanut, recyclarr, kopia (entrypoint.sh). **pihole is
> exempt** (its own
> `absent`→`present` DNS-bootstrap flow, not `docker_deploy`). Design:
> `docs/superpowers/specs/2026-06-07-idempotent-deploys-conditional-recreate-design.md`.
>
> **New config-mounting role?** `register:` each bind-mounted config task with a
> `<role>_`-prefixed name and pass `common_config_changed: "{{ <reg> is changed }}"` (OR
> several) on the `docker_deploy` include — otherwise edits to that config won't recreate.

> Note: DNS is handled by a wildcard record / `cloudflare-ddns`; the former per-record
> `dns.yml` helper (Cloudflare CNAME via API) was removed as dead code on 2026-06-05.

## Notable
- `container_item` (name/port/networks/hostname/use_authelia) is the per-service dict from
  `containers_list`; the deploy loop in `deploy.yml` sets it for each role.
- No `meta/deps.yml` deps — it's a pure utility, ordered first implicitly.

## Editing
Changing `docker_deploy.yml` affects **every** service's deploy — test with `--check`.
