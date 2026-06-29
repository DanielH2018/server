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
- **`redeploy_cron.yml`** — weekly Sunday-06:00 redeploy cron for roles with locally-built
  images (code-server :00, n8n :05, crowdsec :10, peanut :15, ical-proxy :20); Watchtower
  can't update those, and the GitOps deployer only redeploys *changed* roles. Callers pass
  `common_redeploy_cron_minute` to stagger the jobs. The job runs through
  `~/.local/bin/uv run` (absolute path — cron's PATH has no ansible-playbook, and the bare
  uv-tool shim lacks the `community.docker` deps) and logs failures via
  `logger -t redeploy-cron`.

> **Why `recreate` is conditional (not hardcoded `always`).** Deploys are idempotent: a
> no-op `deploy.yml` recreates nothing; editing one config recreates only that service.
> A role that bind-mounts an Ansible-templated config file `register:`s those config tasks
> and passes `common_config_changed` (an **`include_role` var, not a `set_fact`** — a fact
> would leak across the `deploy.yml` role loop and wrongly recreate later services). When a
> config changed → `recreate: always` picks it up; otherwise `recreate: auto`. `auto` alone
> would silently *not* apply a config-file-only edit (the compose config-hash is unchanged),
> but it *does* handle image changes (`build: always` rebuilds; identical rebuild = no-op)
> and `docker-compose.yml` edits. Wired roles (current set:
> `grep -rl common_config_changed roles/containers/*/tasks/`): authelia, traefik, homepage,
> grafana, prometheus, janitorr, livesync, peanut, recyclarr, kopia (entrypoint.sh), pihole
> (resolver configs; its former `absent`→`present` exemption was removed 2026-06-09),
> freshrss (nginx feed-cache conf), home-assistant, monitor-bridge, mosquitto, terraria,
> terraria-stats, zigbee2mqtt. Design:
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
- **Task block tags (fleet-wide convention, 2026-06-11):** every container-role task
  carries `config` (dirs/templates/files/host config), `deploy` (the docker_deploy
  include + post-deploy container ops), or `cron` (scheduled jobs), placed right under
  `name:`. deploy.yml's `apply.tags` adds the service tag, and Ansible tags UNION, so
  scoping is subtractive: `--tags <svc> --skip-tags deploy` = config-only. Rules: a
  register feeding another block carries that block's tag too (e.g. pihole's base-path
  set_fact is `[config, deploy]`); `--skip-tags config` is unsupported — the
  `common_config_changed` registers (see above) feed the recreate decision. Tasks inside
  common/*.yml need no tags of their own — they inherit from the include task.

## Editing
Changing `docker_deploy.yml` affects **every** service's deploy — test with `--check`.
