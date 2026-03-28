---
paths:
  - "containers/**"
  - "ansible/roles/containers/**/*.j2"
---

# Docker Compose Rules

- **NEVER edit files inside `containers/` directly.** That directory is managed by Ansible — it is deployed from templates in `ansible/roles/containers/*/templates/`. Direct edits will be overwritten on the next deploy. Always make changes in the Ansible role template instead.
- All containers must connect to the `proxy` Docker network
- Set `restart: unless-stopped` on all services
- Set PUID/PGID to `1000`/`1000` and TZ to `America/New_York` via environment variables
- Include Traefik labels for reverse proxy routing on any web-facing service
- Add a healthcheck wherever the image supports it
- Use bind mounts to well-known paths under `/data` for persistent storage — don't use anonymous volumes
- Image tags should be pinned or use a stable channel (e.g. `latest` is acceptable for homelab but note when a specific version is preferred)
