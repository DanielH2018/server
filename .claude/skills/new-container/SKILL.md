---
name: new-container
description: Scaffold a new container service for the homelab — creates the Ansible role, docker-compose template, and registers it in deploy.yml.
allowed-tools: Read, Write, Edit, Glob, Bash, WebFetch, WebSearch
---

Scaffold a new container service for the homelab following the existing patterns.

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
7. Target host: `daniel-server` or `daniel-pi` (or both)

Then create the following files:

**`ansible/roles/containers/<name>/tasks/main.yml`**
- Always include a "Create required directories" task first using the `common` role's `setup_dirs.yml`, with at minimum `"{{ container_item.name }}"` in `common_dirs_to_create`
- Follow with a "Deploy Container" task using the `common` role's `docker_deploy.yml`
- Pattern:
  ```yaml
  ---
  - name: Create required directories
    ansible.builtin.include_role:
      name: common
      tasks_from: setup_dirs.yml
    vars:
      common_dirs_to_create:
        - "{{ container_item.name }}"

  - name: Deploy Container
    ansible.builtin.include_role:
      name: common
      tasks_from: docker_deploy.yml
  ```
- Add extra entries to `common_dirs_to_create` if the service needs subdirectories (e.g. config, data)

**`ansible/roles/containers/<name>/templates/docker-compose.yml.j2`**
- Use Jinja2 variables for all configurable values
- **Use the shared macros** (in `ansible/templates/`) — don't hand-roll the boilerplate
  they cover:
  - `traefik.yml.j2` → `labels(...)` — reverse-proxy routing labels
  - `autokuma.yml.j2` → `labels as kuma` — Uptime Kuma monitor labels
  - `healthcheck.yml.j2` → `healthcheck(...)` — healthcheck block with `start_period`
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
  {% from 'healthcheck.yml.j2' import healthcheck %}
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
      {{ healthcheck('curl --fail -s http://localhost:<port>/ || exit 1') }}
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
