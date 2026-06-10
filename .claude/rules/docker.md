---
paths:
  - "containers/**"
  - "ansible/roles/containers/**/*.j2"
---

# Docker Compose Rules

- **NEVER edit files inside `containers/` directly.** That directory is managed by Ansible — it is deployed from templates in `ansible/roles/containers/*/templates/`. Direct edits will be overwritten on the next deploy. Always make changes in the Ansible role template instead.
- All containers must connect to the `proxy` Docker network
- Set `restart: unless-stopped` on all services
- Set PUID/PGID to `1000`/`1000` and TZ to `America/Chicago` via environment variables
- Include Traefik labels for reverse proxy routing on any web-facing service
- Add a healthcheck wherever the image supports it
- Use bind mounts to well-known paths under `/data` for persistent storage — don't use anonymous volumes. Bind mounts under `containers/` are what Kopia backs up.
  - **Documented exception:** a *named* volume is the deliberate pattern for bulky, regenerable state that should stay OUT of Kopia's scope — current uses: `prometheus_data` (TSDB), `loki` (logs), `feed_cache` (freshrss nginx), `karakeep_meili` (rebuildable search index), `crowdsec-db` (shared between traefik+crowdsec). Don't flag these; do justify any new named volume with a comment.
- Image tags should be pinned or use a stable channel (e.g. `latest` is acceptable for homelab but note when a specific version is preferred)
