---
name: new-container
description: Scaffold a new DOCKER service for daniel-pi — creates the Ansible role, docker-compose template, and registers it in the Pi's containers_list (deploy tags derive automatically). New cluster services go to ansible/roles/k8s/ instead; this skill does not scaffold those.
allowed-tools: Read, Write, Edit, Glob, Bash, WebFetch, WebSearch
---

> **STOP — check the target host first.** Since the k3s migration completed (2026-08-14),
> `daniel-server` and `daniel-box` have **no Docker at all**. A Compose role scaffolded for
> either host deploys nothing. This skill applies **only to `daniel-pi`** (LAN-only utilities,
> WireGuard).
>
> For anything on the cluster — which is where nearly every new service belongs — do NOT use
> this skill. Create a role under `ansible/roles/k8s/<name>/` rendering Deployment / Service /
> IngressRoute / PVC manifests (copy the shape from `roles/k8s/freshrss` for a plain web app,
> or `roles/k8s/sonarr` for one on the media volume), then add a `containers_list` entry in
> `host_vars/daniel-box.yml` with `platform: k8s`, positioned **after** `traefik` (CRDs) and
> after `authelia` if the route uses its middleware — that play runs in list order with no
> toposort. See repo-root `CLAUDE.md` → "Adding a New Service".

Scaffold a new Docker service for `daniel-pi` following the existing patterns.

**First, ask:** Is this a brand-new container being built from scratch (custom Dockerfile, local build), or an existing image being pulled from a registry (e.g. Docker Hub, GHCR)?

- **Existing image** — proceed with the questions below and scaffold normally.
- **Custom/from-scratch** — also ask for the Dockerfile location or what the image should do, and create a `build:` context in the compose template instead of an `image:` pull.

**If the service is an existing well-known project** (e.g. Gitea, Vaultwarden, Immich), search GitHub for an official or recommended `docker-compose.yml` example and use it as the starting point for the template. Fetch the raw file contents (e.g. from `raw.githubusercontent.com`) and adapt it to the homelab conventions below.

Gather from the user (or infer from context):
1. Service name (lowercase, hyphenated)
2. Docker image and tag (or build context path if building from scratch)
3. Ports to expose (if any)
4. Persistent data volumes needed
5. Whether it goes behind Traefik reverse proxy
6. Whether it needs Authelia authentication
7. Confirm the target host is `daniel-pi` — it is the only Docker host left. If the answer is
   `daniel-server` or `daniel-box`, stop and scaffold a `roles/k8s/` role instead (see the note
   at the top).

Then create the following files:

**`ansible/roles/containers/<name>/tasks/main.yml`**
- Always include a "Create required directories" task first using the `common` role's `setup_dirs.yml`, with at minimum `"{{ container_item.name }}"` in `common_dirs_to_create`
- Follow with a "Deploy Container" task using the `common` role's `docker_deploy.yml`
- **Every task carries a block tag** (right under `name:`): `config` for dirs/templates/
  files/host config, `deploy` for the container lifecycle and post-deploy container ops,
  `cron` for scheduled-job tasks. This enables config-only runs
  (`--tags <name> --skip-tags deploy`). A task whose `register:` feeds another block must
  carry that block's tag too. (`--skip-tags config` is NOT supported — config registers
  feed the recreate decision.)
- Pattern:
  ```yaml
  ---
  - name: Create required directories
    tags: [config]
    ansible.builtin.include_role:
      name: common
      tasks_from: setup_dirs.yml
    vars:
      common_dirs_to_create:
        - "{{ container_item.name }}"

  - name: Deploy Container
    tags: [deploy]
    ansible.builtin.include_role:
      name: common
      tasks_from: docker_deploy.yml
  ```
