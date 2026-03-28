---
name: new-container
description: Scaffold a new container service for the homelab — creates the Ansible role, docker-compose template, and registers it in deploy.yml.
allowed-tools: Read, Write, Edit, Glob, Bash
---

Scaffold a new container service for the homelab following the existing patterns.

Gather from the user (or infer from context):
1. Service name (lowercase, hyphenated)
2. Docker image and tag
3. Ports to expose (if any)
4. Persistent data volumes needed
5. Whether it goes behind Traefik reverse proxy
6. Whether it needs Authelia authentication
7. Target host: `daniel-server` or `daniel-pi` (or both)

Then create the following files:

**`ansible/roles/containers/<name>/tasks/main.yml`**
- Use `community.docker.docker_compose_v2` to deploy
- Copy the docker-compose template and any config files
- Follow the idempotent pattern from existing roles

**`ansible/roles/containers/<name>/templates/docker-compose.yml.j2`**
- Use Jinja2 variables for all configurable values
- Include Traefik labels if reverse proxy is needed
- Include a healthcheck if the image supports one
- Set `restart: unless-stopped`, PUID/PGID, TZ

**`ansible/deploy.yml`**
- Add the new role with a tag matching the service name

Reference an existing similar container role before writing anything new.
