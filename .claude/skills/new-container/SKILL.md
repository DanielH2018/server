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
- Include Traefik labels if reverse proxy is needed
- Include a healthcheck if the image supports one
- Set `restart: unless-stopped`, PUID/PGID, TZ

**`ansible/inventory/host_vars/<host>.yml`**
- Add the new service to `containers_list` under the appropriate organization comment section
- Include `name`, `port` (if web-facing), `use_authelia`, and `tags` (tag must match service name)

Reference an existing similar container role before writing anything new.