- Add extra entries to `common_dirs_to_create` if the service needs subdirectories (e.g. config, data)
- **If the service bind-mounts an Ansible-templated/copied config _file_** (a specific file,
  not just a `./config` data dir — e.g. `./app.ini:/etc/app/app.ini:ro`), an edit to that file
  will **silently NOT recreate the container**: `docker_deploy` defaults to `recreate: auto`,
  which only notices `docker-compose.yml`/image changes, never a bind-mounted file's contents.
  You MUST `register:` the `template:`/`copy:` task and pass its `is changed` to
  `common_config_changed` on the `docker_deploy` include. Mirror the freshrss role:
  ```yaml
  {% raw %}- name: Copy nginx feed cache config
    tags: [config]
    ansible.builtin.copy:
      src: nginx-feed-cache.conf
      dest: "/home/{{ sys_user }}/server/containers/{{ container_item.name }}/nginx-feed-cache.conf"
      mode: "0644"
    register: <name>_conf          # <name>_-prefixed

  - name: Deploy Container
    tags: [deploy]
    ansible.builtin.include_role:
      name: common
      tasks_from: docker_deploy.yml
    vars:
      common_config_changed: "{{ <name>_conf is changed }}"   # OR several `is changed`{% endraw %}
  ```
  Files baked into a `build:` context need NONE of this — `build: always` rebuilds them.
  (Root `CLAUDE.md` step 6 + `ansible/roles/containers/common/CLAUDE.md`.)

**`ansible/roles/containers/<name>/templates/docker-compose.yml.j2`**
- Use Jinja2 variables for all configurable values
- **Use the shared macros** (in `ansible/templates/`) — don't hand-roll the boilerplate
  they cover:
  - `traefik.yml.j2` → `labels(...)` — reverse-proxy routing labels
  - `autokuma.yml.j2` → `labels as kuma` — Uptime Kuma monitor labels
  - `networks.yml.j2` → `service_networks()` / `external_networks()` — the per-service
    `networks:` list and the top-level external declaration. **Always use these instead
    of inlining the `{% raw %}{% for net in container_item.networks %}{% endraw %}` loops.**
    Append any extra hardcoded networks (e.g. a private `internal` net) on the lines right
    after the macro call; declare them after `{{ '{{ external_networks() }}' }}`.
  - `resources.yml.j2` → `resources(cpu_limit, mem_limit, cpu_res, mem_res)` — the
    `deploy.resources` limits/reservations caps. Pass all four as strings.
- Include a healthcheck if the image supports one
- Set `restart: unless-stopped`, PUID/PGID, TZ, and a `deploy.resources.limits` cap
- Canonical skeleton:
  ```jinja
  {% raw %}{% from 'traefik.yml.j2' import labels with context %}
  {% from 'autokuma.yml.j2' import labels as kuma with context %}
  {% from 'networks.yml.j2' import service_networks, external_networks with context %}
  {% from 'resources.yml.j2' import resources %}
  ---

  services:
    <name>:
      image: <image>:<tag>
      container_name: <name>
      restart: unless-stopped
      environment:
        - PUID={{ puid }}
        - PGID={{ pgid }}
        - TZ={{ tz }}
      volumes:
        - ./config:/config
      security_opt:
        - no-new-privileges:true
      cap_drop:
        - ALL
      {{ service_networks() }}
      healthcheck:
        test: ["CMD-SHELL", "curl --fail -s http://localhost:<port>/ || exit 1"]
        interval: 30s
        timeout: 10s
        retries: 3
        start_period: 30s
      labels:
        {{ labels(
            container_item.hostname | default(container_item.name),
            container_item.port | string,
            (container_item.networks | default([docker_network]))[0],
            container_item.use_authelia
          )
        }}
        {{ kuma(container_item.name) }}
      {{ resources('1.0', '512M', '0.10', '64M') }}

  {{ external_networks() }}{% endraw %}
  ```

**`ansible/inventory/host_vars/<host>.yml`**
- Add the new service to `containers_list` under the appropriate organization comment section
- Include `name`, `port` (if web-facing), `use_authelia`, and `networks` (deploy tags
  derive from `name` automatically; only set `tags:` to override)

Reference an existing similar container role before writing anything new.
